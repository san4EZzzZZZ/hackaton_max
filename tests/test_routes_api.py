"""`POST /api/v1/routes/generate` and the codes the published spec promises around it.

The interesting failure modes are the ones a client cannot infer from a status alone: an empty result is
`404` here while `/places` answers `200 []` for the same filters, and a typo in a body key must not be
silently dropped — a swallowed `budget` looks exactly like an ignored budget limit.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from server.routing import haversine_km, travel_minutes
from server.schemas import Location

CITY = "Ростов-на-Дону"
GENERATE = "/api/v1/routes/generate"

# Two streets southwest of the densest corner of the catalog: a real walking distance to the first door,
# and not the address of any place, so a head hop here cannot be mistaken for a zero-length transition.
START = {"lat": 47.22, "lon": 39.72}
# Ninety kilometres north of the city — the walking budget of any legal request runs out before it.
OUT_OF_TOWN = {"lat": 48.0, "lon": 39.72}


def generate(client: TestClient, **body):
    return client.post(GENERATE, json={"city": CITY, **body})


def with_start(client: TestClient, point: dict, **body):
    return generate(client, start_lat=point["lat"], start_lon=point["lon"], **body)


def test_a_matching_request_returns_a_timed_route(client: TestClient) -> None:
    response = generate(client, duration_hours=4)
    assert response.status_code == 200
    route = response.json()
    assert {
        "route_id",
        "title",
        "city",
        "total_duration_hours",
        "total_duration_minutes",
        "total_distance_m",
        "slack_minutes",
        "total_cost",
        "places",
        "stops",
    } <= set(route)
    assert route["city"] == CITY and route["title"].endswith(CITY)
    assert route["stops"], "a matching catalog must produce at least one stop"
    assert [stop["place"] for stop in route["stops"]] == route["places"]
    assert [stop["order"] for stop in route["stops"]] == list(range(1, len(route["stops"]) + 1))
    assert route["total_duration_hours"] <= 4
    assert route["total_cost"] >= 0
    assert {"distance_m_from_prev", "travel_minutes_from_prev"} <= set(route["stops"][0])


def test_the_answer_adds_up_to_the_numbers_it_reports(client: TestClient) -> None:
    route = generate(client, duration_hours=4).json()
    stops = route["stops"]

    # Two groups of fields describe one walk, so they have to agree with each other: the aggregates and
    # the per-stop timeline, and the hours the client rounds against the minutes it should not round.
    assert route["total_duration_minutes"] == sum(
        stop["travel_minutes_from_prev"] + stop["visit_duration_minutes"] for stop in stops
    )
    assert route["total_duration_hours"] == round(route["total_duration_minutes"] / 60, 2)
    assert route["total_distance_m"] == sum(stop["distance_m_from_prev"] for stop in stops)
    assert route["slack_minutes"] == 240 - route["total_duration_minutes"]
    # This request names no origin, so the first stop has nothing to walk from and both fields are 0.
    # `test_a_start_becomes_the_first_leg_of_the_walk` pins the other half of that sentence.
    assert stops[0]["travel_minutes_from_prev"] == stops[0]["distance_m_from_prev"] == 0
    assert all(stop["distance_m_from_prev"] >= 0 for stop in stops)


def test_a_start_becomes_the_first_leg_of_the_walk(client: TestClient) -> None:
    """Where the visitor stands is not a label on the answer — it is the first segment of the timeline.

    The head hop is what makes a start cost anything: without it `start_lat` would only move the marker on
    the map while the plan, its minutes and its budget stayed exactly as they were.
    """
    route = with_start(client, START, duration_hours=4).json()
    first = route["stops"][0]
    origin = Location(**START)
    walked = haversine_km(origin, Location(**first["place"]["location"]))

    assert route["start"] == START, "the answer has to say which walk it planned"
    assert round(walked * 1000) > 0, "the test start must not sit at a catalog address"
    assert first["distance_m_from_prev"] == round(walked * 1000)
    head_minutes = travel_minutes(origin, Location(**first["place"]["location"]))
    assert first["travel_minutes_from_prev"] == head_minutes > 0
    assert first["arrival_offset_minutes"] == first["travel_minutes_from_prev"]
    assert route["total_distance_m"] == sum(stop["distance_m_from_prev"] for stop in route["stops"])
    assert route["total_duration_minutes"] == sum(
        stop["travel_minutes_from_prev"] + stop["visit_duration_minutes"] for stop in route["stops"]
    )
    assert route["slack_minutes"] == 240 - route["total_duration_minutes"]


def test_a_walk_without_an_origin_says_so(client: TestClient) -> None:
    """`start` is part of every answer, not only the ones that sent it: a missing key and a null are
    two different things for a client drawing the map."""
    route = generate(client, duration_hours=4).json()
    assert "start" in route
    assert route["start"] is None


def test_the_answer_reports_time_it_hands_back(client: TestClient) -> None:
    """`slack_minutes` is the number the screen shows as «запас», so it has to be a real slice.

    Until the reserve existed this field was an accident: the generator spent the budget to the minute and
    the leftover was whatever rounding had left behind — 10 minutes on a four-hour day, which is nothing
    in front of a closed door. The client cannot tell those two situations apart from the shape of the
    JSON, so the floor is pinned here rather than inferred from the routing tests.
    """
    for hours in (2.0, 4.0, 8.0):
        route = generate(client, duration_hours=hours).json()
        requested = int(hours * 60)
        assert route["total_duration_minutes"] <= requested, "the day it asked for is the ceiling"
        assert route["slack_minutes"] >= int(requested * 0.15), (
            f"a {hours} h walk hands back {route['slack_minutes']} min"
        )
        assert route["slack_minutes"] == requested - route["total_duration_minutes"]


def test_a_start_far_enough_away_empties_the_catalog(client: TestClient) -> None:
    """Reaching nothing is the same 404 as an unknown category, and the same request without a start
    still returns a route — so the status points at the start, not at the city."""
    assert with_start(client, OUT_OF_TOWN, duration_hours=12).status_code == 404
    assert generate(client, duration_hours=12).status_code == 200


def test_half_a_coordinate_pair_is_refused(client: TestClient) -> None:
    """One coordinate locates nothing, and quietly dropping the half that arrived is the same lie as a
    swallowed `budget`: the client would read a start-shaped route out of a request it thought it made.
    """
    for body in ({"start_lat": START["lat"]}, {"start_lon": START["lon"]}):
        response = generate(client, duration_hours=4, **body)
        assert response.status_code == 422, body
        assert any("start_lat" in str(error) for error in response.json()["detail"]), body


def test_a_start_off_the_face_of_the_earth_is_refused(client: TestClient) -> None:
    assert with_start(client, {"lat": 91.0, "lon": 39.72}).status_code == 422
    assert with_start(client, {"lat": 47.22, "lon": -181.0}).status_code == 422


def test_an_origin_changes_which_places_the_day_picks(client: TestClient) -> None:
    """The whole point of the field: the visitor's position changes what the day looks like.

    Only the difference and the ceiling are asserted, not a particular stop list — which route is better
    from a given corner is the planner's business, and pinning it here would make the test a copy of the
    fixture.
    """
    from_origin = with_start(client, START, duration_hours=4).json()
    unfixed = generate(client, duration_hours=4).json()

    assert {stop["place"]["id"] for stop in from_origin["stops"]} != {
        stop["place"]["id"] for stop in unfixed["stops"]
    }, "the start was ignored: the plan is identical to the one built with no origin at all"
    assert from_origin["total_duration_minutes"] <= 240


def test_two_identical_requests_agree_except_for_their_identifier(client: TestClient) -> None:
    first, second = generate(client, duration_hours=5).json(), generate(client, duration_hours=5).json()
    # `route_id` is a throwaway uuid4 — nothing can be fetched by it, which is why #18 adds persistence.
    assert first["route_id"] != second["route_id"]
    assert _without_id(first) == _without_id(second)


def test_the_budget_bounds_the_whole_route_not_each_stop(client: TestClient) -> None:
    for budget in (100, 400, 1200):
        route = generate(client, max_budget=budget, duration_hours=12).json()
        assert route["total_cost"] <= budget, budget
        assert all(stop["place"]["price"] <= budget for stop in route["stops"]), budget


def test_a_zero_budget_can_only_take_in_free_objects(client: TestClient) -> None:
    route = generate(client, max_budget=0, duration_hours=12).json()
    assert route["stops"], "the catalog does contain free objects"
    assert all(stop["place"]["price"] == 0 for stop in route["stops"])


def test_the_budget_alias_and_the_full_name_mean_the_same(client: TestClient) -> None:
    aliased = generate(client, budget=150).json()
    named = generate(client, max_budget=150).json()
    assert aliased["total_cost"] <= 150
    assert _without_id(aliased) == _without_id(named)


def _without_id(route: dict) -> dict:
    return {key: value for key, value in route.items() if key != "route_id"}


def test_a_key_nobody_recognises_is_refused(client: TestClient) -> None:
    response = generate(client, buget=150)
    assert response.status_code == 422
    errors = response.json()["detail"]
    assert any("buget" in str(error["loc"]) for error in errors), "the answer must name the bad key"
    assert any("extra" in str(error.get("type")) for error in errors)


def test_values_that_cannot_build_a_route_are_refused_by_validation(client: TestClient) -> None:
    assert generate(client, duration_hours=0).status_code == 422
    assert generate(client, duration_hours=12.5).status_code == 422
    assert generate(client, city="А").status_code == 422
    assert generate(client, is_pushkin_card_only="maybe").status_code == 422


def test_a_city_without_any_object_answers_404(client: TestClient) -> None:
    response = client.post(GENERATE, json={"city": "Сочи"})
    assert response.status_code == 404
    assert isinstance(response.json()["detail"], str)


def test_constraints_that_leave_nothing_behind_answer_404(client: TestClient) -> None:
    assert generate(client, categories=["такого категория нет"]).status_code == 404
    # The cheapest Pushkin Card object costs 300, so this budget cannot admit any of them.
    assert generate(client, is_pushkin_card_only=True, max_budget=299).status_code == 404
    # Twenty minutes is the shortest recommended visit in the catalog, so a quarter of an hour fits none.
    assert generate(client, duration_hours=0.25).status_code == 404


def test_only_pushkin_objects_survive_that_switch(client: TestClient) -> None:
    route = generate(client, is_pushkin_card_only=True, duration_hours=12).json()
    assert route["places"]
    assert all(place["is_pushkin_card"] for place in route["places"])


def test_a_chip_walks_only_to_places_that_carried_it(client: TestClient) -> None:
    """`tags` is the axis the setup screen sends instead of a guessed category list.

    The screen used to answer «Перекусить» with `categories: []`, which the planner reads as «no
    filter» — so it built a walk past the zoo and the user saw a coffee chip that did nothing.
    """
    for chip in ("coffee", "culture", "walk", "food", "photo"):
        route = generate(client, duration_hours=4, tags=[chip]).json()
        assert route["stops"], f"чип {chip} не дал маршрута"
        for place in route["places"]:
            assert chip in place["tags"], f'{place["id"]} в подборке {chip}'


def test_two_tags_in_one_request_widen_instead_of_demanding_both(client: TestClient) -> None:
    """OR inside the `tags` list, the same way the categories list already works.

    AND would be the readable wrong reading: «кофе или фото» gives a walk of both kinds of place,
    while «кофе и фото» asks for a coffeehouse that is also a photo spot and returns three stops.
    """
    alone = generate(client, duration_hours=8, tags=["coffee"]).json()["places"]
    joined = generate(client, duration_hours=8, tags=["coffee", "photo"]).json()["places"]
    assert len(joined) >= len(alone), "второй тег расширяет пул, а не требует оба сразу"
    assert any("photo" not in place["tags"] for place in joined), "в подборку попали бы только совмещающие"
    assert any("coffee" not in place["tags"] for place in joined), "тег «photo» не добавил ни одного места"
    assert all({"coffee", "photo"} & set(place["tags"]) for place in joined)


def test_tags_and_categories_narrow_together(client: TestClient) -> None:
    route = generate(client, duration_hours=8, categories=["Парк"], tags=["walk"]).json()
    assert route["stops"]
    for place in route["places"]:
        assert place["category"] == "Парк" and "walk" in place["tags"]
    # Ни один парк не кормит: пересечение пустое, и это тот же 404, что даёт пустая категория.
    assert generate(client, duration_hours=2, categories=["Парк"], tags=["coffee"]).status_code == 404


def test_an_unknown_tag_is_a_filter_with_no_results_not_a_broken_request(client: TestClient) -> None:
    """`RouteRequest.tags` is a list of plain strings on purpose.

    `Literal` here would answer 422 while a wrong category answers 404, and one screen cannot tell the
    two apart: both mean «nothing matched», only one of them means the client sent nonsense.
    """
    response = generate(client, duration_hours=2, tags=["kofe"])
    assert response.status_code == 404
    assert isinstance(response.json()["detail"], str)


def test_the_spec_documents_the_codes_clients_must_handle(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]
    assert paths[GENERATE]["post"]["responses"]["404"]["content"]["application/json"]["schema"][
        "$ref"
    ].endswith("ApiError")
    assert "503" in paths[GENERATE]["post"]["responses"]
    assert "503" in paths["/api/v1/places"]["get"]["responses"]
    assert "404" in paths["/api/v1/places/{place_id}"]["get"]["responses"]
    assert "503" in paths["/readyz"]["get"]["responses"]


def test_the_spec_publishes_the_start_pair(client: TestClient) -> None:
    """`start_lat`/`start_lon` are useless to the Mini App if the generated schema hides them."""
    schemas = client.get("/openapi.json").json()["components"]["schemas"]
    fields = schemas["RouteRequest"]["properties"]
    assert {"start_lat", "start_lon"} <= set(fields)
    for name in ("start_lat", "start_lon"):
        assert fields[name].get("description"), f"{name} has to arrive documented"
    assert "start" in schemas["RouteResponse"]["properties"]

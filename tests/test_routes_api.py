"""`POST /api/v1/routes/generate` and the codes the published spec promises around it.

The interesting failure modes are the ones a client cannot infer from a status alone: an empty result is
`404` here while `/places` answers `200 []` for the same filters, and a typo in a body key must not be
silently dropped — a swallowed `budget` looks exactly like an ignored budget limit.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

CITY = "Ростов-на-Дону"
GENERATE = "/api/v1/routes/generate"


def generate(client: TestClient, **body):
    return client.post(GENERATE, json={"city": CITY, **body})


def test_a_matching_request_returns_a_timed_route(client: TestClient) -> None:
    response = generate(client, duration_hours=4)
    assert response.status_code == 200
    route = response.json()
    assert {
        "route_id",
        "title",
        "city",
        "total_duration_hours",
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
    # 15 minutes is shorter than the shortest recommended visit in the catalog (30).
    assert generate(client, duration_hours=0.25).status_code == 404


def test_only_pushkin_objects_survive_that_switch(client: TestClient) -> None:
    route = generate(client, is_pushkin_card_only=True, duration_hours=12).json()
    assert route["places"]
    assert all(place["is_pushkin_card"] for place in route["places"])


def test_the_spec_documents_the_codes_clients_must_handle(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]
    assert paths[GENERATE]["post"]["responses"]["404"]["content"]["application/json"]["schema"][
        "$ref"
    ].endswith("ApiError")
    assert "503" in paths[GENERATE]["post"]["responses"]
    assert "503" in paths["/api/v1/places"]["get"]["responses"]
    assert "404" in paths["/api/v1/places/{place_id}"]["get"]["responses"]
    assert "503" in paths["/readyz"]["get"]["responses"]

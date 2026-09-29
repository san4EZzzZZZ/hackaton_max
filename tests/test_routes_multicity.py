"""A person picking city + chips + budget has to get a walk out of a *generated* city.

Everything else in the suite plans routes over the hand-written Rostov seed, which is 43 objects
inside two kilometres. A fetched city is different data in three ways that each broke the route
screen in a live run: the places are spread over a whole bounding box (Sochi's answer reached 48 km
along the coast), a museum was budgeted at 90 minutes, and twelve café slots filled with
restaurants, leaving the coffee chip with one object. Those are the regressions this file pins, over
two synthetic cities whose geometry is the shape of the real complaint.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from server.catalog import invalidate_cache
from server.routing import haversine_km
from server.schemas import Location

DENSE = "Тестов"
#: A city whose sights are not neighbours: four in the core, two a nine-kilometre ride away.
SPREAD = "Дальнегорск"

CORE = {"lat": 55.79, "lon": 49.11}


def record(one: dict[str, Any], identifier: str, *, lat: float, lon: float, **overrides: Any) -> dict:
    """A complete, valid catalog entry built from the seed's first record.

    Built from a real record rather than a stub so that a new required field on `Place` fails here
    loudly instead of turning these routes into a walk through empty cards.
    """
    return {
        **one,
        "id": identifier,
        "title": f"Место {identifier}",
        "city": DENSE,
        "location": {"lat": lat, "lon": lon},
        **overrides,
    }


def dense_city(one: dict[str, Any]) -> list[dict]:
    """Twelve objects inside a kilometre, both café moods represented."""
    step = 0.003  # ~330 m of latitude
    culture = [
        record(one, f"museum-{index}", lat=CORE["lat"] + step * index, lon=CORE["lon"],
               category="Музей", tags=["culture", "photo"], visit_duration_minutes=45)
        for index in range(4)
    ]
    parks = [
        record(one, f"park-{index}", lat=CORE["lat"], lon=CORE["lon"] + step * (index + 1),
               category="Парк", tags=["walk", "photo"], visit_duration_minutes=30)
        for index in range(3)
    ]
    coffee = [
        record(one, f"coffee-{index}", lat=CORE["lat"] + step, lon=CORE["lon"] + step * (index + 1),
               category="Кафе", tags=["coffee"], visit_duration_minutes=30, price=350.0)
        for index in range(3)
    ]
    food = [
        record(one, f"food-{index}", lat=CORE["lat"] + 2 * step, lon=CORE["lon"] + step * (index + 1),
               category="Кафе", tags=["food"], visit_duration_minutes=30, price=900.0)
        for index in range(2)
    ]
    pushkin = record(
        one, "pushkin-only", lat=CORE["lat"] + 3 * step, lon=CORE["lon"],
        category="Галерея", tags=["culture"], visit_duration_minutes=45, is_pushkin_card=True, price=600.0,
    )
    return culture + parks + coffee + food + [pushkin]


def spread_city(one: dict[str, Any]) -> list[dict]:
    """Four stops a few hundred metres apart, and two objects nine kilometres out of town."""
    core = [
        record(one, f"core-{index}", lat=CORE["lat"] + 0.003 * index, lon=CORE["lon"],
               city=SPREAD, category="Музей", tags=["culture", "photo"], visit_duration_minutes=45)
        for index in range(4)
    ]
    edge = [
        record(one, f"edge-{index}", lat=CORE["lat"] + 0.09 + 0.003 * index, lon=CORE["lon"],
               city=SPREAD, category="Музей", tags=["culture", "photo"], visit_duration_minutes=45)
        for index in range(2)
    ]
    return core + edge


@pytest.fixture
def generated(client: TestClient, one_place: dict[str, Any], tmp_path) -> TestClient:
    """The app serving the seed plus two generated cities, in a client that never leaves the process.

    `tests/conftest.py` already points the generated-cities directory at this test's `tmp_path`, so
    writing the two files here is what installs the cities: no patching, and nothing of it survives
    the test.
    """
    directory = tmp_path / "places.d"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "testov.json").write_text(
        json.dumps(dense_city(one_place), ensure_ascii=False), encoding="utf-8"
    )
    (directory / "dalnegorsk.json").write_text(
        json.dumps(spread_city(one_place), ensure_ascii=False), encoding="utf-8"
    )
    invalidate_cache()
    return client


def generate(client: TestClient, **body):
    return client.post("/api/v1/routes/generate", json=body)


def legs(answer: dict) -> list[int]:
    return [stop["distance_m_from_prev"] for stop in answer["stops"]]


def stop_places(answer: dict) -> list[dict]:
    return [stop["place"] for stop in answer["stops"]]


# --- the catalog the setup screen offers ---------------------------------------------------------


def test_both_generated_cities_are_selectable(generated: TestClient) -> None:
    cities = {entry["city"]: entry for entry in generated.get("/api/v1/cities").json()}

    assert cities[DENSE]["place_count"] == 13
    assert cities[SPREAD]["place_count"] == 6
    assert set(cities[SPREAD]["categories"]) == {"Музей"}


def test_a_chip_that_cannot_be_filled_is_not_offered(generated: TestClient) -> None:
    """The city selector and the chip row are the same data: no «Взять кофе» in a city of museums.

    `chip_summaries` drops an empty spec, so the client never has to hold its own list of which chips
    happen to work in which city — which is exactly the list that used to lie.
    """
    chips = generated.get("/api/v1/chips", params={"city": SPREAD}).json()

    assert [chip["id"] for chip in chips] == ["culture", "photo"]
    assert all(chip["place_count"] > 0 for chip in chips)


# --- a route built from the filters the screen sends ---------------------------------------------


def test_a_two_hour_culture_walk_in_a_generated_city_is_a_walk(generated: TestClient) -> None:
    """The regression the fix is named for: ninety-minute museums made «Культура» on two hours one stop.

    Non-vacuous because the same request on the old durations returned a single 90-minute museum with
    half its budget unspent, and every other assertion in this test passed then too.
    """
    answer = generate(generated, city=DENSE, tags=["culture"], duration_hours=2).json()

    assert len(answer["stops"]) >= 2
    assert all("culture" in place["tags"] for place in stop_places(answer))
    assert answer["total_duration_minutes"] <= 120


def test_a_longer_day_buys_more_stops_not_the_same_two(generated: TestClient) -> None:
    short = generate(generated, city=DENSE, tags=["culture"], duration_hours=2).json()
    long = generate(generated, city=DENSE, tags=["culture"], duration_hours=4).json()

    assert len(long["stops"]) >= 3 > 1
    assert len(long["stops"]) > len(short["stops"])


def test_the_coffee_chip_reaches_more_than_one_door(generated: TestClient) -> None:
    """Twelve café slots filled with restaurants left one coffee place in Kazan.

    Both moods of the category have to survive the cap, because a chip whose answer is a single stop is
    a chip the setup screen should not have shown at all.
    """
    answer = generate(generated, city=DENSE, tags=["coffee"], duration_hours=2).json()

    assert len(answer["stops"]) >= 2
    assert all(place["tags"] == ["coffee"] for place in stop_places(answer))


def test_categories_and_chips_narrow_a_generated_city_together(generated: TestClient) -> None:
    """Парк and walk are independent axes here, as they are in `GET /places`: nothing else leaks in."""
    answer = generate(generated, city=DENSE, categories=["Парк"], tags=["walk"], duration_hours=2).json()
    places = stop_places(answer)

    assert len(places) >= 2
    assert {place["category"] for place in places} == {"Парк"}
    assert all("walk" in place["tags"] for place in places)


def test_budget_and_pushkin_card_are_honoured_in_a_generated_city(generated: TestClient) -> None:
    cheap = generate(generated, city=DENSE, max_budget=800, duration_hours=4).json()

    assert cheap["total_cost"] <= 800
    assert all(place["price"] <= 800 for place in stop_places(cheap))

    pushkin = generate(
        generated, city=DENSE, is_pushkin_card_only=True, duration_hours=4
    ).json()
    assert all(place["is_pushkin_card"] for place in stop_places(pushkin))


def test_a_filter_that_matches_nothing_in_this_city_answers_404(generated: TestClient) -> None:
    """A generated city is allowed to be empty for a filter; an invented route is not."""
    assert generate(generated, city=SPREAD, tags=["coffee"], duration_hours=3).status_code == 404
    assert generate(generated, city="Шудареченск", duration_hours=3).status_code == 404


# --- the geometry of the answer --------------------------------------------------------------------


def test_a_city_spread_over_a_region_is_still_one_walk(generated: TestClient) -> None:
    """Two monuments 4 km apart were the answer to a two-hour request in Sochi, twice.

    Ranking by stop count and then by spread made the distant pair the best two-stop route, and the
    visitor got an hour of walking between them. A hop longer than `MAX_LEG_KM` is now not a route, so
    the answer is the cluster the walk can actually cover — which is why this asserts the spread of
    the whole set, not only the legs it reports.
    """
    answer = generate(generated, city=SPREAD, duration_hours=4).json()
    places = stop_places(answer)

    assert len(places) >= 3
    assert max(legs(answer)) <= 2500
    corners = [Location(**place["location"]) for place in places]
    assert max(haversine_km(a, b) for a in corners for b in corners) <= 2.5


def test_the_origin_moves_the_walk_to_the_far_side_of_the_city(generated: TestClient) -> None:
    """Standing nine kilometres out, the visitor is shown what is around them, not the city centre.

    The core is closer to unreachable than to a first stop: `filter_candidates` drops what cannot be
    walked to inside the requested time, so the far pair is all the two remaining hours can spend. And
    the museum the origin stands on is not offered back as a stop with a zero-metre walk to it — the
    walk starts at the next door.
    """
    answer = generate(
        generated,
        city=SPREAD,
        duration_hours=2,
        start_lat=CORE["lat"] + 0.09,
        start_lon=CORE["lon"],
    ).json()

    assert [place["id"] for place in stop_places(answer)] == ["edge-1"]
    assert answer["total_duration_minutes"] <= 120

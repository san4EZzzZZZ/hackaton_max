"""HTTP contract of the catalog endpoints, as the Mini App sees it.

Two shapes are load-bearing for the frontend and easy to break by accident: `/places` answers a bare
JSON array (not a `{items, total}` envelope), and an empty selection is `200 []` rather than an error.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from conftest import app_client

import server.catalog as catalog_module

CITY = "Ростов-на-Дону"
PLACES = "/api/v1/places"
CATEGORIES = "/api/v1/categories"
CITIES = "/api/v1/cities"


def ids(response) -> list[str]:
    return [item["id"] for item in response.json()]


def test_places_answers_a_bare_array_of_every_known_object(client: TestClient, places: list[dict]) -> None:
    response = client.get(PLACES)
    assert response.status_code == 200
    assert isinstance(response.json(), list), "the envelope shape is part of the published contract"
    assert len(response.json()) == len(places) >= 1, "the fixture and the endpoint must read the same file"
    assert {"id", "title", "category", "location", "city", "price"} <= set(response.json()[0])


def test_place_filters_narrow_the_same_collection(client: TestClient, places: list[dict]) -> None:
    museums = [item for item in places if item["category"] == "Музей"]
    assert museums, "the seed data must keep an object in this category"
    assert len(client.get(PLACES, params={"category": "Музей"}).json()) == len(museums)
    assert len(client.get(PLACES, params={"category": "музей"}).json()) == len(museums)
    assert len(client.get(PLACES, params={"city": "Ростов"}).json()) == len(places)
    pushkin = [item for item in places if item["is_pushkin_card"]]
    assert len(client.get(PLACES, params={"is_pushkin_card": True}).json()) == len(pushkin) == 5
    free = [item for item in places if item["price"] == 0]
    assert len(client.get(PLACES, params={"max_price": 0}).json()) == len(free)


def test_filters_combine(client: TestClient) -> None:
    response = client.get(
        PLACES, params={"city": "ростов-на-Дону", "category": "Театр", "is_pushkin_card": True}
    )
    assert response.status_code == 200
    for item in response.json():
        assert item["city"] == CITY and item["category"] == "Театр" and item["is_pushkin_card"]


def test_an_empty_selection_is_not_an_error(client: TestClient) -> None:
    response = client.get(PLACES, params={"city": "Москва"})
    assert response.status_code == 200
    assert response.json() == []
    assert client.get(PLACES, params={"max_price": 0.01, "is_pushkin_card": False}).status_code == 200


def test_an_impossible_price_is_rejected_before_it_is_evaluated(client: TestClient) -> None:
    response = client.get(PLACES, params={"max_price": -1})
    assert response.status_code == 422
    assert isinstance(response.json()["detail"], list), "422 keeps FastAPI's error list"


def test_a_known_object_comes_back_whole(client: TestClient) -> None:
    listed = client.get(PLACES).json()[0]
    detail = client.get(f"{PLACES}/{listed['id']}")
    assert detail.status_code == 200
    assert detail.json() == listed


def test_an_unknown_object_is_a_404_with_a_readable_detail(client: TestClient) -> None:
    response = client.get(f"{PLACES}/no-such-place")
    assert response.status_code == 404
    # The wording belongs to the endpoint, not to the contract; only its shape is asserted here.
    detail = response.json()["detail"]
    assert isinstance(detail, str) and detail.strip()


def test_categories_are_the_ones_the_data_actually_has(client: TestClient, places: list[dict]) -> None:
    response = client.get(CATEGORIES)
    assert response.status_code == 200
    assert response.json() == list(dict.fromkeys(item["category"] for item in places))


@pytest.mark.parametrize(
    "path",
    [
        PLACES,
        f"{PLACES}/theatre-square",
        CATEGORIES,
        CITIES,
        "/api/v1/routes/generate",
    ],
)
def test_an_unreadable_catalog_reports_503_rather_than_500(tmp_path: Path, monkeypatch, path: str) -> None:
    monkeypatch.setattr(catalog_module, "DATA_FILE", tmp_path / "absent.json")
    with app_client() as client:
        if path.endswith("generate"):
            response = client.post(path, json={"city": CITY})
        else:
            response = client.get(path)
        assert response.status_code == 503, path
        assert "detail" in response.json(), path


def test_a_search_word_reaches_the_text_a_visitor_reads(client: TestClient) -> None:
    # "фонтан" appears in no title — it lives in the description of Театральная площадь.
    found = client.get(PLACES, params={"q": "фонтан"})
    assert found.status_code == 200
    assert [item["id"] for item in found.json()] == ["theatre-square"]
    assert found.headers["X-Total-Count"] == "1"


def test_search_words_narrow_instead_of_broadening(client: TestClient) -> None:
    broad = {item["id"] for item in client.get(PLACES, params={"q": "ростов"}).json()}
    narrow = {item["id"] for item in client.get(PLACES, params={"q": "ростов девушка"}).json()}
    assert broad > narrow, "adding a word can only cut the result set"
    assert client.get(PLACES, params={"q": "ростов несуществующее слово"}).json() == []


def test_search_combines_with_the_other_filters(client: TestClient) -> None:
    items = client.get(
        PLACES, params={"city": "Ростов", "q": "музей", "min_rating": 4.5}
    ).json()
    assert items
    for item in items:
        assert item["rating"] >= 4.5
        assert "музей" in (item["title"] + (item["description"] or "") + item["category"]).lower()


def test_a_query_shorter_than_two_characters_is_refused_by_validation(client: TestClient) -> None:
    response = client.get(PLACES, params={"q": "а"})
    assert response.status_code == 422
    assert isinstance(response.json()["detail"], list), "this one is FastAPI's own error list"


def test_min_rating_keeps_objects_at_or_above_the_line(client: TestClient, places: list[dict]) -> None:
    threshold = 4.6
    expected = [item["id"] for item in places if item["rating"] >= threshold]
    response = client.get(PLACES, params={"min_rating": threshold})
    assert [item["id"] for item in response.json()] == expected
    assert response.headers["X-Total-Count"] == str(len(expected))


def test_min_rating_out_of_range_is_refused(client: TestClient) -> None:
    assert client.get(PLACES, params={"min_rating": 5.5}).status_code == 422
    assert client.get(PLACES, params={"min_rating": -1}).status_code == 422


def test_radius_selects_by_straight_line_distance(client: TestClient, places: list[dict]) -> None:
    from server.routing import haversine_km
    from server.schemas import Location

    center = Location(**places[0]["location"])
    widths = []
    for radius in (1, 3, 10):
        response = client.get(
            PLACES,
            params={"near_lat": center.lat, "near_lon": center.lon, "radius_km": radius},
        )
        assert response.status_code == 200, radius
        expected = {
            item["id"]
            for item in places
            if haversine_km(center, Location(**item["location"])) <= radius
        }
        assert {item["id"] for item in response.json()} == expected, radius
        assert places[0]["id"] in {item["id"] for item in response.json()}, "the center is at 0 km"
        widths.append(len(response.json()))
    assert widths == sorted(widths) and widths[0] < widths[-1]


def test_radius_needs_a_complete_center(client: TestClient, places: list[dict]) -> None:
    center = places[0]["location"]
    # These two 422s carry a plain message, unlike validation errors: the mistake is a combination of
    # individually valid parameters, which the request model cannot express.
    only_radius = client.get(PLACES, params={"radius_km": 5})
    assert only_radius.status_code == 422
    assert isinstance(only_radius.json()["detail"], str)
    assert "near_lat" in only_radius.json()["detail"]

    half_center = client.get(PLACES, params={"near_lat": center["lat"]})
    assert half_center.status_code == 422
    assert isinstance(half_center.json()["detail"], str)

    assert client.get(
        PLACES, params={"near_lat": center["lat"], "near_lon": center["lon"]}
    ).status_code == 200, "a center without a radius just orders nothing away"


def test_a_page_is_a_slice_of_the_unpaginated_collection(client: TestClient, places: list[dict]) -> None:
    whole = [item["id"] for item in client.get(PLACES).json()]
    assert whole == [item["id"] for item in places]

    response = client.get(PLACES, params={"limit": 4, "offset": 6})
    assert [item["id"] for item in response.json()] == whole[6:10]
    assert response.headers["X-Offset"] == "6"

    first_page = client.get(PLACES, params={"limit": 2})
    assert [item["id"] for item in first_page.json()] == whole[:2]
    assert len(first_page.json()) == 2 < len(whole)


def test_the_total_header_ignores_the_page_size(client: TestClient, places: list[dict]) -> None:
    totals = {
        client.get(PLACES, params=params).headers["X-Total-Count"]
        for params in (
            {},
            {"limit": 1},
            {"limit": 1, "offset": 1},
            {"offset": len(places)},
            {"city": "Ростов"},
        )
    }
    assert totals == {str(len(places))}
    museums = client.get(PLACES, params={"category": "Музей", "limit": 1})
    assert int(museums.headers["X-Total-Count"]) == len(
        [item for item in places if item["category"] == "Музей"]
    )
    assert len(museums.json()) == 1


def test_an_offset_past_the_end_answers_an_empty_page(client: TestClient, places: list[dict]) -> None:
    response = client.get(PLACES, params={"offset": len(places) + 50})
    assert response.status_code == 200
    assert response.json() == []
    assert response.headers["X-Total-Count"] == str(len(places))


def test_pages_have_boundaries_a_client_can_rely_on(client: TestClient) -> None:
    assert client.get(PLACES, params={"limit": 0}).status_code == 422
    assert client.get(PLACES, params={"limit": 201}).status_code == 422
    assert client.get(PLACES, params={"offset": -1}).status_code == 422
    assert client.get(PLACES, params={"radius_km": 0}).status_code == 422


def test_cities_offers_only_what_the_catalog_can_show(client: TestClient, places: list[dict]) -> None:
    response = client.get(CITIES)
    assert response.status_code == 200
    summary = response.json()
    assert isinstance(summary, list) and summary
    assert {item["city"] for item in summary} == {place["city"] for place in places}
    assert sum(item["place_count"] for item in summary) == len(places)
    for item in summary:
        own = [place for place in places if place["city"] == item["city"]]
        assert item["categories"] == list(dict.fromkeys(place["category"] for place in own))
    assert {"city", "place_count", "categories"} == set(summary[0])


def test_the_new_query_parameters_and_headers_are_documented(client: TestClient) -> None:
    spec = client.get("/openapi.json").json()
    place_params = {parameter["name"] for parameter in spec["paths"][PLACES]["get"]["parameters"]}
    assert {"q", "min_rating", "near_lat", "near_lon", "radius_km", "limit", "offset"} <= place_params
    headers = spec["paths"][PLACES]["get"]["responses"]["200"]["headers"]
    assert set(headers) == {"X-Total-Count", "X-Offset"}
    responses = spec["paths"][PLACES]["get"]["responses"]
    assert {"200", "422", "503"} <= set(responses)
    # 422 on this endpoint really has two shapes, and the contract has to admit both.
    declared = responses["422"]["content"]["application/json"]["schema"]["oneOf"]
    assert {"$ref": "#/components/schemas/HTTPValidationError"} in declared
    assert {"$ref": "#/components/schemas/ApiError"} in declared
    assert CITIES in spec["paths"], "the frontend reads the contract, not the code"

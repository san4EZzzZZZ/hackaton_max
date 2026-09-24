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


def ids(response) -> list[str]:
    return [item["id"] for item in response.json()]


def test_places_answers_a_bare_array_of_every_known_object(client: TestClient, places: list[dict]) -> None:
    response = client.get(PLACES)
    assert response.status_code == 200
    assert isinstance(response.json(), list), "the envelope shape is part of the published contract"
    assert len(response.json()) == len(places) == 18
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
    assert response.json() == {"detail": "Место 'no-such-place' не найдено"}


def test_categories_are_the_ones_the_data_actually_has(client: TestClient, places: list[dict]) -> None:
    response = client.get(CATEGORIES)
    assert response.status_code == 200
    assert response.json() == list(dict.fromkeys(item["category"] for item in places))


@pytest.mark.parametrize(
    "path",
    [PLACES, f"{PLACES}/theatre-square", CATEGORIES, "/api/v1/routes/generate"],
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

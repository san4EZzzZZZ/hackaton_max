"""`GET /api/v1/chips` — the chip row of the setup screen, counted out of the catalog.

The screen used to draw five moods somebody had guessed at and to send no filter for three of them, so
«Взять кофе» produced a walk past the zoo. This endpoint exists so the row can only contain chips the
data can fill, which makes an empty chip a thing that cannot be served rather than a bug in the frontend.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient
from conftest import app_client

import server.catalog as catalog_module
from server.catalog import CHIPS

CHIPS_PATH = "/api/v1/chips"

CHIP_KEYS = {"id", "label", "emoji", "tags", "place_count"}


def is_subsequence(serve: list[str], declared: tuple[str, ...]) -> bool:
    """Same order as `CHIPS`, entries allowed to be missing — which is what a chip with no stops does."""
    return serve == [chip_id for chip_id in declared if chip_id in serve]



def test_chips_answer_only_the_moods_the_catalog_can_fill(client: TestClient) -> None:
    response = client.get(CHIPS_PATH)
    assert response.status_code == 200
    body = response.json()
    assert isinstance(body, list), "the envelope shape is part of the published contract"
    assert body, "пустой ответ оставил бы экран настройки с пятью мёртвыми кнопками"
    for chip in body:
        assert set(chip) == CHIP_KEYS, chip
        assert chip["place_count"] >= 1, f"чип {chip['id']} приехал без мест"
        assert chip["label"].strip() and chip["emoji"].strip()
        assert chip["tags"], f"чип {chip['id']} ничего не выбирает"
    assert is_subsequence([chip["id"] for chip in body], tuple(spec.id for spec in CHIPS))


def test_a_chip_the_data_cannot_fill_never_reaches_the_client(
    tmp_path: Path, monkeypatch, one_place: dict
) -> None:
    """`Field(ge=1)` on `place_count` is the whole promise, so it has to be load-bearing.

    One place, one tag: the remaining chips have nothing to show, and the honest answer about a chip
    with no stops is not to offer it at all.
    """
    seed = tmp_path / "places.json"
    seed.write_text(
        json.dumps([dict(one_place, tags=["walk"])], ensure_ascii=False), encoding="utf-8"
    )
    monkeypatch.setattr(catalog_module, "DATA_FILE", seed)
    with app_client() as client:
        body = client.get(CHIPS_PATH).json()
    assert [chip["id"] for chip in body] == ["walk"]
    assert body[0]["place_count"] == 1


def test_a_city_the_catalog_does_not_have_gets_no_chips(client: TestClient) -> None:
    assert client.get(CHIPS_PATH, params={"city": "Москва"}).json() == []
    # Spelling tolerance is the same helper the rest of the API uses, so this really is the same city.
    loose = [chip["id"] for chip in client.get(CHIPS_PATH, params={"city": "ростов"}).json()]
    assert loose == [chip["id"] for chip in client.get(CHIPS_PATH).json()]


def test_an_empty_catalog_offers_no_chips_rather_than_the_full_row(
    tmp_path: Path, monkeypatch
) -> None:
    seed = tmp_path / "places.json"
    seed.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(catalog_module, "DATA_FILE", seed)
    with app_client() as client:
        response = client.get(CHIPS_PATH)
        assert response.status_code == 200
        assert response.json() == []


def test_the_chip_endpoint_documents_its_answers(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    responses = schema["paths"][CHIPS_PATH]["get"]["responses"]
    assert responses["200"]["content"]["application/json"]["schema"]["items"]["$ref"].endswith(
        "/Chip"
    )
    assert "503" in responses, "битый сид должен быть предсказуем и здесь"
    assert "tags" in schema["components"]["schemas"]["Place"]["properties"]
    assert "tags" in schema["components"]["schemas"]["RouteRequest"]["properties"]

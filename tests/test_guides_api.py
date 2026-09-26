"""«Справка по пути» as the Mini App sees it: what a guide echoes, and which failures are which.

The two distinctions the client cannot recover from on its own are asserted here: 404 versus 503 on a
guide screen (nobody wrote this one yet, as opposed to the feature being down), and the guide screen
quoting the catalog for price and opening hours rather than trusting its own file.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from conftest import app_client, guide_record, write_guides

import server.guides as guides_module
from server.guides import NEARBY_LIMIT, guide_for, load_guides
from server.routing import travel_minutes
from server.schemas import Location

GUIDES = "/api/v1/guides"
PLACES = "/api/v1/places"
GUIDE_IN_SPEC = "/api/v1/places/{place_id}/guide"
CITY = "Ростов-на-Дону"


def guide_path(place_id: str) -> str:
    return f"{PLACES}/{place_id}/guide"


def guided_ids() -> list[str]:
    return [guide.place_id for guide in load_guides()]


def test_the_index_lists_every_written_guide_in_the_order_the_editor_kept(
    client: TestClient, places: list[dict]
) -> None:
    response = client.get(GUIDES)
    assert response.status_code == 200
    catalog = {place["id"]: place for place in places}
    listed = response.json()
    assert [item["place_id"] for item in listed] == guided_ids()
    for item in listed:
        place = catalog[item["place_id"]]
        assert item["title"] == place["title"]
        assert item["category"] == place["category"] and item["city"] == place["city"]
        assert item["status"] in {"ready", "seed"}
        # One picture address for the client: the badge and the guide screen cannot diverge.
        assert item["cover_url"] == place["image_url"]
        assert item["media_count"] == len(guide_for(item["place_id"]).media)
    assert {"place_id", "title", "category", "city", "status", "cover_url", "media_count",
            "last_verified"} == set(listed[0])


def test_the_index_can_be_filtered_by_city_like_the_catalog(client: TestClient) -> None:
    assert client.get(GUIDES, params={"city": CITY}).json() == client.get(GUIDES).json()
    assert client.get(GUIDES, params={"city": "ростов"}).json() == client.get(GUIDES).json()
    assert client.get(GUIDES, params={"city": "Сочи"}).json() == []


def test_every_listed_place_answers_its_own_guide(client: TestClient) -> None:
    for item in client.get(GUIDES).json():
        response = client.get(guide_path(item["place_id"]))
        assert response.status_code == 200, item["place_id"]
        assert response.json()["history"] == guide_for(item["place_id"]).history


def test_a_guide_echoes_the_catalog_instead_of_repeating_it(client: TestClient, places: list[dict]) -> None:
    """`price`, `working_hours` and `visit_duration_minutes` are read from the place, not the text.

    The editor is not the system of record for a ticket price, and a route quotes the catalog for the
    same number — so a guide that carried its own would disagree with the walk that just opened it.
    """
    place = next(item for item in places if item["id"] == "theatre-square")
    body = client.get(guide_path("theatre-square")).json()
    for field in ("title", "category", "city", "price", "working_hours", "visit_duration_minutes"):
        assert body[field] == place[field], field
    assert body["location"] == place["location"]
    assert body["place_id"] == place["id"]


def test_a_guide_carries_the_editors_provenance(client: TestClient) -> None:
    body = client.get(guide_path("pokrov-church")).json()
    stored = guide_for("pokrov-church")
    assert body["provenance"] == stored.provenance.model_dump(mode="json")
    assert body["provenance"]["last_verified"] == "2026-09-27"
    assert body["provenance"]["source_url"].startswith("https://ru.wikipedia.org/wiki/")
    assert body["status"] == "ready"
    assert body["media"] == [media.model_dump() for media in stored.media]
    assert body["highlights"] == stored.highlights


def test_the_neighbours_are_the_route_engines_own_numbers(client: TestClient, places: list[dict]) -> None:
    """Same walking model, same minutes: the guide screen may not promise a shorter street than the
    route screen charges for it."""
    catalog = {place["id"]: place for place in places}
    for item in client.get(GUIDES).json():
        body = client.get(guide_path(item["place_id"])).json()
        origin = Location(**catalog[item["place_id"]]["location"])
        nearby = body["nearby"]
        assert len(nearby) == NEARBY_LIMIT
        assert item["place_id"] not in [neighbour["id"] for neighbour in nearby]
        for neighbour in nearby:
            assert neighbour["travel_minutes"] == travel_minutes(
                origin, Location(**catalog[neighbour["id"]]["location"])
            )
            assert neighbour["title"] == catalog[neighbour["id"]]["title"]
        assert [neighbour["distance_m"] for neighbour in nearby] == sorted(
            neighbour["distance_m"] for neighbour in nearby
        )


def test_a_place_without_a_guide_says_so_in_a_way_the_client_can_show(
    client: TestClient, places: list[dict]
) -> None:
    guided = set(guided_ids())
    unwritten = next(place for place in places if place["id"] not in guided)
    response = client.get(guide_path(unwritten["id"]))
    assert response.status_code == 404
    detail = response.json()["detail"]
    assert "ещё нет" in detail and unwritten["title"] in detail, "плашка и текст должны сходиться"


def test_an_unknown_id_is_a_404_about_the_place_not_the_guide(client: TestClient) -> None:
    response = client.get(guide_path("no-such-place"))
    assert response.status_code == 404
    assert "не найдено" in response.json()["detail"]


def test_a_guide_for_a_place_that_is_no_longer_in_the_catalog_is_a_503(
    tmp_path: Path, monkeypatch
) -> None:
    """A dangling `place_id` is a broken data file, and both screens admit it rather than guessing.

    Dropping the row would hide a badge the index had already promised; inventing a title would put a
    word nobody wrote in the app. So the feature reports itself unavailable, in one place at a time.
    """
    write_guides(monkeypatch, tmp_path, guide_record("theatre-square"), guide_record("gone-place"))
    with app_client() as client:
        assert client.get(GUIDES).status_code == 503
        assert "gone-place" in client.get(GUIDES).json()["detail"]
        # The catalog half of the answer is unchanged, and so is the place that never existed.
        assert client.get(PLACES).status_code == 200
        assert client.get(guide_path("gone-place")).status_code == 404


def test_a_broken_guide_file_takes_the_guides_down_and_nothing_else(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(guides_module, "DATA_FILE", tmp_path / "absent.json")
    with app_client() as client:
        assert client.get(GUIDES).status_code == 503
        assert client.get(guide_path("theatre-square")).status_code == 503
        assert "not found" in client.get(guide_path("theatre-square")).json()["detail"]
        assert client.get(PLACES).status_code == 200, "каталог читается — прогулки строятся"
        assert client.get(f"{PLACES}/theatre-square").status_code == 200


@pytest.mark.parametrize(
    "content",
    ["это не json", '{"history": []}', "42"],
)
def test_a_damaged_guide_file_is_a_503_rather_than_a_500(
    tmp_path: Path, monkeypatch, content: str
) -> None:
    broken = tmp_path / "place_guides.json"
    broken.write_text(content, encoding="utf-8")
    monkeypatch.setattr(guides_module, "DATA_FILE", broken)
    with app_client() as client:
        for path in (GUIDES, guide_path("theatre-square")):
            response = client.get(path)
            assert response.status_code == 503, path
            assert response.json()["detail"], path


def test_an_empty_guide_file_is_a_valid_empty_index(tmp_path: Path, monkeypatch) -> None:
    """The feature shipping nothing is a state, not a failure: `[]` and 404s, no 503 anywhere."""
    write_guides(monkeypatch, tmp_path)
    with app_client() as client:
        assert client.get(GUIDES).json() == []
        assert client.get(guide_path("theatre-square")).status_code == 404
        assert "ещё нет" in client.get(guide_path("theatre-square")).json()["detail"]


def test_both_endpoints_are_part_of_the_published_contract(client: TestClient) -> None:
    spec = client.get("/openapi.json").json()
    index = spec["paths"][GUIDES]["get"]
    guide = spec["paths"][GUIDE_IN_SPEC]["get"]
    assert index["responses"]["503"]["description"]
    assert {"404", "503"} <= set(guide["responses"])
    assert guide["responses"]["404"]["description"]
    for operation in (index, guide):
        description = operation["description"]
        assert description and description == description.strip()
    parameters = {parameter["name"] for parameter in index["parameters"]}
    assert parameters == {"city"}
    # The guide answer is the editor record plus the catalog echo; a client reading the contract
    # should see the text fields and `nearby` without opening the code.
    schema = guide["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
    name = schema.rsplit("/", 1)[-1]
    properties = spec["components"]["schemas"][name]["properties"]
    assert {"history", "highlights", "media", "provenance", "nearby", "price"} <= set(properties)
    assert spec["components"]["schemas"]["NearbyPlace"]["properties"]["travel_minutes"]["description"]

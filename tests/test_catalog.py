"""The catalog layer: city spelling, category discovery and the failures that mean HTTP 503."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

import server.catalog as catalog_module
from server.catalog import (
    CatalogError,
    city_matches,
    invalidate_cache,
    known_categories,
    known_cities,
    load_places,
    search_matches,
)
from server.schemas import Location, Place

CATALOG_CITY = "Ростов-на-Дону"


@pytest.mark.parametrize(
    "query",
    ["Ростов", "ростов", "ростов-на-Дону", "Ростов на Дону", "Ростов-на-Дона", "РОСТОВ-НА-ДОНУ"],
)
def test_every_spelling_people_actually_type_matches(query: str) -> None:
    assert city_matches(query, CATALOG_CITY)


@pytest.mark.parametrize("query", ["Москва", "Ростов Область", "Дон", "на", "", "   "])
def test_a_query_that_is_not_this_city_does_not_match(query: str) -> None:
    # "на" collapses to nothing once noise words are dropped: an empty token list must not match
    # everything, or a stray filter would silently widen the results.
    assert not city_matches(query, CATALOG_CITY)


def test_known_categories_follow_catalog_order_without_duplicates(places: list[dict]) -> None:
    found = known_categories()
    typed_order = list(dict.fromkeys(place["category"] for place in places))
    assert found == tuple(typed_order)
    assert len(found) == len(set(found))


def test_load_places_returns_validated_models(places: list[dict]) -> None:
    loaded = load_places()
    assert len(loaded) == len(places) > 0
    assert {type(item) for item in loaded} == {Place}
    assert len({item.id for item in loaded}) == len(loaded), "place ids must be unique"
    assert all(item.rating <= 5 and item.visit_duration_minutes >= 15 for item in loaded)


def test_a_missing_seed_file_is_a_catalog_error(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(catalog_module, "DATA_FILE", tmp_path / "absent.json")
    with pytest.raises(CatalogError, match="not found"):
        load_places()


@pytest.mark.parametrize("content", ["not json at all", '[{"title": "без координат"}]', '{"id": 1}'])
def test_an_unusable_seed_file_is_a_catalog_error(
    tmp_path: Path, monkeypatch, content: str
) -> None:
    broken = tmp_path / "places.json"
    broken.write_text(content, encoding="utf-8")
    monkeypatch.setattr(catalog_module, "DATA_FILE", broken)
    with pytest.raises(CatalogError):
        load_places()


def test_an_empty_seed_file_loads_as_an_empty_catalog(tmp_path: Path, monkeypatch) -> None:
    """`[]` is a valid, if useless, catalog — the endpoints then answer `200 []` and `404`, not 503."""
    empty = tmp_path / "places.json"
    empty.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(catalog_module, "DATA_FILE", empty)
    assert load_places() == ()


def test_the_seed_file_itself_is_the_documented_shape() -> None:
    raw = json.loads(catalog_module.DATA_FILE.read_text(encoding="utf-8"))
    assert isinstance(raw, list) and raw
    assert {"id", "title", "category", "location", "city"} <= set(raw[0])


def test_place_tolerates_the_fields_a_scraper_forgets() -> None:
    minimal = Place(
        id="x",
        title="  Обрезка пробелов  ",
        category="Музей",
        location=Location(lat=47.2, lon=39.7),
    )
    assert minimal.title == "Обрезка пробелов"
    assert minimal.city == CATALOG_CITY
    assert minimal.price == 0.0
    assert minimal.rating == 5.0
    assert minimal.visit_duration_minutes == 60
    assert minimal.image_url is None


def test_place_rejects_nonsense_but_ignores_stray_keys() -> None:
    base = dict(
        id="x", title="T", category="C", location={"lat": 0, "lon": 0}
    )
    with pytest.raises(ValidationError):
        Place(**base, rating=5.5)
    with pytest.raises(ValidationError):
        Place(**base, price=-1)
    with pytest.raises(ValidationError):
        Place(**base, visit_duration_minutes=5)
    assert Place(**base, source_of_truth="wikidata").category == "C"


def write_seed(tmp_path: Path, entries: list[dict]) -> Path:
    seed = tmp_path / "places.json"
    seed.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
    return seed


def test_search_finds_words_from_any_readable_field(one_place: dict) -> None:
    target = Place.model_validate(one_place)
    for query in (one_place["title"].split()[0], one_place["category"], "ростов"):
        assert search_matches(target, query), query


def test_search_needs_every_word_of_the_query(one_place: dict) -> None:
    target = Place.model_validate(one_place)
    assert search_matches(target, one_place["title"])
    # One invented word makes the conjunction false; search never falls back to "best match".
    assert not search_matches(target, f'{one_place["title"]} nonexistentword')


def test_search_ignores_case_and_how_the_words_are_separated(one_place: dict) -> None:
    title = one_place["title"]
    target = Place.model_validate(one_place)
    assert search_matches(target, title.upper())
    assert search_matches(target, f"  {title}  ")
    assert search_matches(target, title.replace(" ", "  ")), "extra gaps are just separators"


@pytest.mark.parametrize("query", ["", "   ", "!", "«»"])
def test_a_query_without_words_matches_nothing(one_place: dict, query: str) -> None:
    assert not search_matches(Place.model_validate(one_place), query)


def test_known_cities_groups_the_catalog_in_order_of_appearance(places: list[dict]) -> None:
    grouped = known_cities()
    assert list(grouped) == list(dict.fromkeys(place["city"] for place in places))
    assert sum(len(items) for items in grouped.values()) == len(places)
    assert grouped, "an empty catalog would leave the city selector with nothing to offer"
    assert all(isinstance(place, Place) for items in grouped.values() for place in items)


def test_an_edited_seed_file_is_served_only_after_the_cache_drops(
    tmp_path: Path, monkeypatch, one_place: dict
) -> None:
    """`load_places` is memoized, which is why editing `data/places.json` used to need a restart."""
    monkeypatch.setattr(catalog_module, "DATA_FILE", write_seed(tmp_path, [one_place]))
    assert len(load_places()) == 1

    second = dict(one_place, id="second-object")
    write_seed(tmp_path, [one_place, second])
    assert len(load_places()) == 1, "the point of a cache is that it does not notice"

    invalidate_cache()
    assert [place.id for place in load_places()] == [one_place["id"], "second-object"]

"""The catalog layer: city spelling, category discovery and the failures that mean HTTP 503."""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

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
from server.routing import haversine_km
from server.schemas import Location, Place

CATALOG_CITY = "Ростов-на-Дону"


def raw_catalog() -> list[dict]:
    """The seed file as written, keys and all.

    Read here rather than through `load_places` on purpose: the models drop what they do not declare,
    so provenance notes and the difference between an absent key and a deliberate `null` are visible
    only in the file itself.
    """
    return json.loads(catalog_module.DATA_FILE.read_text(encoding="utf-8"))


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
    assert all(item.rating <= 5 for item in loaded)


def test_a_visit_is_long_enough_to_be_a_visit_and_short_enough_to_be_walked(places: list[dict]) -> None:
    """20 minutes is the floor the route builder is planned around, not a schema minimum (that is 15).

    Below it a "visit" is a photo stop, and `test_constraints_that_leave_nothing_behind_answer_404`
    stops being a 404 — that test asks for a quarter of an hour and expects nothing to fit.
    """
    durations = [place["visit_duration_minutes"] for place in places]
    assert min(durations) >= 20
    # A route is supposed to hold several objects: one that eats half a day on its own is a destination,
    # not a stop, and it is why the old catalog could only ever offer two points.
    assert max(durations) <= 150
    assert sum(durations) / len(durations) <= 75, (
        "послеобеденная прогулка должна вмещать больше одной точки"
    )


def test_no_category_offers_less_than_a_pair() -> None:
    """The setup screen draws one chip per category, so a category with a single object is a dead end.

    #32 brought the second gallery and the second museum; before it «Галерея» and «Музей» each answered
    every request with exactly one stop, whatever the hours asked for.
    """
    counts = Counter(place["category"] for place in raw_catalog())
    assert min(counts.values()) >= 2, f"одиночная категория в каталоге: {counts}"


def test_the_center_is_dense_enough_for_an_hour_of_walking() -> None:
    """An hour fits two stops only if short visits actually sit next to each other.

    This is the data half of «1 час даёт ≥ 2 точки»; the engine half lives in
    `test_the_requests_the_mini_app_opens_with_are_measured_in_stops`. Half a kilometre is the stretch
    of street an hour of walking spends, and a visit of 25 minutes is what leaves room for the walk
    between two of them.
    """
    places = raw_catalog()
    center = next(p for p in places if p["id"] == "theatre-square")["location"]
    near = [
        p
        for p in places
        if p["id"] != "theatre-square"
        and haversine_km(Location(**center), Location(**p["location"])) <= 0.5
    ]
    assert len(near) >= 8, f"вокруг Театральной площади осталось только {len(near)} объекта"
    assert sum(1 for p in near if p["visit_duration_minutes"] <= 25) >= 3, (
        "рядом нет коротких остановок — час не вмещает две точки"
    )


def test_a_provenance_note_names_a_wikidata_item_and_admits_what_was_not_checked() -> None:
    """Coordinates come out of Wikidata statements, never out of a guess, so the file says which.

    `provenance` is invisible to the API (`Place` ignores what it does not declare): it is the reviewer's
    trail. A place may carry an unverified price or rating while it waits for a source, but never an
    unverified location — the order of a walk depends on it.
    """
    noted = [place for place in raw_catalog() if "provenance" in place]
    assert noted, "новые места обязаны оставлять след источника"
    for place in noted:
        provenance = place["provenance"]
        assert re.fullmatch(r"https://www\.wikidata\.org/wiki/Q\d+", provenance["source_url"])
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", provenance["last_verified"])
        unverified = set(provenance["unverified_fields"])
        assert unverified <= set(place), "в непроверенном списке нет такого поля записи"
        assert {"location", "id", "category"}.isdisjoint(unverified), (
            "координаты, id и категория сверяются всегда"
        )


def test_image_links_are_commons_thumbnails_or_a_deliberate_null() -> None:
    raw = raw_catalog()
    assert all("image_url" in record for record in raw)
    filled = [record["image_url"] for record in raw if record["image_url"]]
    assert len(filled) >= len(raw) - 2, "the demo catalog is meant to be mostly illustrated"
    # Hotlinked thumbnails only: nothing here may turn into a request our server makes at runtime.
    assert all(
        url.startswith("https://") and urlsplit(url).hostname.endswith(".wikimedia.org")
        for url in filled
    )


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
    raw = raw_catalog()
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

"""Unit tests for the collector: classification, ids, ranking and the catalog merge.

No network here — `tests/test_ingest_api.py` drives the pipeline over a fake transport. What is
worth pinning down at this level are the decisions the collector makes about real-world data: which
OSM element becomes which category, and what happens when two of them are the same place.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import server.catalog as catalog_module
from ingest.commons import Picture
from ingest.geo import City
from ingest.normalize import (
    Candidate,
    _rating,
    assign_ids,
    build_candidates,
    dedupe,
    fit_to_publish,
    humanize_hours,
    picture_title,
    select,
    slugify,
    title_key,
)
from ingest.osm import Element, build_query
from ingest.store import city_path, read_city, write_city
from ingest.taxonomy import (
    ARCHITECTURE,
    CAFE,
    INTERESTS,
    MUSEUM,
    PARK,
    category_of,
    worth_showing,
)
from ingest.wikidata import Entity, _file_name, _qid_of
from server.catalog import CatalogError
from server.schemas import Place

CITY = City(
    name="Тестов",
    lat=55.79,
    lon=49.11,
    south=55.6,
    north=55.95,
    west=48.8,
    east=49.4,
    osm_type="relation",
    osm_id=1,
)


def element(
    osm_id: int = 1, name: str = "Музей", *, lat: float = 55.79, lon: float = 49.11, **tags: str
) -> Element:
    return Element(
        osm_type="node",
        osm_id=osm_id,
        lat=lat,
        lon=lon,
        tags={"name": name, **tags},
    )


# --- taxonomy --------------------------------------------------------------


@pytest.mark.parametrize(
    ("tags", "expected"),
    [
        ({"tourism": "museum"}, MUSEUM),
        ({"amenity": "theatre"}, "Театр"),
        ({"leisure": "park"}, PARK),
        ({"amenity": "cafe"}, CAFE),
        ({"historic": "monument"}, "Памятник"),
        ({"shop": "supermarket"}, None),
    ],
)
def test_category_of_maps_osm_tags_to_catalog_categories(tags: dict[str, str], expected: str | None) -> None:
    assert category_of(tags) == expected


def test_a_museum_inside_a_building_is_a_museum_not_a_building() -> None:
    """Declaration order in `INTERESTS` is the tie-break, and this is the case it exists for."""
    assert category_of({"historic": "building", "tourism": "museum"}) == MUSEUM


def test_an_unremarkable_church_is_not_a_destination() -> None:
    parish = element(1, "Церковь Митрофания", amenity="place_of_worship")
    assert category_of(parish.tags) == ARCHITECTURE
    assert worth_showing(ARCHITECTURE, parish.tags) is False

    cathedral = element(2, "Казанский собор", amenity="place_of_worship", wikidata="Q4190492")
    assert worth_showing(ARCHITECTURE, cathedral.tags) is True


def test_a_waterfall_is_not_architecture() -> None:
    """`tourism=attraction` sits on nature too, and «Архитектура» is where a visitor is sent for buildings.

    Real Sochi answers that came out of this rule: 33 водопада and a nudist beach, both ranked as
    architecture because somebody tagged them an attraction. A viewpoint on a mountain keeps its place
    — it arrives as Парк, which is what people walk up it for.
    """
    falls = element(3, "Агура", tourism="attraction", natural="waterfall")
    assert category_of(falls.tags) == ARCHITECTURE
    assert worth_showing(ARCHITECTURE, falls.tags) is False

    beach = element(4, "Нудистский пляж", tourism="attraction", leisure="beach")
    assert worth_showing(ARCHITECTURE, beach.tags) is False

    viewpoint = element(5, "Смотровая площадка", tourism="viewpoint", natural="peak")
    assert category_of(viewpoint.tags) == PARK
    assert worth_showing(PARK, viewpoint.tags) is True


def test_every_interest_carries_chips_tags() -> None:
    """`GET /chips` promises a non-empty count per chip, so an untagged category breaks the client."""
    for interest in INTERESTS:
        assert interest.tags, interest.category
        assert interest.visit_duration_minutes >= 15


def test_query_asks_only_for_named_objects() -> None:
    query = build_query(INTERESTS[0].clauses, "1,2,3,4")
    assert '["name"]' in query
    assert "(1,2,3,4)" in query
    assert "out tags center" in query


# --- hours, ids, ranking ---------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("24/7", "круглосуточно"),
        ("Mo-Fr 09:00-18:00", "пн–пт 09:00–18:00"),
        ("Mo-Fr 09:00-18:00; Sa 10:00-14:00", "пн–пт 09:00–18:00, сб 10:00–14:00"),
        ("Mo,We,Fr 10:00-18:00", "пн,ср,пт 10:00–18:00"),
        # Anything the renderer would have to guess at is dropped rather than paraphrased: a card
        # that says «пн» under a rule that said «Sa[-1] off» is a wrong card.
        ("Sa[-1] off", None),
        ("", None),
        (None, None),
    ],
)
def test_humanize_hours(raw: str | None, expected: str | None) -> None:
    assert humanize_hours(raw) == expected


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Казанский кремль", "kazanskiy-kreml"),
        ("Дом-музей Аксёнова", "dom-muzey-aksyonova"),
        ("Центр «Эрмитаж-Казань»", "tsentr-ermitazh-kazan"),
        ("", "place"),
    ],
)
def test_slugify(title: str, expected: str) -> None:
    assert slugify(title) == expected


def _candidate(name: str, *, osm_id: int = 1, **tags: str) -> Candidate:
    element_ = element(osm_id, name, **tags)
    return Candidate(
        place=Place(
            id="pending",
            title=name,
            category=MUSEUM,
            location={"lat": 55.79, "lon": 49.11},
        ),
        provenance={},
        score=1.0,
        element=element_,
    )


def test_ids_are_unique_and_do_not_depend_on_fetch_order() -> None:
    """The slug is what `data/place_guides.json` references, so a re-run must not move it."""
    first = assign_ids([_candidate("Музей"), _candidate("Музей", osm_id=2)])
    second = assign_ids([_candidate("Музей", osm_id=2), _candidate("Музей")])
    assert [place.place.id for place in first] == ["muzey", "muzey-2"]
    assert [place.place.id for place in first] == [place.place.id for place in second]


def test_the_same_object_mapped_twice_becomes_one_place() -> None:
    """OSM carries a node and a way for plenty of museums; the catalog shows one of them."""
    duplicated = [
        _candidate("Музей", osm_id=1, wikidata="Q100"),
        _candidate("Музей", osm_id=2, wikidata="Q100"),
    ]
    for candidate in duplicated:
        candidate.score = 2.0 if candidate.element.osm_id == 2 else 1.0
    kept = dedupe(duplicated)
    assert [candidate.element.osm_id for candidate in kept] == [2]


def test_title_key_treats_quoting_and_case_as_noise() -> None:
    assert title_key("«Дом Врангеля»") == title_key("Дом  врангеля") == "дом врангеля"


def test_a_place_with_neither_a_picture_nor_a_sentence_is_not_published() -> None:
    """The card the visitor opens at a stop has to have something in it.

    Measured on the Rostov pass: 147 objects found, 2 able to fill a card. Publishing the other 145
    turns a walk into a series of empty screens, which is worse than a shorter list.
    """
    bare = element(1, "Кофейня у вокзала", amenity="cafe")
    described = element(2, "Дом Врангеля", historic="building", wikidata="Q1")
    entity = Entity(qid="Q1", description="доходный дом на Большой Садовой")
    candidates = build_candidates(CITY, {CAFE: (bare,), ARCHITECTURE: (described,)}, {entity.qid: entity}, {})

    kept = fit_to_publish(candidates)

    assert [candidate.place.title for candidate in kept] == ["Дом Врангеля"]
    dropped = [(c.place.title, c.dropped_reason) for c in candidates if id(c) not in map(id, kept)]
    assert dropped == [("Кофейня у вокзала", "no picture and no description")]


def test_a_picture_alone_is_enough_to_publish() -> None:
    """Not every honest card needs prose: a photographed bridge tells the visitor where they are."""
    park = element(3, "Ворошиловский мост", leisure="park", wikidata="Q2")
    entity = Entity(qid="Q2", image="File:Voroshilovsky bridge.jpg")
    pictures = {entity.image: Picture(url="https://upload.wikimedia.org/x.jpg")}
    candidates = build_candidates(CITY, {PARK: (park,)}, {entity.qid: entity}, pictures)

    assert [candidate.place.title for candidate in fit_to_publish(candidates)] == [
        "Ворошиловский мост"
    ]


def test_a_place_the_seed_already_carries_is_not_published_a_second_time() -> None:
    """`dedupe` cannot catch this one: the seed is not among the objects of this run.

    The ids differ because the seed slug is hand-written and the fetched one is derived, so nothing
    else stops the same building standing on the map twice, one metre from its own copy.
    """
    house = element(4, "Дом Врангеля", historic="building", wikidata="Q3")
    entity = Entity(qid="Q3", description="особняк XIX века")
    candidates = build_candidates(CITY, {ARCHITECTURE: (house,)}, {entity.qid: entity}, {})

    kept = fit_to_publish(candidates, known_titles=frozenset({title_key("«Дом Врангеля»")}))

    assert kept == []
    assert candidates[0].dropped_reason == "already in the seed"


def test_per_category_cap_lets_a_small_category_survive() -> None:
    """30 monuments and one theatre must not come out as 10 monuments.

    The cap is applied per category before the total one: a plain global top-N on real city data
    returns whatever OpenStreetMap has most of, which is never what a visitor came for.
    """
    monuments = tuple(element(index, f"Памятник {index}", historic="monument") for index in range(30))
    theatre = (element(90, "Татарский театр", amenity="theatre"),)
    candidates = build_candidates(CITY, {"Памятник": monuments, "Театр": theatre}, {}, {})
    chosen = select(candidates, INTERESTS, total_limit=10, per_category=3)

    categories = [place.place.category for place in chosen]
    assert len(chosen) == 4
    assert categories.count("Театр") == 1
    assert categories.count("Памятник") == 3


def test_the_total_limit_does_not_empty_the_least_attested_category() -> None:
    """Кафе loses every global slot it is cut globally: cafés have no Wikidata item and no photo.

    The live proof was Kazan after the mood balance landed: the category cap gave café twelve places,
    the city limit of 60 then sorted everything by attestation and left one coffee house in the file.
    The total is now dealt out one object per category in turn, so the filters the client offers always
    have something behind them.
    """
    monuments = tuple(
        element(
            index,
            f"Памятник {index}",
            historic="monument",
            wikidata=f"Q{100 + index}",
            opening_hours="24/7",
        )
        for index in range(20)
    )
    cafes = tuple(
        element(200 + index, f"Кофейня {index}", amenity="cafe", lat=55.79 + 0.001 * index)
        for index in range(20)
    )
    candidates = build_candidates(CITY, {"Памятник": monuments, CAFE: cafes}, {}, {})
    chosen = select(candidates, INTERESTS, total_limit=10, per_category=8)

    categories = [candidate.place.category for candidate in chosen]
    assert len(chosen) == 10
    assert categories.count(CAFE) == 5, categories


def test_one_chain_branch_per_brand() -> None:
    elements = tuple(
        element(index, f"Кофейня «Анни» {index}", amenity="cafe", brand="Анни")
        for index in range(5)
    )
    candidates = build_candidates(CITY, {CAFE: elements}, {}, {})
    chosen = select(candidates, INTERESTS, total_limit=20)
    assert len(chosen) == 1


def test_both_cafe_moods_survive_the_category_cap() -> None:
    """A city whose twelve café slots are all restaurants has no «Взять кофе» left.

    Ranked by attestation alone, the restaurants win: they carry opening hours and websites, the
    coffee houses do not. Kazan came out that way — one place behind the coffee chip, so the chip's
    route was a single stop. The cap now fills alternately from the two mood groups.
    """
    restaurants = tuple(
        element(
            index,
            f"Ресторан {index}",
            amenity="restaurant",
            website=f"https://rest{index}.ru",
            opening_hours="Mo-Fr 12:00-23:00",
        )
        for index in range(6)
    )
    coffee = tuple(
        element(100 + index, f"Кофейня {index}", amenity="cafe") for index in range(6)
    )
    candidates = build_candidates(CITY, {CAFE: restaurants + coffee}, {}, {})
    chosen = select(candidates, INTERESTS, total_limit=20, per_category=4)

    tags = [candidate.place.tags[0] for candidate in chosen]
    assert len(chosen) == 4
    assert tags.count("coffee") == 2, tags
    assert tags.count("food") == 2, tags


def test_objects_outside_the_core_are_not_published() -> None:
    """The bounding box is a rectangle, so a long city's box reaches places nobody walks to.

    Sochi's answer included a waterfall 31 km and a viaduct 48 km from the centre. `rating` and the
    per-category ranking were already pushing them down; a catalog that still lists them hands the
    route planner stops whose first hop is an hour and a half.
    """
    nearby = element(1, "Близкое место", historic="monument", lat=55.80, lon=49.12)
    distant = element(2, "Далёкое место", historic="monument", lat=56.05, lon=49.11)

    clipped = build_candidates(CITY, {"Памятник": (nearby, distant)}, {}, {}, core_radius_km=25)
    assert [candidate.place.title for candidate in clipped] == ["Близкое место"]
    unclipped = build_candidates(CITY, {"Памятник": (nearby, distant)}, {}, {})
    assert len(unclipped) == 2


def test_a_stop_is_budgeted_for_the_walk_not_for_the_collection() -> None:
    """90 minutes for a museum turned a two-hour request into a one-stop answer.

    OSM does not know how long a visit takes, and the number the planner budgets against is ours. It
    is written as the time a self-guided walk spends at a door, because the alternative — the 2-3
    hours a museum's own site recommends — is a different product: the record still says plainly in
    `unverified_fields` that the duration was inferred.
    """
    candidates = build_candidates(
        CITY,
        {
            MUSEUM: (element(1, "Музей", tourism="museum"),),
            CAFE: (element(2, "Кафе", amenity="cafe"),),
            ARCHITECTURE: (element(3, "Башня", historic="tower"),),
        },
        {},
        {},
    )
    durations = {candidate.place.category: candidate.place.visit_duration_minutes for candidate in candidates}

    assert durations[MUSEUM] == 45
    assert durations[CAFE] == 30
    assert durations[ARCHITECTURE] == 20
    # A two-hour request has to be able to hold three of them plus the walking between.
    assert sum(sorted(durations.values())) <= 120


# --- storage and the catalog merge -----------------------------------------


def test_write_city_is_a_round_trip(tmp_path: Path) -> None:
    records = [{"id": "a", "title": "A"}]
    path = write_city(tmp_path, "Тестов", records)
    assert path == city_path(tmp_path, "Тестов")
    assert path.name == "testov.json"
    assert read_city(path) == records
    assert not list(tmp_path.glob("*.tmp*")), "the temporary file must not outlive the write"


def test_a_broken_generated_file_is_reported_not_served(tmp_path: Path, monkeypatch, one_place) -> None:
    seed = tmp_path / "places.json"
    seed.write_text(json.dumps([one_place], ensure_ascii=False), encoding="utf-8")
    generated = tmp_path / "places.d"
    generated.mkdir()
    (generated / "kazan.json").write_text("{ this is not json", encoding="utf-8")
    monkeypatch.setattr(catalog_module, "DATA_FILE", seed)
    monkeypatch.setattr(catalog_module, "EXTRA_DIR", generated)
    catalog_module.invalidate_cache()

    with pytest.raises(CatalogError, match="kazan.json"):
        catalog_module.load_places()


def test_generated_cities_join_the_seed_and_never_shadow_it(
    tmp_path: Path, monkeypatch, one_place
) -> None:
    """Both sources are one catalog, and the hand-checked record wins an id collision."""
    seed = tmp_path / "places.json"
    seed.write_text(json.dumps([one_place], ensure_ascii=False), encoding="utf-8")
    generated = tmp_path / "places.d"
    generated.mkdir()
    extra = {**one_place, "id": "other", "city": "Казань", "title": "Другое место"}
    duplicate = {**one_place, "title": "Поддельная Театральная площадь"}
    (generated / "kazan.json").write_text(
        json.dumps([extra, duplicate], ensure_ascii=False), encoding="utf-8"
    )
    monkeypatch.setattr(catalog_module, "DATA_FILE", seed)
    monkeypatch.setattr(catalog_module, "EXTRA_DIR", generated)
    catalog_module.invalidate_cache()

    places = catalog_module.load_places()
    assert [place.id for place in places] == [one_place["id"], "other"]
    assert places[0].title == one_place["title"]
    assert catalog_module.known_cities()["Казань"][0].id == "other"


# --- wikidata parsing ------------------------------------------------------


@pytest.mark.parametrize(
    "reference",
    ["http://www.wikidata.org/entity/Q4315032", "Q4315032"],
)
def test_qid_of_accepts_both_shapes_that_appear(reference: str) -> None:
    """The full URI is what SPARQL binds; the bare id is what the OSM tag carries."""
    assert _qid_of(reference) == "Q4315032"


def test_qid_of_rejects_a_non_entity() -> None:
    assert _qid_of("http://www.wikidata.org/prop/direct/P18") == ""


@pytest.mark.parametrize(
    ("reference", "expected"),
    [
        # Тем, что SPARQL отвечает сегодня: редирект-форма с закодированным именем файла.
        (
            "http://commons.wikimedia.org/wiki/Special:FilePath/Kazan%20Kremlin.jpg",
            "File:Kazan Kremlin.jpg",
        ),
        ("https://commons.wikimedia.org/wiki/File:Kazan_Kremlin.jpg", "File:Kazan Kremlin.jpg"),
        ("File:Храм.jpg", "File:Храм.jpg"),
    ],
)
def test_file_name_accepts_every_spelling_of_a_commons_file(reference: str, expected: str) -> None:
    """Ключ, по которому ищется картинка, — заголовок Commons с пробелами: их возвращает сам API."""
    assert _file_name(reference) == expected


def test_file_name_gives_up_on_an_answer_without_a_file() -> None:
    assert _file_name("http://www.wikidata.org/entity/Q4315032") is None
    assert _file_name(None) is None


def test_build_candidates_prefers_the_russian_name_and_the_wiki_description() -> None:
    museum = element(7, "Национальный музей РТ", tourism="museum", wikidata="Q4315032")
    entity = Entity(qid="Q4315032", description="музей в Татарстане", has_ru_article=True)
    candidate = build_candidates(CITY, {MUSEUM: (museum,)}, {entity.qid: entity}, {})[0]

    assert candidate.place.title == "Национальный музей РТ"
    assert candidate.place.description == "музей в Татарстане."
    assert candidate.place.city == "Тестов"
    assert candidate.provenance["source_url"].endswith("Q4315032")
    assert "rating" in candidate.provenance["unverified_fields"]
    # An element with no opening hours and no price cannot claim either: both stay listed.
    assert {"price", "working_hours"} <= set(candidate.provenance["unverified_fields"])


def test_a_well_attested_place_outranks_a_bare_one() -> None:
    bare = element(1, "Сквер", leisure="park")
    famous = element(2, "Кремль", leisure="park", wikidata="Q270149", opening_hours="24/7")
    entity = Entity(qid="Q270149", has_ru_article=True)
    candidates = {candidate.element.osm_id: candidate for candidate in build_candidates(
        CITY, {PARK: (bare, famous)}, {entity.qid: entity}, {}
    )}

    assert candidates[2].score > candidates[1].score
    assert candidates[2].place.rating > candidates[1].place.rating
    assert candidates[1].place.working_hours is None
    assert candidates[2].place.working_hours == "круглосуточно"


def test_ratings_spread_over_the_seed_band_instead_of_parking_on_the_ceiling() -> None:
    """Половина каталога с одной и той же оценкой обесценивает фильтр `min_rating`.

    Проверено на живой Казани: при делителе 4 объект с статьёй в ру-Вики (2.5 балла) уже упирался
    в потолок, и все тридцать мест выходили ровно 4.8.
    """
    assert _rating(-1.0) == 4.2 and _rating(0.0) == 4.2
    assert _rating(2.5) == 4.5, "элемент Викиданных плюс статья в Вики — середина полосы"
    assert _rating(4.0) == 4.7
    assert _rating(7.0) == 4.8 and _rating(20.0) == 4.8, "4.8 — потолок, а не средняя"


def test_an_osm_image_tag_is_looked_up_by_the_title_commons_returns() -> None:
    """`image=Kazan_Kremlin.jpg` в OSM и `File:Kazan Kremlin.jpg` в ответе API — один и тот же файл."""
    assert picture_title({"image": "Kazan_Kremlin.jpg"}, None) == "File:Kazan Kremlin.jpg"
    assert picture_title({"wikimedia_commons": "File:Храм.jpg"}, None) == "File:Храм.jpg"


def test_places_built_by_the_collector_validate_as_api_records() -> None:
    """The file the collector writes is the file the API serves — `Place` is the contract."""
    candidate = build_candidates(
        CITY, {MUSEUM: (element(3, "Музей", tourism="museum"),)}, {}, {}
    )[0]
    assert Place.model_validate(candidate.place.model_dump()) == candidate.place

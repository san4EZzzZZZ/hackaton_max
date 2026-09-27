"""The guide layer: the editor file's own integrity rules, the failures that mean 503, the neighbours.

The schema keeps the loader honest; these tests keep the *shipped* file honest. Two kinds of mistake
matter here and neither one raises at write time: a guide that points at a place the catalog does not
have, and an attribution that is a guess rather than what the Commons page of that file says.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlsplit

import pytest

import server.guides as guides_module
from conftest import guide_record, write_guides
from server.guides import GuideError, guide_for, load_guides, nearby_for
from server.routing import haversine_km, travel_minutes
from server.schemas import GuideRecord, Location, Place

# A guide is read standing at the object, not lying on a sofa: the rails below are the lengths that
# survive on one screen. The schema floors them (120 characters of history, 12 of a fact); this is
# where the ceiling lives, because nothing in Pydantic will notice a 2000-character "paragraph".
MAX_HIGHLIGHT_CHARS = 90
MAX_PARAGRAPH_CHARS = 700
MAX_CAPTION_CHARS = 90


def raw_guides() -> list[dict]:
    """The seed file as written, `provenance` and all.

    `GuideRecord` drops what it does not declare, so the difference between a field the editor left
    out and a field deliberately set to `null` is visible only in the file.
    """
    return json.loads(guides_module.DATA_FILE.read_text(encoding="utf-8"))


def place_by_id(places: list[dict]) -> dict[str, dict]:
    return {place["id"]: place for place in places}


def test_the_shipped_file_holds_a_guide_per_written_place() -> None:
    guides = load_guides()
    assert len(guides) == 8 == len(raw_guides())
    assert {type(item) for item in guides} == {GuideRecord}
    # The seed ships no stubs: a `seed` record is allowed by the contract, but nothing in the file
    # would yet explain to the reader that the text below is only a beginning.
    assert {guide.status for guide in guides} == {"ready"}


def test_every_guide_belongs_to_a_place_the_catalog_actually_has(places: list[dict]) -> None:
    guides = load_guides()
    known = {place["id"] for place in places}
    assert {guide.place_id for guide in guides} <= known
    assert len({guide.place_id for guide in guides}) == len(guides), "one guide per place"


def test_the_cover_of_a_guide_is_the_picture_the_client_already_loaded(places: list[dict]) -> None:
    """`GuideSummary.cover_url` is `place.image_url`; the first media entry has to be the same pixel.

    Otherwise the badge and the screen it opens show two different buildings, which is the kind of
    thing a reader notices only after they have walked there.
    """
    catalog = place_by_id(places)
    for guide in load_guides():
        assert guide.media, "the shipped guides are illustrated"
        assert guide.media[0].thumb_url == catalog[guide.place_id]["image_url"]


def test_original_and_thumbnail_of_a_shot_always_name_the_same_file() -> None:
    """`url` is the Commons file, `thumb_url` the served resize of exactly that file.

    Deriving one from the other is a string edit, which is where a hand-typed pair drifts: the reader
    then follows "open the original" into a different photograph.
    """
    for guide in load_guides():
        for media in guide.media:
            original = urlsplit(media.url).path.rsplit("/", 1)[-1]
            served = urlsplit(media.thumb_url).path.rsplit("/", 1)[-1]
            assert (
                media.url == media.thumb_url
                or served == original
                or served.replace("%20", "_") == original
                or served.split("-", 1)[-1] == original
            ), (guide.place_id, media.url, media.thumb_url)


def test_provenance_of_a_ready_guide_names_the_article_it_came_from() -> None:
    for guide in load_guides():
        provenance = guide.provenance
        assert provenance.source_url, guide.place_id
        assert provenance.last_verified, guide.place_id
        assert provenance.source_url.startswith("https://ru.wikipedia.org/wiki/")
        assert set(provenance.verified_fields) == {"history", "highlights", "media"}, (
            "текст, факты и подписи сверяются раздельно"
        )


def test_attribution_is_carried_for_every_shot() -> None:
    """A CC image without a credit line is a licence violation the app would be serving.

    The license page is the one thing the file itself can be silent about: public domain has none.
    """
    for guide in load_guides():
        for media in guide.media:
            assert media.credit.strip() and media.license.strip(), guide.place_id
            if media.license.strip().lower().startswith("public domain"):
                assert media.license_url is None, "у public domain страницы лицензии нет"
            else:
                assert media.license_url.startswith("https://"), (guide.place_id, media.license)


def test_the_text_is_short_enough_to_read_standing_at_the_object() -> None:
    for guide in load_guides():
        for paragraph in guide.history:
            assert len(paragraph) <= MAX_PARAGRAPH_CHARS, guide.place_id
        for highlight in guide.highlights:
            assert len(highlight) <= MAX_HIGHLIGHT_CHARS, (guide.place_id, highlight)
        for media in guide.media:
            assert len(media.caption) <= MAX_CAPTION_CHARS, (guide.place_id, media.caption)


def test_the_seed_file_is_the_shape_the_contract_describes() -> None:
    raw = raw_guides()
    assert isinstance(raw, list) and raw
    assert {"place_id", "status", "history", "highlights", "media", "provenance"} <= set(raw[0])


def test_guide_for_hands_back_the_stored_half_only() -> None:
    guide = guide_for("theatre-square")
    assert guide is not None
    assert guide.place_id == "theatre-square"
    assert not hasattr(guide, "title"), "the title belongs to the catalog, not to the editor"
    assert guide_for("no-such-place") is None


def test_an_empty_file_is_a_valid_empty_index(tmp_path: Path, monkeypatch) -> None:
    write_guides(monkeypatch, tmp_path)
    assert load_guides() == ()
    assert guide_for("theatre-square") is None


def test_a_missing_file_is_a_guide_error(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(guides_module, "DATA_FILE", tmp_path / "absent.json")
    with pytest.raises(GuideError, match="not found"):
        load_guides()


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        ("не json при всём желании", "Cannot read"),
        ('{"place_id": "theatre-square"}', "Invalid guide"),
        ("42", "a list is expected"),
        ("null", "a list is expected"),
    ],
)
def test_an_unusable_file_is_a_guide_error(
    tmp_path: Path, monkeypatch, content: str, reason: str
) -> None:
    broken = tmp_path / "place_guides.json"
    broken.write_text(content, encoding="utf-8")
    monkeypatch.setattr(guides_module, "DATA_FILE", broken)
    with pytest.raises(GuideError, match=reason):
        load_guides()


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("history", ["Первый абзац. " * 12], "at least 2 items"),
        (
            "history",
            ["Первый абзац. " * 12, "Слишком короткий абзац истории справочного текста."],
            "at least 120 characters",
        ),
        ("highlights", ["!", "!", "!", "!"], "at least 12 characters"),
        ("status", "draft", "Input should be"),
        (
            "media",
            [
                {
                    "url": "https://example.com/a.jpg",
                    "thumb_url": "https://example.com/a.jpg",
                    "caption": "Снимок",
                    "credit": "Автор",
                    "license": "CC BY 4.0",
                    "license_url": "https://creativecommons.org/licenses/by/4.0",
                }
            ],
            "wikimedia",
        ),
        (
            "media",
            [
                {
                    "url": "http://upload.wikimedia.org/wikipedia/commons/1/13/a.jpg",
                    "thumb_url": "http://upload.wikimedia.org/wikipedia/commons/1/13/a.jpg",
                    "caption": "Снимок",
                    "credit": "Автор",
                    "license": "CC BY 4.0",
                    "license_url": "https://creativecommons.org/licenses/by/4.0",
                }
            ],
            "wikimedia",
        ),
        (
            "media",
            [
                {
                    "url": "https://upload.wikimedia.org/wikipedia/commons/1/13/a.jpg",
                    "thumb_url": "https://upload.wikimedia.org/wikipedia/commons/1/13/a.jpg",
                    "caption": "Снимок",
                    "credit": "Автор",
                    "license": "CC BY 4.0",
                    "license_url": None,
                }
            ],
            "обязан быть адрес",
        ),
        ("media_note", None, "media_note"),
        ("provenance", {"source_url": None, "last_verified": "2026-09-27", "verified_fields": []},
         "last_verified без source_url"),
        ("provenance", {"source_url": "https://ru.wikipedia.org/wiki/Тест", "last_verified": None,
                        "verified_fields": ["description"]}, "verified_fields"),
    ],
    ids=[
        "один абзац",
        "абзац короче 120",
        "факт короче 12",
        "статус не из списка",
        "чужой хост",
        "http вместо https",
        "лицензия без страницы",
        "пустой media без media_note",
        "дата без источника",
        "поле не записи",
    ],
)
def test_one_broken_rule_names_itself_in_the_error(
    tmp_path: Path, monkeypatch, field: str, value: object, reason: str
) -> None:
    """Each rule of the contract is checked on its own, so `detail` can be read as advice.

    A loader that only says "invalid" would leave the editor opening the file and hunting for the
    line, which is the reason 503 carries the message instead of a bare status.
    """
    write_guides(monkeypatch, tmp_path, guide_record(**{field: value}))
    with pytest.raises(GuideError, match=reason):
        load_guides()


def test_public_domain_may_be_attributed_without_a_license_page(
    tmp_path: Path, monkeypatch
) -> None:
    media = {
        "url": "https://upload.wikimedia.org/wikipedia/commons/5/56/a.jpg",
        "thumb_url": "https://upload.wikimedia.org/wikipedia/commons/5/56/a.jpg",
        "caption": "Открытка 1909 года",
        "credit": "Неизвестный автор",
        "license": "Public domain",
        "license_url": None,
    }
    write_guides(monkeypatch, tmp_path, guide_record(media=[media], media_note=None))
    loaded = load_guides()
    assert loaded[0].media[0].license == "Public domain"
    assert loaded[0].media_note is None, "раз фото есть, объяснять отсутствие нечем"


def test_two_guides_for_one_place_is_a_broken_file(tmp_path: Path, monkeypatch) -> None:
    write_guides(monkeypatch, tmp_path, guide_record(), guide_record())
    with pytest.raises(GuideError, match="Duplicate guide"):
        load_guides()


def test_an_edited_file_is_served_only_after_the_cache_drops(
    tmp_path: Path, monkeypatch
) -> None:
    """`load_guides` is memoized, so the same restart rule the catalog had applies here.

    `write_guides` drops the cache on purpose, which is what the middle of this test must not do.
    """
    path = tmp_path / "place_guides.json"
    monkeypatch.setattr(guides_module, "DATA_FILE", path)

    def write(*records: dict) -> None:
        path.write_text(json.dumps(list(records), ensure_ascii=False), encoding="utf-8")

    write(guide_record("theatre-square"))
    assert [guide.place_id for guide in load_guides()] == ["theatre-square"]

    write(guide_record("theatre-square"), guide_record("martyn-house"))
    assert len(load_guides()) == 1, "the point of a cache is that it does not notice"

    guides_module.invalidate_cache()
    assert [guide.place_id for guide in load_guides()] == ["theatre-square", "martyn-house"]


def synthetic_place(place_id: str, lat: float, lon: float) -> Place:
    return Place(
        id=place_id,
        title=place_id,
        category="Памятник",
        location=Location(lat=lat, lon=lon),
    )


ORIGIN = 47.2092, 39.7090  # Театральная площадь, the densest corner of the catalog


def test_nearby_orders_by_distance_and_stops_at_the_limit() -> None:
    origin = synthetic_place("origin", *ORIGIN)
    others = [synthetic_place(f"p{index}", ORIGIN[0] + index / 1000, ORIGIN[1]) for index in range(1, 6)]
    nearby = nearby_for(origin, [origin, *others])
    assert [item.id for item in nearby] == ["p1", "p2", "p3", "p4"]
    assert len(nearby) == guides_module.NEARBY_LIMIT
    assert nearby == nearby_for(origin, [origin, *reversed(others)]), "input order cannot matter"


def test_nearby_skips_the_place_itself_and_any_twin_of_its_coordinates() -> None:
    origin = synthetic_place("origin", *ORIGIN)
    twin = synthetic_place("same-coordinates", *ORIGIN)
    nearby = nearby_for(origin, [origin, twin], limit=4)
    assert nearby == [], "соседом называют другое место, а не ту же точку"


def test_an_exact_tie_is_broken_by_id_so_the_screen_never_reshuffles() -> None:
    origin = synthetic_place("origin", *ORIGIN)
    east = synthetic_place("b-east", ORIGIN[0], ORIGIN[1] + 0.01)
    west = synthetic_place("a-west", ORIGIN[0], ORIGIN[1] - 0.01)
    assert haversine_km(origin.location, east.location) == pytest.approx(
        haversine_km(origin.location, west.location)
    )
    assert [item.id for item in nearby_for(origin, [origin, east, west])] == ["a-west", "b-east"]


def test_nearby_minutes_are_the_same_number_the_route_engine_would_charge(
    places: list[dict]
) -> None:
    """The one rule that makes two screens agree, asserted against `server.routing` itself.

    A guide that promised «5 минут» and a route that billed 8 for the same pavement would be a bug a
    reader walks into, so this compares against the planner's own function rather than a copy of it.
    """
    catalog = [Place.model_validate(place) for place in places]
    for origin in catalog[:8]:
        for neighbour in nearby_for(origin, catalog):
            target = next(place for place in catalog if place.id == neighbour.id)
            assert neighbour.travel_minutes == travel_minutes(origin.location, target.location)
            assert neighbour.distance_m == round(
                haversine_km(origin.location, target.location) * 1000
            )
            assert neighbour.category == target.category and neighbour.title == target.title


def test_nearby_minutes_never_run_backwards(places: list[dict]) -> None:
    """Minutes round, metres do not: a farther neighbour can match the walk time but never beat it."""
    catalog = [Place.model_validate(place) for place in places]
    for origin in catalog[:8]:
        nearby = nearby_for(origin, catalog)
        assert [item.distance_m for item in nearby] == sorted(item.distance_m for item in nearby)
        assert [item.travel_minutes for item in nearby] == sorted(
            item.travel_minutes for item in nearby
        )

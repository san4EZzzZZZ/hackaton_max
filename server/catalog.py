"""Places catalog: loads the seed file plus the generated cities and serves them to the API layer."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable, Sequence
from functools import lru_cache
from pathlib import Path
from typing import Any, NamedTuple

from pydantic import ValidationError

from server.schemas import Chip, Place

logger = logging.getLogger(__name__)

DATA_FILE = Path(__file__).resolve().parent.parent / "data" / "places.json"
#: Cities written by `python -m ingest` (see `ingest/store.py`). Read exactly like the seed and
#: deliberately not committed: they are a local copy of somebody else's data, refreshed by a run.
EXTRA_DIR = DATA_FILE.parent / "places.d"

_TOKEN_SPLIT = re.compile(r"[^0-9a-zа-яё]+")
# Words that carry no identity in a city name, and the case endings people type interchangeably.
_CITY_NOISE = {"на", "в", "над", "город"}
_CITY_SYNONYMS = {"дона": "дону", "донец": "дону"}


def _city_tokens(value: str) -> list[str]:
    tokens = [token for token in _TOKEN_SPLIT.split(value.lower()) if token]
    return [
        _CITY_SYNONYMS.get(token, token) for token in tokens if token not in _CITY_NOISE
    ]


def city_matches(query: str, place_city: str) -> bool:
    """Match a user-typed city against a catalog city, tolerating case and spelling variants.

    "Ростов", "ростов-на-Дону", "Ростов на Дону" and "Ростов-на-Дона" all resolve to the same
    settlement: the typed words have to be a prefix of the catalog name's significant words.
    """
    wanted, actual = _city_tokens(query), _city_tokens(place_city)
    return bool(wanted) and actual[: len(wanted)] == wanted


class CatalogError(RuntimeError):
    """Raised when the seed file is missing or malformed — surfaced as HTTP 503."""


def _entries(path: Path) -> list[Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CatalogError(f"Cannot read {path}: {error}") from error
    if not isinstance(raw, list):
        # A bare `{}` or `42` would otherwise reach the endpoints as a 500 instead of the 503 that
        # says the catalog is down: iterating a dict walks its keys, and each key fails validation.
        raise CatalogError(f"Invalid catalog {path.name}: a list is expected, got {type(raw).__name__}")
    return raw


def data_files() -> list[Path]:
    """The seed first, then every generated city in name order.

    The order is the merge rule: a colliding id keeps whichever record was read first, and the seed
    is always read first, so a hand-checked object can never be shadowed by a fetched one.
    """
    files = [DATA_FILE]
    if EXTRA_DIR.is_dir():
        files.extend(sorted((path for path in EXTRA_DIR.glob("*.json") if path.is_file())))
    return files


def _validated(path: Path) -> list[Place]:
    """Every record of one catalog file, refused as a whole if any of them fails to parse."""
    places: list[Place] = []
    for index, entry in enumerate(_entries(path)):
        try:
            places.append(Place.model_validate(entry))
        except ValidationError as error:
            raise CatalogError(f"Invalid place #{index} in {path.name}: {error}") from error
    return places


@lru_cache(maxsize=1)
def load_seed() -> tuple[Place, ...]:
    """The hand-checked seed on its own, without the generated cities.

    `python -m ingest` compares a fresh city against this file rather than against `load_places`: the
    generated file the run is about to overwrite is itself part of that catalog, so measured against
    the whole, the second run of a city would find everything the first one published already taken
    and publish nothing at all.
    """
    if not DATA_FILE.is_file():
        raise CatalogError(f"Places catalog not found at {DATA_FILE}")
    return tuple(_validated(DATA_FILE))


@lru_cache(maxsize=1)
def load_places() -> tuple[Place, ...]:
    if not DATA_FILE.is_file():
        raise CatalogError(f"Places catalog not found at {DATA_FILE}")

    places: list[Place] = []
    seen: set[str] = set()
    paths = data_files()
    for path in paths:
        for place in _validated(path):
            if place.id in seen:
                logger.warning("Skipping %r in %s: the id is already in the catalog", place.id, path.name)
                continue
            seen.add(place.id)
            places.append(place)

    logger.info("Loaded %d places from %d file(s)", len(places), len(paths))
    return tuple(places)


def invalidate_cache() -> None:
    """Forget the memoized catalog so an edited `data/places.json` is served without a restart."""
    load_places.cache_clear()


def known_categories() -> tuple[str, ...]:
    """Categories the seed data actually contains, in catalog order."""
    return tuple(dict.fromkeys(place.category for place in load_places()))


class ChipSpec(NamedTuple):
    """One mood chip of the setup screen: what it is called and which tags answer it."""

    id: str
    label: str
    emoji: str
    tags: tuple[str, ...]


# Порядок объявления — это порядок чипов на экране. `tags` списком, а не строкой: «Культура» завтра
# сможет выбрать ещё и `photo`, и контракт менять не придётся.
CHIPS: tuple[ChipSpec, ...] = (
    ChipSpec(id="coffee", label="Взять кофе", emoji="☕", tags=("coffee",)),
    ChipSpec(id="culture", label="Культура", emoji="🏛", tags=("culture",)),
    ChipSpec(id="walk", label="Погулять в парке", emoji="🌳", tags=("walk",)),
    ChipSpec(id="food", label="Перекусить", emoji="🍕", tags=("food",)),
    ChipSpec(id="photo", label="Красивые фото", emoji="📸", tags=("photo",)),
)


def normalize_tags(values: Iterable[str]) -> set[str]:
    """Tags as the planner compares them: trimmed, lowercased, blanks gone."""
    return {value.strip().lower() for value in values if value.strip()}


def has_any_tag(place: Place, wanted: set[str]) -> bool:
    """Whether a place answers a set of requested tags — ИЛИ внутри набора, пустой набор не фильтрует.

    Одна функция на `/chips` и на `filter_candidates`: счётчик чипа и реальный подбор не могут
    разойтись, как не разошлись минуты перехода в справке.
    """
    return not wanted or bool(wanted & normalize_tags(place.tags))


def chip_summaries(places: Sequence[Place]) -> tuple[Chip, ...]:
    """Чипы, которые данным каталога есть чем наполнить, в порядке объявления.

    Spec без хоть одного подходящего места выбрасывается, а не возвращается с нулём: тогда клиент
    рисует ответ как есть и не держит у себя список «настоящих» чипов — именно из-за такого списка
    экран настройки обещал кофе там, где его искать было нечего.
    """
    chips: list[Chip] = []
    for spec in CHIPS:
        wanted = normalize_tags(spec.tags)
        count = sum(1 for place in places if has_any_tag(place, wanted))
        if count:
            chips.append(
                Chip(
                    id=spec.id,
                    label=spec.label,
                    emoji=spec.emoji,
                    tags=list(spec.tags),
                    place_count=count,
                )
            )
    return tuple(chips)


def known_cities() -> dict[str, list[Place]]:
    """Cities present in the data, mapped to their objects, in order of first appearance."""
    grouped: dict[str, list[Place]] = {}
    for place in load_places():
        grouped.setdefault(place.city, []).append(place)
    return grouped


def _search_blob(place: Place) -> str:
    return " ".join(
        text
        for text in (place.title, place.description, place.address, place.category)
        if text
    ).lower()


def search_matches(place: Place, query: str) -> bool:
    """Free-text match over the fields a visitor actually reads.

    Every word of the query has to appear somewhere in the object, so «девушка кувшин» narrows down to
    one place while «фонтан» alone still finds Театральная площадь — the word is only in the description.
    """
    tokens = [token for token in _TOKEN_SPLIT.split(query.lower()) if token]
    if not tokens:
        return False
    blob = _search_blob(place)
    return all(token in blob for token in tokens)


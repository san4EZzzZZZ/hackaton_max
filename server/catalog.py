"""Places catalog: loads `data/places.json` once and serves it to the API layer."""

from __future__ import annotations

import json
import logging
import re
from functools import lru_cache
from pathlib import Path

from pydantic import ValidationError

from server.schemas import Place

logger = logging.getLogger(__name__)

DATA_FILE = Path(__file__).resolve().parent.parent / "data" / "places.json"

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


@lru_cache(maxsize=1)
def load_places() -> tuple[Place, ...]:
    if not DATA_FILE.is_file():
        raise CatalogError(f"Places catalog not found at {DATA_FILE}")
    try:
        raw = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CatalogError(f"Cannot read {DATA_FILE}: {error}") from error

    places: list[Place] = []
    for index, entry in enumerate(raw):
        try:
            places.append(Place.model_validate(entry))
        except ValidationError as error:
            raise CatalogError(f"Invalid place #{index} in {DATA_FILE.name}: {error}") from error

    logger.info("Loaded %d places from %s", len(places), DATA_FILE.name)
    return tuple(places)


def invalidate_cache() -> None:
    """Forget the memoized catalog so an edited `data/places.json` is served without a restart."""
    load_places.cache_clear()


def known_categories() -> tuple[str, ...]:
    """Categories the seed data actually contains, in catalog order."""
    return tuple(dict.fromkeys(place.category for place in load_places()))


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


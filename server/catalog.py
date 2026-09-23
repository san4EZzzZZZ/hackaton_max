"""Places catalog: loads `data/places.json` once and serves it to the API layer."""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path

from pydantic import ValidationError

from server.schemas import Place

logger = logging.getLogger(__name__)

DATA_FILE = Path(__file__).resolve().parent.parent / "data" / "places.json"


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


def known_categories() -> tuple[str, ...]:
    """Categories the seed data actually contains, in catalog order."""
    return tuple(dict.fromkeys(place.category for place in load_places()))

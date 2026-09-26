"""Place guides: loads `data/place_guides.json` once and says which places have a written guide.

The guide is a separate file and a separate endpoint rather than four more fields on `Place`, because
a `Place` is serialized in every element of `/places`, twice in every generated route and once more
inside the saved-route JSON, and unknown keys are ignored: prose there would be paid for by every
client, and a typo in a field name would simply vanish.

`load_guides` never reads the catalog — the caller hands the places over — so a broken guide file
cannot take the catalog down with it. `server.routing` is the one import past the schemas, and it is
there on purpose: the transfer time on the guide screen has to be the same number the route screen
shows for the same two coordinates.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from functools import lru_cache
from pathlib import Path

from pydantic import ValidationError

from server.routing import haversine_km, travel_minutes
from server.schemas import GuideRecord, NearbyPlace, Place

logger = logging.getLogger(__name__)

DATA_FILE = Path(__file__).resolve().parent.parent / "data" / "place_guides.json"

# Four neighbours is what fits under the text on a phone without turning the screen into a list.
NEARBY_LIMIT = 4


class GuideError(RuntimeError):
    """Raised when the guide file is missing or malformed — surfaced as HTTP 503."""


@lru_cache(maxsize=1)
def load_guides() -> tuple[GuideRecord, ...]:
    if not DATA_FILE.is_file():
        raise GuideError(f"Place guides not found at {DATA_FILE}")
    try:
        raw = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise GuideError(f"Cannot read {DATA_FILE}: {error}") from error
    if not isinstance(raw, list):
        # Without this a bare `{}` or `42` in the file reaches the endpoints as a 500 instead of the
        # 503 that tells the reader the feature is down: iterating a non-list is a TypeError.
        raise GuideError(f"Invalid guide file {DATA_FILE.name}: a list is expected, got {type(raw).__name__}")

    guides: list[GuideRecord] = []
    seen: set[str] = set()
    for index, entry in enumerate(raw):
        try:
            guide = GuideRecord.model_validate(entry)
        except ValidationError as error:
            raise GuideError(f"Invalid guide #{index} in {DATA_FILE.name}: {error}") from error
        if guide.place_id in seen:
            raise GuideError(f"Duplicate guide for {guide.place_id} in {DATA_FILE.name}")
        seen.add(guide.place_id)
        guides.append(guide)

    logger.info("Loaded %d place guides from %s", len(guides), DATA_FILE.name)
    return tuple(guides)


def invalidate_cache() -> None:
    """Forget the memoized guides so an edited `data/place_guides.json` is served without a restart."""
    load_guides.cache_clear()


def guide_for(place_id: str) -> GuideRecord | None:
    """The stored half of the answer, or None — the caller decides which of two 404s that is."""
    for guide in load_guides():
        if guide.place_id == place_id:
            return guide
    return None


def nearby_for(
    origin: Place,
    places: Sequence[Place],
    *,
    limit: int = NEARBY_LIMIT,
) -> list[NearbyPlace]:
    """The closest objects in the catalog to this one, with the route engine's own transfer time.

    Sorting by distance rather than by minutes is not an accident of implementation: minutes round,
    so two neighbours a street apart can tie while the metres behind them never do.
    """
    neighbours = [
        place
        for place in places
        if place.id != origin.id and (place.location.lat, place.location.lon)
        != (origin.location.lat, origin.location.lon)
    ]
    scored = sorted(
        neighbours,
        key=lambda place: (haversine_km(origin.location, place.location), place.id),
    )
    return [
        NearbyPlace(
            id=place.id,
            title=place.title,
            category=place.category,
            distance_m=round(haversine_km(origin.location, place.location) * 1000),
            travel_minutes=travel_minutes(origin.location, place.location),
        )
        for place in scored[:limit]
    ]

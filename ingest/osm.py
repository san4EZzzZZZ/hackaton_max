"""Overpass: the raw list of tagged objects inside a city's bounding box.

One query per interest rather than one for the whole city. A combined query is fewer round trips,
but it is also one bigger unit of work for the public instance — and it was measured timing out at
504 where the per-category queries all answered. Per-category requests additionally survive one
category failing: losing `Кафе` is a warning in the report, not an empty city.

The bounding box is used instead of the boundary polygon (`area(...)`), for the same reason: the
polygon clip costs the server a point-in-polygon test per element. What the box lets in — the next
settlement over, a field 30 km out — is dropped on the way back, both by `worth_showing` here and by
the `core_radius_km` clip in `ingest.normalize.build_candidates`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from ingest.taxonomy import Interest, category_of, worth_showing
from ingest.transport import IngestSourceError, PublicApi

logger = logging.getLogger(__name__)

#: Hard ceiling on one query's answer. Overpass truncates silently, and a truncated city is worse
#: than a narrow one: it looks complete.
OUT_LIMIT = 400
QUERY_TIMEOUT = 90


@dataclass(frozen=True)
class Element:
    """One OSM object with its tags, in a shape the normalizer can work with."""

    osm_type: str
    osm_id: int
    lat: float
    lon: float
    tags: dict[str, str]

    @property
    def source_url(self) -> str:
        return f"https://www.openstreetmap.org/{self.osm_type}/{self.osm_id}"

    @property
    def wikidata(self) -> str | None:
        value = (self.tags.get("wikidata") or "").strip()
        return value if value.startswith("Q") else None


def build_query(clauses: tuple[str, ...], bbox: str) -> str:
    """A union of `clauses` clipped to `bbox`, asking for tags plus a usable coordinate."""
    body = ";\n  ".join(f"{clause}({bbox})" for clause in clauses)
    return (
        f"[out:json][timeout:{QUERY_TIMEOUT}];\n(\n  {body};\n);\nout tags center {OUT_LIMIT};"
    )


async def fetch_interest(
    api: PublicApi,
    overpass_url: str,
    clauses: tuple[str, ...],
    bbox: str,
) -> tuple[Element, ...]:
    payload = await api.json(
        "POST",
        overpass_url,
        data={"data": build_query(clauses, bbox)},
        headers={"Accept": "application/json"},
    )
    if not isinstance(payload, dict) or "elements" not in payload:
        raise IngestSourceError(f"Overpass answered without an `elements` list: {str(payload)[:200]}")
    if len(payload["elements"]) >= OUT_LIMIT:
        logger.warning("Overpass answer hit the %d element cap; the city is truncated", OUT_LIMIT)
    return tuple(
        element
        for raw in payload["elements"]
        if (element := _to_element(raw)) is not None
    )


async def collect(
    api: PublicApi,
    overpass_url: str,
    bbox: str,
    interests: tuple[Interest, ...],
) -> tuple[dict[str, tuple[Element, ...]], list[str]]:
    """Elements grouped by catalog category, and the categories whose query failed.

    Failures come back rather than raising: a city where `Театр` timed out is still a usable city,
    and the report has to say which half of it is missing.
    """
    grouped: dict[str, list[Element]] = {}
    failures: list[str] = []
    for interest in interests:
        try:
            found = await fetch_interest(api, overpass_url, interest.clauses, bbox)
        except IngestSourceError as error:
            logger.warning("Skipping %s: %s", interest.category, error)
            failures.append(f"{interest.category}: {error}")
            continue
        for element in found:
            category = category_of(element.tags)
            if category is None or not worth_showing(category, element.tags):
                continue
            grouped.setdefault(category, []).append(element)
    return {key: tuple(value) for key, value in grouped.items()}, failures


def _to_element(raw: Any) -> Element | None:
    if not isinstance(raw, dict):
        return None
    tags = raw.get("tags")
    if not isinstance(tags, dict) or not str(tags.get("name") or "").strip():
        return None
    # A way's position is only known once Overpass computes its center; `out center` puts it under
    # `center`, while nodes carry lat/lon directly.
    position = raw.get("center") or {}
    lat = position.get("lat", raw.get("lat"))
    lon = position.get("lon", raw.get("lon"))
    if lat is None or lon is None:
        return None
    try:
        return Element(
            osm_type=str(raw.get("type", "node")),
            osm_id=int(raw.get("id") or 0),
            lat=float(lat),
            lon=float(lon),
            tags={str(key): str(value) for key, value in tags.items()},
        )
    except (TypeError, ValueError):
        return None

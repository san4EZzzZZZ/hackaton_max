"""City name to a piece of the map: Nominatim gives the center, the bounding box and the boundary.

Everything downstream needs a rectangle to ask Overpass about, and the boundary's OSM id so the
answer can be clipped to the city proper rather than to whatever falls inside the box. A box is used
for the search itself: `area(...)` polygon clipping is dramatically slower on the public instances
and is what earns a 504 on a wide query.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import urlencode

from ingest.transport import IngestSourceError, PublicApi
from server.routing import haversine_km
from server.schemas import Location

logger = logging.getLogger(__name__)

# OSM's area id is its element id with a type-specific offset; the formula is stable and cheaper
# than a second round trip to look the area up by name.
_AREA_OFFSET = {"relation": 3_600_000_000, "way": 2_400_000_000, "node": 1_800_000_000}


@dataclass(frozen=True)
class City:
    """A resolved settlement: what to call it in the catalog, and where it is."""

    name: str
    lat: float
    lon: float
    south: float
    west: float
    north: float
    east: float
    osm_type: str
    osm_id: int
    wikidata: str | None = None

    @property
    def bbox(self) -> str:
        """Overpass's `south,west,north,east` order — not the (lat, lon) order the API answers use."""
        return f"{self.south},{self.west},{self.north},{self.east}"

    @property
    def area_id(self) -> int | None:
        offset = _AREA_OFFSET.get(self.osm_type)
        return None if offset is None else offset + self.osm_id

    @property
    def source_url(self) -> str:
        return f"https://www.openstreetmap.org/{self.osm_type}/{self.osm_id}"

    @property
    def half_extent_km(self) -> float:
        """From the center to the edge of the box, along the shorter axis.

        The search is clipped to the box, so this is not a filter but a scale: it decides what
        «далеко от центра» means for ranking, and it has to mean something different for Kazan and
        for a town whose boundary is a five-minute walk across.
        """
        north_south = haversine_km(_point(self.lat, self.lon), _point(self.north, self.lon))
        west_east = haversine_km(_point(self.lat, self.lon), _point(self.lat, self.west))
        return max(1.0, min(north_south, west_east))


def _point(lat: float, lon: float) -> Location:
    return Location(lat=lat, lon=lon)


async def resolve_city(api: PublicApi, nominatim_url: str, query: str) -> City:
    """Find the settlement called `query`, or raise with what Nominatim actually said.

    A bare search for «Казань» also returns a village in Sverdlovsk oblast and a station in
    Yekaterinburg, and quietly taking the first hit would write one of those over the real city. So
    only a `place=city|town` or an administrative boundary counts, and the pass widens to
    `place=town`-or-anything only if the strict reading found nothing.
    """
    url = f"{nominatim_url.rstrip('/')}/search?{urlencode({'q': query, 'format': 'jsonv2', 'limit': 10})}"
    payload = await api.json("GET", url, headers={"Accept": "application/json"})
    if not isinstance(payload, list):
        raise IngestSourceError(f"Nominatim answered {type(payload).__name__} for {query!r}")

    strict = [
        hit
        for hit in payload
        if (hit.get("category") == "place" and hit.get("type") in {"city", "town"})
        or (hit.get("category") == "boundary" and hit.get("type") == "administrative")
    ]
    relaxed = [hit for hit in payload if hit.get("category") == "place"]
    candidates = strict or relaxed
    if not candidates:
        described = ", ".join(
            f"{hit.get('category')}={hit.get('type')} «{hit.get('display_name', '')[:60]}»"
            for hit in payload[:5]
        )
        raise IngestSourceError(
            f"Nominatim found no city for {query!r}" + (f": {described}" if described else "")
        )
    return _to_city(candidates[0], fallback=query)


def _to_city(hit: dict, *, fallback: str) -> City:
    box = [float(value) for value in hit.get("boundingbox", ["0", "0", "0", "0"])]
    south, north, west, east = (list(box) + [0, 0, 0, 0])[:4]
    label = hit.get("name") or (hit.get("display_name", "").split(",")[0] or fallback)
    return City(
        name=label,
        lat=float(hit["lat"]),
        lon=float(hit["lon"]),
        south=min(south, north),
        north=max(south, north),
        west=min(west, east),
        east=max(west, east),
        osm_type=hit.get("osm_type", "node"),
        osm_id=int(hit.get("osm_id") or 0),
        wikidata=(hit.get("extratags") or {}).get("wikidata"),
    )

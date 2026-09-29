"""The sidewalk line under a planned route.

`server/routing.py` measures distance in straight lines, and it should: a planner that guessed street
lengths would spend its budget on a map server's moods instead of on the visitor's time. But a map
cannot draw a route as a sequence of chords between markers — the line has to follow the pavement, or
the moment the visitor trusts it and turns left into a courtyard is the moment the app stops being a
guide.

One request per route, not one per leg: OSRM accepts the whole chain as via points and answers with
one polyline plus a leg per gap, so a nine-stop walk costs one round trip. The polyline is then cut
back into legs at the snapped waypoints, which lets the navigation screen draw only what is left.

Two numbers arrive from OSRM and only one of them is used. `legs[i].distance` is the length of the
path a person will actually cover, which is what the "N м по тротуарам" caption promises.
`legs[i].duration` is not: the public demo instance returns 107 seconds for an 809 metre leg — 27 km/h
on foot — so this module never writes to a timeline. Minutes stay the planner's own model, and a
route that fit two hours when it was assembled still fits them after the line is drawn.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

import httpx

from core.config import settings
from server.schemas import Location, RouteStop

logger = logging.getLogger(__name__)

#: Where the line came from, and therefore what the client is allowed to claim about it.
GeometrySource = Literal["osrm", "straight_line"]

# A waypoint is reported by OSRM with the same arithmetic that produced the polyline, so the two agree
# to the digit. The tolerance exists for the case where they do not: a via point snapped to a node
# shared by both legs has to be found by both, and the last leg has to end at the end of the line.
_SNAP_TOLERANCE = 1e-6

# OSRM takes longitude first, the opposite of every other coordinate in this codebase. Getting it
# backwards is not an error the service reports: swapping lat/lon on Rostov asks for a route across the
# Black Sea and answers with one (measured: 1 491 km for two points 700 m apart).
_COORD_PRECISION = 6


@dataclass(slots=True)
class WalkLeg:
    """One gap between neighbouring stops: the line to draw and the metres it contains."""

    coordinates: list[tuple[float, float]]
    distance_m: int | None


@dataclass(slots=True)
class Walk:
    """The whole route as leg-sized pieces, plus the honesty marker the client renders."""

    source: GeometrySource
    legs: list[WalkLeg]


def route_points(stops: Sequence[RouteStop], start: Location | None) -> list[Location]:
    """The nodes of the chain in walk order: the visitor's start first, when they named one."""
    points = [start] if start is not None else []
    points.extend(stop.place.location for stop in stops)
    return points


async def draw(
    points: Sequence[Location],
    *,
    base_url: str | None = None,
    timeout: float | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Walk:
    """Fetch the walking line through `points`, or fall back to chords and say so.

    The fallback is the normal outcome, not an exceptional one: a public demo instance is shared
    infrastructure, and a route with a straight line on the map beats a spinner. What makes it honest is
    that `source` travels with it, so the Mini App can caption the line «напрямую» instead of
    pretending it knows the pavement.
    """
    url = (base_url if base_url is not None else settings.osrm_url).strip().rstrip("/")
    if len(points) < 2:
        return Walk(source="straight_line", legs=[])
    if not url:
        return _chords(points)

    query = ";".join(
        f"{point.lon:.{_COORD_PRECISION}f},{point.lat:.{_COORD_PRECISION}f}" for point in points
    )
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout if timeout is not None else settings.osrm_timeout, connect=3.0),
            headers={"User-Agent": settings.ingest_user_agent},
            transport=transport,
        ) as client:
            response = await client.get(
                f"{url}/route/v1/foot/{query}",
                params={"overview": "full", "geometries": "geojson", "alternatives": "false"},
            )
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as error:
        logger.info("Геометрия маршрута недоступна (%s) — рисую прямыми", type(error).__name__)
        return _chords(points)

    legs = _legs(payload, len(points))
    if legs is None:
        logger.info("OSRM вернул ответ, который не похож на маршрут — рисую прямыми")
        return _chords(points)
    return Walk(source="osrm", legs=legs)


def _legs(payload: object, expected_nodes: int) -> list[WalkLeg] | None:
    """Split OSRM's single polyline into one leg per gap, or refuse the answer.

    Refusing is the safe branch. A response whose leg count disagrees with the chain would put leg 3
    under stop 5, and a wrong line on a map is a worse failure than no line: the visitor follows it.
    """
    try:
        route = payload["routes"][0]  # type: ignore[index]
        coordinates = route["geometry"]["coordinates"]
        osrm_legs = route["legs"]
        waypoints = payload["waypoints"]  # type: ignore[index]
    except (KeyError, IndexError, TypeError):
        return None
    if len(osrm_legs) != expected_nodes - 1 or len(waypoints) != expected_nodes:
        return None
    nodes = [waypoint["location"] for waypoint in waypoints]
    if not coordinates:
        return None

    bounds = _bounds(coordinates, nodes)
    if bounds is None:
        return None
    return [
        WalkLeg(
            coordinates=[(node[0], node[1]) for node in coordinates[bounds[index] : bounds[index + 1] + 1]],
            distance_m=round(float(osrm_legs[index]["distance"])),
        )
        for index in range(len(bounds) - 1)
    ]


def _bounds(coordinates: list[list[float]], nodes: list[list[float]]) -> list[int] | None:
    """Indices of the polyline where each chain node sits, in walk order.

    Searched forward from the previous hit: OSRM revisits a node on a route that doubles back, and
    taking the first match for the second waypoint would then cut leg one in half and hand leg two a
    line belonging to leg one.
    """
    bounds: list[int] = []
    last = len(nodes) - 1
    for index, node in enumerate(nodes):
        matches = [
            position
            for position in range(bounds[-1] if bounds else 0, len(coordinates))
            if _same(coordinates[position], node)
        ]
        if not matches:
            return None
        # The final node is the end of the line even when the last metres restate a coordinate.
        bounds.append(matches[-1] if index == last else matches[0])
    return bounds


def _same(a: list[float], b: list[float]) -> bool:
    return abs(a[0] - b[0]) <= _SNAP_TOLERANCE and abs(a[1] - b[1]) <= _SNAP_TOLERANCE


def _chords(points: Sequence[Location]) -> Walk:
    """Straight segments, unmeasured. `distance_m` stays None on purpose.

    The stop already carries `distance_m_from_prev`, which is a straight line by definition, so putting
    the same number here would relabel a chord as a walk without learning anything new about it.
    """
    legs = [
        WalkLeg(
            coordinates=[
                (points[index].lon, points[index].lat),
                (points[index + 1].lon, points[index + 1].lat),
            ],
            distance_m=None,
        )
        for index in range(len(points) - 1)
    ]
    return Walk(source="straight_line", legs=legs)


def annotate(stops: Sequence[RouteStop], start: Location | None, walk: Walk) -> list[RouteStop]:
    """Hang each leg on the stop it ends at, leaving the timeline alone.

    Without a start the chain begins at stop one, so legs[0] describes the gap into stop two — the
    offset is the whole of the alignment logic, and it is why `route_points` puts the start first.
    """
    offset = 0 if start is not None else 1
    updated: list[RouteStop] = []
    for index, stop in enumerate(stops):
        leg_index = index - offset
        leg = walk.legs[leg_index] if 0 <= leg_index < len(walk.legs) else None
        updated.append(
            stop.model_copy(
                update={
                    "geometry_from_prev": (
                        [list(node) for node in leg.coordinates] if leg else None
                    ),
                    "walk_distance_m_from_prev": leg.distance_m if leg else None,
                }
            )
        )
    return updated

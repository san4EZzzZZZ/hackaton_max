"""The walking line under a route: where it comes from, and what it is allowed to change.

Everything here answers against a recorded shape of an OSRM reply (`httpx.MockTransport` is the same
seam the ingester tests use). The public demo server is shared infrastructure whose answer for one pair
of coordinates is not a fixture a suite can depend on, and the branch that matters most — the server did
not answer — cannot be produced on demand from a live host at all.
"""

from __future__ import annotations

import asyncio

import httpx

import server.geometry as geometry
from server.geometry import Walk, WalkLeg, annotate, draw, route_points
from server.schemas import Location, Place, RouteStop

# A 700 m leg in Rostov, drawn the way OSRM draws it: the line leaves the first node to the right and
# comes back to the second one, so the polyline is longer than the chord it stands for.
LEG_ONE = [[39.732293, 47.230059], [39.732293, 47.231000], [39.728278, 47.235549]]
LEG_TWO = [[39.728278, 47.235549], [39.722000, 47.236000], [39.718198, 47.236623]]


def osrm_payload(chains: list[list[list[float]]], *, distances: list[int], durations: list[float] | None = None):
    """One polyline, one leg per gap, one waypoint per node — exactly how OSRM splits them.

    Consecutive legs share their junction coordinate, and the real answer carries it once, so the
    joined line does not repeat it either.
    """
    coordinates = list(chains[0])
    for chain in chains[1:]:
        coordinates.extend(chain[1:])
    timings = durations if durations is not None else [100.0] * len(chains)
    nodes = [chain[0] for chain in chains] + [chains[-1][-1]]
    return {
        "code": "Ok",
        "waypoints": [{"location": node, "name": ""} for node in nodes],
        "routes": [
            {
                "geometry": {"type": "LineString", "coordinates": coordinates},
                "legs": [
                    {"distance": distance, "duration": duration, "summary": ""}
                    for distance, duration in zip(distances, timings, strict=True)
                ],
            }
        ],
    }


def answering(payload: object) -> httpx.MockTransport:
    """A transport that replies 200 with this body."""
    return httpx.MockTransport(lambda request: httpx.Response(200, json=payload))


def failing(error: Exception) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        raise error

    return httpx.MockTransport(handler)


def stop(identifier: str, lat: float, lon: float, *, order: int) -> RouteStop:
    place = Place(
        id=identifier,
        title=identifier.title(),
        category="Памятник",
        location=Location(lat=lat, lon=lon),
        visit_duration_minutes=30,
    )
    return RouteStop(
        place=place,
        order=order,
        arrival_offset_minutes=10 * order,
        visit_duration_minutes=30,
        travel_minutes_from_prev=8,
        distance_m_from_prev=640,
    )


def run(points: list[Location], **kwargs: object) -> Walk:
    return asyncio.run(draw(points, base_url="https://osrm.test", **kwargs))


NODES = [
    Location(lat=47.230059, lon=39.732293),
    Location(lat=47.235549, lon=39.728278),
    Location(lat=47.236623, lon=39.718198),
]


def test_the_line_follows_the_pavement_and_is_cut_per_leg() -> None:
    """One request for the whole chain, one leg per gap, real metres per gap.

    Nine stops must not become nine round trips: the chain goes out as via points, and the single
    polyline comes back split.
    """
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(200, json=osrm_payload([LEG_ONE, LEG_TWO], distances=[809, 1147]))

    walk = run(NODES, transport=httpx.MockTransport(handler))

    assert len(requests) == 1
    assert "/route/v1/foot/" in requests[0]
    assert "39.732293,47.230059;39.728278,47.235549;39.718198,47.236623" in requests[0]
    assert walk.source == "osrm"
    assert [leg.coordinates for leg in walk.legs] == [
        [(lon, lat) for lon, lat in LEG_ONE],
        [(lon, lat) for lon, lat in LEG_TWO],
    ]
    assert [leg.distance_m for leg in walk.legs] == [809, 1147]


def test_the_measured_length_is_a_separate_number_from_the_planned_minutes() -> None:
    """OSRM's foot timings stay out of the answer.

    Measured on the live demo instance: 107 seconds for 809 metres, which is 27 km/h on foot. Taking it
    would rewrite a timeline the route already fitted its budget with, so only `distance` is trusted.
    """
    walk = run(
        NODES,
        transport=answering(osrm_payload([LEG_ONE, LEG_TWO], distances=[809, 1147], durations=[107.5, 94.9])),
    )

    assert [leg.distance_m for leg in walk.legs] == [809, 1147]
    assert not hasattr(walk.legs[0], "duration")


def test_a_route_that_ends_on_a_repeated_coordinate_keeps_its_last_metres() -> None:
    """The real answer lists the final node twice when the last step doubles back."""
    tail = [[39.728278, 47.235549], [39.718198, 47.236623], [39.718198, 47.236623]]

    walk = run(NODES, transport=answering(osrm_payload([LEG_ONE, tail], distances=[809, 20])))

    assert walk.legs[1].coordinates[-1] == (39.718198, 47.236623)
    assert len(walk.legs[1].coordinates) == 3


def test_a_route_that_doubles_back_over_a_node_is_still_cut_into_legs() -> None:
    """The approach to a stop can restate coordinates the walk already used.

    Where exactly to cut such a leg is ambiguous, and the answer does not have to be right to be safe:
    the split only decides which metres the navigation screen highlights as upcoming, while the length of
    each gap comes from OSRM's own per-leg distance. What must never break is that the legs tile the
    line — no gap the visitor would not see, no metres counted twice in the drawing.
    """
    out = [[39.732293, 47.230059], [39.730000, 47.233000], [39.728278, 47.235549]]
    back = [[39.728278, 47.235549], [39.730000, 47.233000], [39.718198, 47.236623]]

    walk = run(NODES, transport=answering(osrm_payload([out, back], distances=[700, 1400])))

    drawn = list(walk.legs[0].coordinates) + list(walk.legs[1].coordinates[1:])
    assert [tuple(node) for node in back] == walk.legs[1].coordinates
    assert len(drawn) == 5
    assert walk.legs[1].coordinates[0] == walk.legs[0].coordinates[-1]


def test_an_unreachable_router_leaves_chords_and_no_invented_metres() -> None:
    """The fallback draws something, and marks it as not a walk."""
    walk = run(NODES, transport=failing(httpx.ConnectError("down")))

    assert walk.source == "straight_line"
    assert [leg.distance_m for leg in walk.legs] == [None, None]
    assert [leg.coordinates for leg in walk.legs] == [
        [(39.732293, 47.230059), (39.728278, 47.235549)],
        [(39.728278, 47.235549), (39.718198, 47.236623)],
    ]


def test_an_empty_base_url_asks_nothing_of_the_network() -> None:
    """OSRM_URL='' switches the router off, and an off switch must not be a timeout."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json=osrm_payload([LEG_ONE, LEG_TWO], distances=[1, 2]))

    walk = asyncio.run(draw(NODES, base_url="", transport=httpx.MockTransport(handler)))

    assert seen == []
    assert walk.source == "straight_line"


def test_an_answer_that_is_not_a_route_is_not_drawn() -> None:
    """A `code` other than Ok, a leg count that disagrees with the chain, and HTML from a proxy."""
    bad = [
        {"code": "No Route", "routes": []},
        osrm_payload([LEG_ONE], distances=[809]),
        {"code": "Ok", "routes": [{"geometry": {"coordinates": []}, "legs": []}], "waypoints": []},
        "<html>502 Bad Gateway</html>",
    ]
    for payload in bad:
        walk = run(NODES, transport=answering(payload))
        assert walk.source == "straight_line", payload


def test_a_polyline_that_misses_a_node_is_not_guesswork() -> None:
    """If a waypoint is not on the line, every leg boundary after it would be a coin toss."""
    payload = osrm_payload([LEG_ONE, LEG_TWO], distances=[809, 1147])
    payload["routes"][0]["geometry"]["coordinates"] = [[39.9, 47.9], [39.95, 47.95]]

    assert run(NODES, transport=answering(payload)).source == "straight_line"


def test_one_point_has_no_line_to_draw() -> None:
    assert run(NODES[:1]).legs == []


def test_the_first_stop_keeps_the_walk_from_the_start_and_loses_it_without_one() -> None:
    """The offset is the whole alignment: with a start, legs[0] ends at stop one."""
    first = stop("a", 47.230059, 39.732293, order=1)
    second = stop("b", 47.235549, 39.728278, order=2)
    origin = Location(lat=47.2280, lon=39.7350)
    origin_leg = WalkLeg(coordinates=[(0.0, 0.0), (1.0, 1.0)], distance_m=200)
    between = WalkLeg(coordinates=[(1.0, 1.0), (2.0, 2.0)], distance_m=809)

    from_start = annotate([first, second], origin, Walk(source="osrm", legs=[origin_leg, between]))
    without_start = annotate([first, second], None, Walk(source="osrm", legs=[between]))

    assert route_points([first, second], origin)[0] == origin
    assert [stop.walk_distance_m_from_prev for stop in from_start] == [200, 809]
    assert [stop.walk_distance_m_from_prev for stop in without_start] == [None, 809]
    assert without_start[0].geometry_from_prev is None


def test_annotate_leaves_the_timeline_alone() -> None:
    """The plan the visitor bought is not edited by a map server's answer."""
    stops = [stop("a", 47.230059, 39.732293, order=1), stop("b", 47.235549, 39.728278, order=2)]
    before = [(s.arrival_offset_minutes, s.travel_minutes_from_prev, s.distance_m_from_prev) for s in stops]

    after = annotate(stops, None, Walk(source="osrm", legs=[WalkLeg(coordinates=[(1.0, 1.0), (2.0, 2.0)], distance_m=900)]))

    assert [(s.arrival_offset_minutes, s.travel_minutes_from_prev, s.distance_m_from_prev) for s in after] == before


# --- through the endpoint -----------------------------------------------------

GENERATE = "/api/v1/routes/generate"
BODY = {"city": "Ростов-на-Дону", "categories": ["Памятник"], "duration_hours": 2}


def timeline(payload: dict) -> list[tuple]:
    return [
        (stop["order"], stop["arrival_offset_minutes"], stop["travel_minutes_from_prev"], stop["distance_m_from_prev"])
        for stop in payload["stops"]
    ]


async def measured(points, **kwargs: object) -> Walk:
    """A router that always answers, with a line and a length per gap."""
    return Walk(
        source="osrm",
        legs=[
            WalkLeg(
                coordinates=[[left.lon, left.lat], [right.lon, right.lat], [right.lon + 0.001, right.lat]],
                distance_m=300 + 100 * index,
            )
            for index, (left, right) in enumerate(zip(points, points[1:], strict=False))
        ],
    )


def test_generate_answers_with_a_line_even_when_the_router_is_off(client) -> None:
    """Without OSRM the Mini App still gets something to draw, and it is told it is a chord.

    `OSRM_URL` is empty for the whole suite (see conftest), so this is the branch every other route
    test takes without noticing it.
    """
    body = client.post(GENERATE, json=BODY).json()

    assert len(body["stops"]) >= 2, "цепочка из одной точки ничего не проверяет"
    assert body["geometry_source"] == "straight_line"
    assert [stop["geometry_from_prev"] for stop in body["stops"]][0] is None
    assert all(len(leg) == 2 for leg in [s["geometry_from_prev"] for s in body["stops"]] if leg)
    assert all(stop["walk_distance_m_from_prev"] is None for stop in body["stops"])
    assert body["total_walk_distance_m"] is None


def test_a_measured_line_does_not_move_a_single_minute(client, monkeypatch) -> None:
    """The plan is what was promised; the line only measures the ground under it.

    Both requests are deterministic, so the two timelines must be identical — if a future version let
    OSRM's numbers into `arrival_offset_minutes`, a two-hour route would come back three hours long.
    """
    planned = client.post(GENERATE, json=BODY).json()
    monkeypatch.setattr(geometry, "draw", measured)

    drawn = client.post(GENERATE, json=BODY).json()

    assert timeline(drawn) == timeline(planned)
    assert drawn["total_duration_minutes"] == planned["total_duration_minutes"]
    assert drawn["geometry_source"] == "osrm"
    expected = [300 + 100 * index for index in range(len(drawn["stops"]) - 1)]
    assert [stop["walk_distance_m_from_prev"] for stop in drawn["stops"]][1:] == expected
    assert drawn["total_walk_distance_m"] == sum(expected)


def test_a_saved_route_replays_its_line_without_a_second_request(client, monkeypatch) -> None:
    """Geometry is part of the snapshot, not something the reader has to re-derive."""

    def refusing(points, **kwargs: object) -> Walk:
        raise AssertionError("сохранённый маршрут не должен спрашивать у OSRM")

    monkeypatch.setattr(geometry, "draw", measured)
    generated = client.post(GENERATE, json=BODY).json()
    saved = client.post("/api/v1/routes", json=generated).json()
    monkeypatch.setattr(geometry, "draw", refusing)

    fetched = client.get(f"/api/v1/routes/{saved['route_id']}").json()["route"]

    assert [stop["geometry_from_prev"] for stop in fetched["stops"]] == [
        stop["geometry_from_prev"] for stop in generated["stops"]
    ]
    assert fetched["geometry_source"] == "osrm"
    assert fetched["total_walk_distance_m"] == generated["total_walk_distance_m"]

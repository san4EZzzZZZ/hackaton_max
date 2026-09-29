"""Route assembly: the densest walk that fits, then the shortest order for it.

Two phases. Selection searches for the largest set of objects that fits the time and money budget;
ordering then applies 2-opt reversals to shorten the chain of transfers between them. Everything is
deterministic (stable tie-breaks) so the same request always yields the same route.

When the request names an origin, the chain starts there: the walk to the first door is budgeted,
places too far to reach in the time given are dropped before selection, the place the origin stands on
is not offered back as a stop, and the first stop is no longer pinned to the search's own choice.

What gets budgeted is not the whole requested time: `TIME_RESERVE` of it stays unspent so the day has
somewhere to put a queue, and every rule that asks "does this still fit" asks against the same
`planned_minutes` number.

Selection is a beam search rather than a greedy step because a greedy step cannot see ahead: on the
real catalog it spent 150 of 240 minutes on one theatre and returned a 4-hour route with fewer stops
than the 3-hour route for the same categories.

Density is not free: the catalog has cafés standing inside metres of the monuments next door, so a
planner that only counts stops fills a whole day without leaving one address. `admissible` rejects
pairs closer than `MIN_SPACING_M`, rejects a chain with a hop longer than `MAX_LEG_KM`, and among
routes of equal fullness `_final_key` prefers the one that covers the most streets.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from server.catalog import city_matches, has_any_tag, normalize_tags
from server.schemas import Location, Place, RouteRequest, RouteStop

EARTH_RADIUS_KM = 6371.0

# Walking speed plus a fixed allowance for leaving a building and finding the next pavement.
TRANSIT_KMH = 4.8
FIXED_TRANSFER_MINUTES = 3.0

# Two objects can share an address: a café in the arcade of a theatre is a different visit but not a
# different walk. Closer than this the pair counts as one place, and the planner will not spend two
# stops of the day on it.
MIN_SPACING_M = 150.0

# The other end of the same problem. A generated catalog covers a whole city, and a city can be 40 km
# of coastline: without a bound the search pairs two monuments 8 km apart, spends 55 of the 120
# minutes on the path between them and calls the result a route. A hop longer than this is a trip
# somewhere else, not a stop on this walk.
MAX_LEG_KM = 2.5

# The plan is the walk, the day is what the walk takes. Queues, a closed door and one exhibit that turns
# out to be worth an hour all live outside `visit_duration_minutes`, and a route that fills the requested
# hour to the last minute has nowhere to put them. So a slice of the requested time is never spent: the
# product asks for 15–20 %, and 15 % is the one that still costs only a stop on a three- and four-hour
# day (20 % takes one back from a two-hour walk as well).
TIME_RESERVE = 0.15


def planned_minutes(duration_hours: float) -> int:
    """How much of the requested time the engine may spend on the plan; the rest is the reserve."""
    return int(duration_hours * 60 * (1 - TIME_RESERVE))

# Selection is exponential in the number of stops, so it is explored as a beam: a few promising
# starting objects, each keeping its `BEAM_WIDTH` best partial routes per extension step. The cap bounds
# the search, not the route — `_fill_gaps` keeps growing it afterwards, and on a twelve-hour day that is
# the difference between twelve stops and thirteen.
START_VARIANTS = 4
BEAM_WIDTH = 12
MAX_SEARCH_STOPS = 12


def haversine_km(origin: Location, target: Location) -> float:
    """Great-circle distance between two coordinates."""
    lat1, lat2 = math.radians(origin.lat), math.radians(target.lat)
    d_lat = lat2 - lat1
    d_lon = math.radians(target.lon - origin.lon)
    chord = (
        math.sin(d_lat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(d_lon / 2) ** 2
    )
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(chord))


def within_radius_km(location: Location, center: Location, radius_km: float) -> bool:
    """Straight-line filter: how far the object is, not how long the transfer takes."""
    return haversine_km(center, location) <= radius_km


def travel_minutes(origin: Location, target: Location) -> int:
    """Door-to-door transfer time, floored at the fixed walking allowance."""
    driving = haversine_km(origin, target) / TRANSIT_KMH * 60
    return max(1, round(FIXED_TRANSFER_MINUTES + driving))


@dataclass(slots=True)
class _Candidate:
    place: Place
    rating: float

    @property
    def sort_key(self) -> tuple[float, float, str]:
        """Deterministic ordering: rating first, then cheapest, then id."""
        return (-self.rating, self.place.price, self.place.id)


def filter_candidates(places: list[Place], request: RouteRequest) -> list[_Candidate]:
    """Apply the hard constraints: city, categories, mood tags, Pushkin Card, budget and time ceiling.

    An origin adds two more: a place the visitor cannot walk to inside the time they asked for is not a
    candidate at all, however good it is once reached, and a place standing exactly where they already
    stand is not a walk but the starting point of one.
    """
    wanted_categories = {category.strip().lower() for category in request.categories}
    wanted_tags = normalize_tags(request.tags)
    time_budget = planned_minutes(request.duration_hours)
    start = request.start

    result: list[_Candidate] = []
    for place in places:
        if not city_matches(request.city, place.city):
            continue
        if wanted_categories and place.category.lower() not in wanted_categories:
            continue
        if not has_any_tag(place, wanted_tags):
            continue
        if request.is_pushkin_card_only and not place.is_pushkin_card:
            continue
        # A single object already more expensive than the whole budget can never be afforded.
        if request.max_budget is not None and place.price > request.max_budget:
            continue
        # Neither can one that overruns the requested time on its own.
        if place.visit_duration_minutes > time_budget:
            continue
        # Or one the visitor cannot walk to in the time they have: the direct hop is the cheapest
        # arrival there is, since every extra stop adds both a visit and its own transfer allowance.
        if start is not None and travel_minutes(start, place.location) > time_budget:
            continue
        # And one they are already standing at. Coincidence, not closeness: an object across the pavement
        # is a real hop of its own, and the frontend picks starts from this same catalog, so without this
        # rule the walk sends the guest to the place they said they were at.
        if start is not None and place.location == start:
            continue
        result.append(_Candidate(place=place, rating=place.rating))
    return result


def _location_of(point: Place | Location) -> Location:
    """The coordinates of a stop, or of the start — the chain head is a `Location`, the rest are places."""
    return point.location if isinstance(point, Place) else point


def _edge_minutes(origin: Place | Location, target: Place) -> int:
    return travel_minutes(_location_of(origin), target.location)


def _chain_minutes(route: Sequence[Place], start: Location | None = None) -> int:
    """Visits plus transfers, in the given order, including the walk from the start to the first stop."""
    if not route:
        return 0
    visits = sum(place.visit_duration_minutes for place in route)
    transfers = sum(_edge_minutes(route[i], route[i + 1]) for i in range(len(route) - 1))
    head = 0 if start is None else _edge_minutes(start, route[0])
    return visits + transfers + head


def _cost(route: Sequence[Place]) -> float:
    return sum(place.price for place in route)


def _too_close(route: Sequence[Place]) -> bool:
    """Whether two stops of the route stand at one address, however the chain is ordered.

    The start is not one of the stops and not checked here: standing in front of the object you came for
    is the point of a start, not a duplicated visit.
    """
    return any(
        haversine_km(route[i].location, route[j].location) * 1000 < MIN_SPACING_M
        for i in range(len(route))
        for j in range(i + 1, len(route))
    )


def _too_far(route: Sequence[Place]) -> bool:
    """Whether the walk as ordered contains a hop nobody makes on foot to see a monument.

    Consecutive pairs, not every pair: a chain of eight stops can legitimately cross a city street for
    street as long as each step is short. The walk from the visitor's own start is not one of the hops
    either — where they began is their choice, not the planner's promise about the rest.
    """
    return any(
        haversine_km(route[i].location, route[i + 1].location) > MAX_LEG_KM
        for i in range(len(route) - 1)
    )


def _spread(route: Sequence[Place]) -> float:
    """The diameter of the stops in km: how much of the city the visit covers.

    Measured between stops only, so a start far out on the map cannot make a route of three neighbouring
    cafés look like a tour of the whole city.
    """
    if len(route) < 2:
        return 0.0
    return max(haversine_km(a.location, b.location) for a in route for b in route)


def admissible(
    route: Sequence[Place], time_budget: int, budget: float | None, start: Location | None = None
) -> bool:
    """Whether a set of stops can be part of an answer: it fits the day, the money, and is one walk.

    One predicate for the search, the fill pass and everything that audits them afterwards, so a gap the
    planner leaves behind is always a gap it can name a rule for.
    """
    if _chain_minutes(route, start) > time_budget:
        return False
    if budget is not None and _cost(route) > budget:
        return False
    if _too_close(route):
        return False
    return not _too_far(route)


def _step_key(
    route: Sequence[Place], start: Location | None = None
) -> tuple[int, int, float, tuple[str, ...]]:
    """Which partial route survives the beam, sorted ascending: more stops, then more leftover time.

    Stop count has to outrank rating here. Pruning by rating sum reintroduces the exact failure the
    search replaces: the promising-looking long stop wins the slot, and a longer request returns
    fewer places than a shorter one.

    Deliberately not the same ranking as `_final_key`: coverage only becomes a criterion once the stop
    count can no longer grow. Pruning partial routes by spread would drop the chains that were about to
    collect those extra stops, which is the trade the beam is built to avoid. Spacing is a hard filter
    in `admissible`, not a rank.
    """
    return (
        -len(route),
        _chain_minutes(route, start),
        -sum(p.rating for p in route),
        tuple(p.id for p in route),
    )


def _final_key(
    route: Sequence[Place], start: Location | None = None
) -> tuple[int, float, float, int, tuple[str, ...]]:
    """Which complete route is returned: most stops, then best rated, then the widest walk, then the shortest chain.

    The third slot used to be the least time spent, so among equally full routes the engine chose the one
    whose addresses sat closest together: on the real catalog the fourth hour of a day bought a stop 40
    metres from the rest. Spread ranks above time because it is a property of the set, while time is a
    property of its order — dropping time entirely would leave the permutations of one set tied and pick
    between them by generation order, and routes of two or three stops never reach `optimize_order`.

    The trailing id tuple is what makes 'the same request always gives the same route' a property of
    the sort rather than of float arithmetic landing in a lucky order.
    """
    return (
        -len(route),
        -sum(place.rating for place in route),
        -_spread(route),
        _chain_minutes(route, start),
        tuple(sorted(place.id for place in route)),
    )


def _search(
    pool: list[Place], time_budget: int, budget: float | None, start: Location | None = None
) -> list[Place]:
    """Beam search over append-only chains of places that the constraints admit."""
    # Sorting before the search keeps the rating sums — and therefore the tie-breaks — independent of
    # the order the records happen to sit in inside data/places.json.
    pool = sorted(pool, key=lambda place: (-place.rating, place.price, place.id))
    states = [
        [place] for place in pool[:START_VARIANTS] if admissible([place], time_budget, budget, start)
    ]
    complete = list(states)

    while states and len(states[0]) < MAX_SEARCH_STOPS:
        extended: list[list[Place]] = []
        for route in states:
            visited = {place.id for place in route}
            for candidate in pool:
                if candidate.id in visited:
                    continue
                option = [*route, candidate]
                if not admissible(option, time_budget, budget, start):
                    continue
                extended.append(option)
        if not extended:
            break
        extended.sort(key=lambda route: _step_key(route, start))
        states = extended[:BEAM_WIDTH]
        complete.extend(states)

    return min(complete, key=lambda route: _final_key(route, start)) if complete else []


def _fill_gaps(
    route: list[Place],
    leftovers: list[Place],
    time_budget: int,
    budget: float | None,
    start: Location | None = None,
) -> tuple[list[Place], list[Place]]:
    """Drop the best remaining place into the earliest gap it fits, until nothing fits anywhere.

    Without a start, position 0 is not a candidate: the place the search chose to begin from is part of
    its answer, and moving it would let the fill pass undo the one decision the ordering phase is not
    allowed to revisit. With a start the walk begins where the visitor stands, so the first stop is
    whichever place suits that point best and inserting in front of it is a legal move.
    """
    route = list(route)
    remaining = list(leftovers)
    first_gap = 0 if start is not None else 1
    while True:
        options = [
            (-place.rating, index, place)
            for place in remaining
            for index in range(first_gap, len(route) + 1)
            if admissible([*route[:index], place, *route[index:]], time_budget, budget, start)
        ]
        if not options:
            return route, remaining
        _, index, place = min(options, key=lambda option: (option[0], option[1], option[2].id))
        route = [*route[:index], place, *route[index:]]
        remaining.remove(place)


def optimize_order(places: list[Place], start: Location | None = None) -> list[Place]:
    """Shorten the transfer chain with 2-opt segment reversals.

    The set of stops never changes, and every accepted move only reduces the total travel time — so a
    route that fit the time budget still fits after reordering. Without a start the first stop is pinned
    too, because it is the search's own choice, and a reversal needs four stops to have anything to
    reverse; with a start the head of the chain is the visitor, so reversing from index 0 is allowed and
    two stops are already worth comparing.
    """
    route = list(places)
    first_index = 0 if start is not None else 1
    # A reversal needs four stops to have a segment to reverse once the first stop is pinned; with the
    # visitor's position pinned instead, comparing two stops is already a decision worth making.
    if len(route) < (2 if start is not None else 4):
        return route

    improved = True
    while improved:
        improved = False
        for i in range(first_index, len(route) - 1):
            for j in range(i + 1, len(route)):
                # `before` is the start when i == 0, which `first_index` only allows with a start given.
                before = route[i - 1] if i else start
                delta = _edge_minutes(before, route[j]) - _edge_minutes(before, route[i])
                if j + 1 < len(route):
                    delta += _edge_minutes(route[i], route[j + 1]) - _edge_minutes(
                        route[j], route[j + 1]
                    )
                if delta < 0:
                    route[i : j + 1] = reversed(route[i : j + 1])
                    improved = True
    return route


def _timeline(places: list[Place], start: Location | None = None) -> tuple[list[RouteStop], int]:
    """Number the stops and derive arrival offsets, transfer times and distances from the order."""
    stops: list[RouteStop] = []
    elapsed = 0
    for index, place in enumerate(places):
        if index == 0:
            travel = 0 if start is None else _edge_minutes(start, place)
            distance_m = 0 if start is None else round(haversine_km(start, place.location) * 1000)
        else:
            travel = _edge_minutes(places[index - 1], place)
            distance_m = round(haversine_km(places[index - 1].location, place.location) * 1000)
        elapsed += travel
        stops.append(
            RouteStop(
                place=place,
                order=index + 1,
                arrival_offset_minutes=elapsed,
                visit_duration_minutes=place.visit_duration_minutes,
                travel_minutes_from_prev=travel,
                distance_m_from_prev=distance_m,
            )
        )
        elapsed += place.visit_duration_minutes
    return stops, elapsed


def assemble_route(
    candidates: list[_Candidate], request: RouteRequest
) -> tuple[list[RouteStop], int, float]:
    """Build an ordered route; returns stops, total minutes and total cost.

    The minutes and the distance returned here start at `request.start` when the client sent one, so the
    walk it reports is the walk the visitor actually makes — and they stop `TIME_RESERVE` of the asked
    time before the clock they gave runs out.
    """
    time_budget = planned_minutes(request.duration_hours)
    budget = request.max_budget
    start = request.start

    pool = [candidate.place for candidate in candidates]
    if not pool:
        return [], 0, 0.0

    route = _search(pool, time_budget, budget, start)
    leftovers = [place for place in pool if place.id not in {stop.id for stop in route}]

    # Shortening the chain is what opens the gaps the fill pass needs, and filling is what gives the
    # next shortening something to work with, so the two run until the set stops growing.
    while True:
        route = optimize_order(route, start)
        filled, leftovers = _fill_gaps(route, leftovers, time_budget, budget, start)
        if len(filled) == len(route):
            break
        route = filled

    stops, total_minutes = _timeline(optimize_order(route, start), start)
    return stops, total_minutes, _cost(route)

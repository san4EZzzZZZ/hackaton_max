"""Route assembly: the densest walk that fits, then the shortest order for it.

Two phases. Selection searches for the largest set of objects that fits the time and money budget;
ordering then applies 2-opt reversals to shorten the chain of transfers between them. Everything is
deterministic (stable tie-breaks) so the same request always yields the same route.

Selection is a beam search rather than a greedy step because a greedy step cannot see ahead: on the
real catalog it spent 150 of 240 minutes on one theatre and returned a 4-hour route with fewer stops
than the 3-hour route for the same categories.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from server.catalog import city_matches
from server.schemas import Location, Place, RouteRequest, RouteStop

EARTH_RADIUS_KM = 6371.0

# Walking speed plus a fixed allowance for leaving a building and finding the next pavement.
TRANSIT_KMH = 4.8
FIXED_TRANSFER_MINUTES = 3.0

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
    """Apply the hard constraints: city, categories, Pushkin Card, budget and time ceiling."""
    wanted_categories = {category.strip().lower() for category in request.categories}
    time_budget = int(request.duration_hours * 60)

    result: list[_Candidate] = []
    for place in places:
        if not city_matches(request.city, place.city):
            continue
        if wanted_categories and place.category.lower() not in wanted_categories:
            continue
        if request.is_pushkin_card_only and not place.is_pushkin_card:
            continue
        # A single object already more expensive than the whole budget can never be afforded.
        if request.max_budget is not None and place.price > request.max_budget:
            continue
        # Neither can one that overruns the requested time on its own.
        if place.visit_duration_minutes > time_budget:
            continue
        result.append(_Candidate(place=place, rating=place.rating))
    return result


def _edge_minutes(origin: Place, target: Place) -> int:
    return travel_minutes(origin.location, target.location)


def _chain_minutes(route: Sequence[Place]) -> int:
    """Visits plus transfers, in the given order."""
    visits = sum(place.visit_duration_minutes for place in route)
    transfers = sum(_edge_minutes(route[i], route[i + 1]) for i in range(len(route) - 1))
    return visits + transfers


def _cost(route: Sequence[Place]) -> float:
    return sum(place.price for place in route)


def _step_key(route: Sequence[Place]) -> tuple[int, int, float, tuple[str, ...]]:
    """Which partial route survives the beam, sorted ascending: more stops, then more leftover time.

    Stop count has to outrank rating here. Pruning by rating sum reintroduces the exact failure the
    search replaces: the promising-looking long stop wins the slot, and a longer request returns
    fewer places than a shorter one.
    """
    return (-len(route), _chain_minutes(route), -sum(p.rating for p in route), tuple(p.id for p in route))


def _final_key(route: Sequence[Place]) -> tuple[int, float, int, tuple[str, ...]]:
    """Which complete route is returned: most stops, then best rated, then least time spent.

    The trailing id tuple is what makes 'the same request always gives the same route' a property of
    the sort rather than of float arithmetic landing in a lucky order.
    """
    return (
        -len(route),
        -sum(place.rating for place in route),
        _chain_minutes(route),
        tuple(sorted(place.id for place in route)),
    )


def _search(pool: list[Place], time_budget: int, budget: float | None) -> list[Place]:
    """Beam search over append-only chains of places that fit the budget."""
    # Sorting before the search keeps the rating sums — and therefore the tie-breaks — independent of
    # the order the records happen to sit in inside data/places.json.
    pool = sorted(pool, key=lambda place: (-place.rating, place.price, place.id))
    states = [
        [place]
        for place in pool[:START_VARIANTS]
        if _chain_minutes([place]) <= time_budget and (budget is None or _cost([place]) <= budget)
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
                if _chain_minutes(option) > time_budget:
                    continue
                if budget is not None and _cost(option) > budget:
                    continue
                extended.append(option)
        if not extended:
            break
        extended.sort(key=_step_key)
        states = extended[:BEAM_WIDTH]
        complete.extend(states)

    return min(complete, key=_final_key) if complete else []


def _fill_gaps(
    route: list[Place], leftovers: list[Place], time_budget: int, budget: float | None
) -> tuple[list[Place], list[Place]]:
    """Drop the best remaining place into the earliest gap it fits, until nothing fits anywhere.

    Position 0 is never a candidate: the place the search chose to start from is part of its answer,
    and moving it would let the fill pass undo the one decision the ordering phase is not allowed to
    revisit.
    """
    route = list(route)
    remaining = list(leftovers)
    while True:
        options = [
            (-place.rating, index, place)
            for place in remaining
            for index in range(1, len(route) + 1)
            if _fits([*route[:index], place, *route[index:]], time_budget, budget)
        ]
        if not options:
            return route, remaining
        _, index, place = min(options, key=lambda option: (option[0], option[1], option[2].id))
        route = [*route[:index], place, *route[index:]]
        remaining.remove(place)


def _fits(route: Sequence[Place], time_budget: int, budget: float | None) -> bool:
    if _chain_minutes(route) > time_budget:
        return False
    return budget is None or _cost(route) <= budget


def optimize_order(places: list[Place]) -> list[Place]:
    """Shorten the transfer chain with 2-opt segment reversals.

    The set of stops and the starting point never change, and every accepted move only reduces the
    total travel time — so a route that fit the time budget still fits after reordering.
    """
    route = list(places)
    if len(route) < 4:
        return route

    improved = True
    while improved:
        improved = False
        for i in range(1, len(route) - 1):
            for j in range(i + 1, len(route)):
                delta = _edge_minutes(route[i - 1], route[j]) - _edge_minutes(route[i - 1], route[i])
                if j + 1 < len(route):
                    delta += _edge_minutes(route[i], route[j + 1]) - _edge_minutes(
                        route[j], route[j + 1]
                    )
                if delta < 0:
                    route[i : j + 1] = reversed(route[i : j + 1])
                    improved = True
    return route


def _timeline(places: list[Place]) -> tuple[list[RouteStop], int]:
    """Number the stops and derive arrival offsets, transfer times and distances from the order."""
    stops: list[RouteStop] = []
    elapsed = 0
    for index, place in enumerate(places):
        if index == 0:
            travel = 0
            distance_m = 0
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
    """Build an ordered route; returns stops, total minutes and total cost."""
    time_budget = int(request.duration_hours * 60)
    budget = request.max_budget

    pool = [candidate.place for candidate in candidates]
    if not pool:
        return [], 0, 0.0

    route = _search(pool, time_budget, budget)
    leftovers = [place for place in pool if place.id not in {stop.id for stop in route}]

    # Shortening the chain is what opens the gaps the fill pass needs, and filling is what gives the
    # next shortening something to work with, so the two run until the set stops growing.
    while True:
        route = optimize_order(route)
        filled, leftovers = _fill_gaps(route, leftovers, time_budget, budget)
        if len(filled) == len(route):
            break
        route = filled

    stops, total_minutes = _timeline(optimize_order(route))
    return stops, total_minutes, _cost(route)

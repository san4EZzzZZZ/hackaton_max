"""Route assembly: greedy selection under time and budget, ordered by real distance.

Two phases. Selection walks the catalog with a nearest-neighbour heuristic: it starts at the
best-rated object and repeatedly appends the candidate with the highest rating per minute spent,
where the cost of a candidate is the transfer time from the previous stop plus its own visit
duration. Ordering then applies 2-opt reversals to shorten the chain of transfers.
Everything is deterministic (stable tie-breaks) so the same request always yields the same route.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from server.schemas import Location, Place, RouteRequest, RouteStop

EARTH_RADIUS_KM = 6371.0

# Effective door-to-door city speed inside Moscow, including waiting for transport.
TRANSIT_KMH = 20.0
FIXED_TRANSFER_MINUTES = 6.0

# A 10-minute transfer costs the same as 0.1 rating points: proximity matters, content matters more.
TRAVEL_RATING_PENALTY_PER_MINUTE = 0.01


def haversine_km(origin: Location, target: Location) -> float:
    """Great-circle distance between two coordinates."""
    lat1, lat2 = math.radians(origin.lat), math.radians(target.lat)
    d_lat = lat2 - lat1
    d_lon = math.radians(target.lon - origin.lon)
    chord = (
        math.sin(d_lat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(d_lon / 2) ** 2
    )
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(chord))


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
    wanted_city = request.city.strip().lower()
    wanted_categories = {category.strip().lower() for category in request.categories}
    time_budget = int(request.duration_hours * 60)

    result: list[_Candidate] = []
    for place in places:
        if place.city.lower() != wanted_city:
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
    """Number the stops and derive arrival offsets and transfer times from the order."""
    stops: list[RouteStop] = []
    elapsed = 0
    for index, place in enumerate(places):
        travel = 0 if index == 0 else _edge_minutes(places[index - 1], place)
        elapsed += travel
        stops.append(
            RouteStop(
                place=place,
                order=index + 1,
                arrival_offset_minutes=elapsed,
                visit_duration_minutes=place.visit_duration_minutes,
                travel_minutes_from_prev=travel,
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

    remaining = {candidate.place.id: candidate for candidate in candidates}
    if not remaining:
        return [], 0, 0.0

    current = min(remaining.values(), key=lambda candidate: candidate.sort_key)
    del remaining[current.place.id]
    chosen: list[Place] = [current.place]
    elapsed = current.place.visit_duration_minutes
    cost = current.place.price

    while remaining and elapsed < time_budget:
        best_key: tuple[float, float, str] | None = None
        best_candidate: _Candidate | None = None
        best_travel = 0
        for candidate in remaining.values():
            travel = travel_minutes(chosen[-1].location, candidate.place.location)
            total = travel + candidate.place.visit_duration_minutes
            if elapsed + total > time_budget:
                continue
            if budget is not None and cost + candidate.place.price > budget:
                continue
            value = candidate.rating - travel * TRAVEL_RATING_PENALTY_PER_MINUTE
            key = (-value, candidate.place.price, candidate.place.id)
            if best_key is None or key < best_key:
                best_key, best_candidate, best_travel = key, candidate, travel
        if best_candidate is None:
            break

        selected = best_candidate
        del remaining[selected.place.id]
        elapsed += best_travel + selected.place.visit_duration_minutes
        cost += selected.place.price
        chosen.append(selected.place)

    stops, total_minutes = _timeline(optimize_order(chosen))
    return stops, total_minutes, cost

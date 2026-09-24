"""The routing engine, tested as the pure function it is — no HTTP, no database.

The engine's useful properties are determinism and that it never promises more time or money than was
asked for. Both are easy to lose in a heuristic tweak, and both are invisible in a hand-run.
"""

from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from server.routing import (
    FIXED_TRANSFER_MINUTES,
    TRANSIT_KMH,
    assemble_route,
    filter_candidates,
    haversine_km,
    optimize_order,
    travel_minutes,
)
from server.schemas import Location, Place, RouteRequest

CENTER = Location(lat=47.2222, lon=39.7185)


def place(
    identifier: str,
    *,
    lat: float = 47.22,
    lon: float = 39.72,
    price: float = 0.0,
    rating: float = 4.5,
    minutes: int = 60,
    category: str = "Музей",
    city: str = "Ростов-на-Дону",
    pushkin: bool = False,
) -> Place:
    return Place(
        id=identifier,
        title=f"Тест {identifier}",
        category=category,
        city=city,
        location=Location(lat=lat, lon=lon),
        price=price,
        rating=rating,
        visit_duration_minutes=minutes,
        is_pushkin_card=pushkin,
    )


def request(city: str = "Ростов", **kwargs) -> RouteRequest:
    return RouteRequest(city=city, **kwargs)


def offsets(route) -> list[tuple[str, int]]:
    return [(stop.place.id, stop.arrival_offset_minutes) for stop in route]


def test_haversine_is_symmetric_and_zero_on_itself() -> None:
    elsewhere = Location(lat=47.25, lon=39.8)
    assert haversine_km(CENTER, CENTER) == 0.0
    assert math.isclose(
        haversine_km(CENTER, elsewhere), haversine_km(elsewhere, CENTER), rel_tol=1e-9
    )


def test_haversine_is_a_great_circle_not_a_grid_distance() -> None:
    one_degree_of_latitude = haversine_km(CENTER, Location(lat=48.2222, lon=39.7185))
    assert 110 < one_degree_of_latitude < 112
    # The same degree of longitude shrinks with cos(latitude); a flat-earth formula would miss this.
    one_degree_of_longitude = haversine_km(CENTER, Location(lat=47.2222, lon=40.7185))
    assert one_degree_of_longitude < one_degree_of_latitude


def test_transfer_time_never_understates_the_walk() -> None:
    same = travel_minutes(CENTER, CENTER)
    assert same == max(1, round(FIXED_TRANSFER_MINUTES))
    far = travel_minutes(CENTER, Location(lat=47.30, lon=39.90))
    assert far > same
    expected = round(
        FIXED_TRANSFER_MINUTES + haversine_km(CENTER, Location(lat=47.30, lon=39.90)) / TRANSIT_KMH * 60
    )
    assert far == expected


def test_selection_applies_every_hard_constraint() -> None:
    candidates = [
        place("cheap"),
        place("pricey", price=900),
        place("park", category="Парк"),
        place("pushkin", pushkin=True),
        place("far-city", city="Москва"),
        place("long", minutes=120),
    ]

    def ids(**kwargs) -> set[str]:
        return {candidate.place.id for candidate in filter_candidates(candidates, request(**kwargs))}

    assert ids() == {"cheap", "pricey", "park", "pushkin", "long"}
    assert ids(max_budget=500) == {"cheap", "park", "pushkin", "long"}
    assert ids(categories=["Парк"]) == {"park"}
    assert ids(categories=["парк", ""]) == {"park"}, "filter is case-insensitive and drops blanks"
    assert ids(is_pushkin_card_only=True) == {"pushkin"}
    assert ids(duration_hours=1) == {"cheap", "pricey", "park", "pushkin"}, "300 min cannot fit"
    assert ids(city="Москва") == {"far-city"}
    assert ids(categories=["несуществующая"]) == set()


def test_no_candidates_produces_an_empty_route_instead_of_an_error() -> None:
    stops, minutes, cost = assemble_route(filter_candidates([place("a")], request(city="Сочи")), request(city="Сочи"))
    assert stops == [] and minutes == 0 and cost == 0.0


def test_the_same_input_always_produces_the_same_route() -> None:
    candidates = [
        place(name, lat=47.20 + index / 100, lon=39.70 + index / 100, rating=4.9 - index / 10, price=index * 50)
        for index, name in enumerate("abcdefg")
    ]
    payload = request(duration_hours=6, max_budget=300)
    first = offsets(assemble_route(filter_candidates(candidates, payload), payload)[0])
    shuffled = list(reversed(candidates))
    assert offsets(assemble_route(filter_candidates(shuffled, payload), payload)[0]) == first, (
        "candidate order in the file must not change the route"
    )


def test_route_stays_inside_the_time_and_the_budget_it_was_given() -> None:
    candidates = [
        place(name, lat=47.20 + index / 1000, lon=39.70, price=100, rating=4.9, minutes=45)
        for index, name in enumerate("abcdefgh")
    ]
    for hours, budget in ((2.0, 1000), (5.0, 250), (8.0, 60)):
        stops, minutes, cost = assemble_route(
            filter_candidates(candidates, request(duration_hours=hours, max_budget=budget)),
            request(duration_hours=hours, max_budget=budget),
        )
        assert minutes <= hours * 60
        assert cost <= budget


def test_the_priciest_object_is_dropped_before_the_first_is_reached() -> None:
    candidates = [place("good", price=50, rating=4.5), place("bad", price=500, rating=5.0)]
    stops, _minutes, cost = assemble_route(
        filter_candidates(candidates, request(max_budget=60)), request(max_budget=60)
    )
    assert [stop.place.id for stop in stops] == ["good"]
    assert cost == 50


def test_reordering_keeps_the_stops_and_the_start() -> None:
    route = [
        place("start", lat=47.25, lon=39.75),
        place("c", lat=47.20, lon=39.70),
        place("a", lat=47.21, lon=39.71),
        place("b", lat=47.22, lon=39.72),
    ]
    optimized = optimize_order(route)
    assert optimized[0].id == "start"
    assert {stop.id for stop in optimized} == {"start", "a", "b", "c"}
    assert len(optimized) == 4
    assert travel_chain(optimized) <= travel_chain(route)


def travel_chain(route: list[Place]) -> int:
    return sum(travel_minutes(route[i].location, route[i + 1].location) for i in range(len(route) - 1))


def test_short_chains_are_left_alone() -> None:
    pair = [place("a"), place("b")]
    assert optimize_order(pair) == pair
    assert optimize_order([]) == []


def test_the_timeline_numbers_stops_and_accumulates_transfers() -> None:
    first, second = place("first", minutes=30), place("second", lat=47.4, lon=39.9, minutes=45)
    candidates = filter_candidates([first, second], request(duration_hours=4))
    stops, total, _cost = assemble_route(candidates, request(duration_hours=4))

    assert [stop.place.id for stop in stops] == ["first", "second"]
    assert [stop.order for stop in stops] == [1, 2]
    assert stops[0].travel_minutes_from_prev == 0
    assert stops[0].arrival_offset_minutes == 0
    transfer = travel_minutes(first.location, second.location)
    assert stops[1].travel_minutes_from_prev == transfer > FIXED_TRANSFER_MINUTES
    # Arrival is measured from the start of the day, so it carries the previous visit as well.
    assert stops[1].arrival_offset_minutes == 30 + transfer
    assert total == 30 + transfer + 45


def test_request_accepts_the_budget_under_either_name() -> None:
    assert request(max_budget=500).max_budget == 500
    assert request(budget=500).max_budget == 500
    assert request().max_budget is None


def test_request_refuses_the_fields_nobody_can_interpret() -> None:
    with pytest.raises(ValidationError, match="budgett"):
        request(budgett=500)
    with pytest.raises(ValidationError):
        request(duration_hours=0)
    with pytest.raises(ValidationError):
        request(duration_hours=13)
    with pytest.raises(ValidationError):
        request(city="А")
    with pytest.raises(ValidationError):
        request(max_budget=-5)

"""The routing engine, tested as the pure function it is — no HTTP, no database.

The engine's useful properties are determinism and that it never promises more time or money than was
asked for. Both are easy to lose in a heuristic tweak, and both are invisible in a hand-run. The third
group of tests below guards the property the Mini App is judged on: the walk has to be full.
"""

from __future__ import annotations

import math
import random
from itertools import product

import pytest
from pydantic import ValidationError

from server.catalog import load_places
from server.routing import (
    FIXED_TRANSFER_MINUTES,
    MAX_SEARCH_STOPS,
    TRANSIT_KMH,
    assemble_route,
    filter_candidates,
    haversine_km,
    optimize_order,
    travel_minutes,
    within_radius_km,
)
from server.schemas import Location, Place, RouteRequest, RouteStop

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


def test_transfers_are_priced_like_a_walk_and_not_like_a_taxi() -> None:
    """The constants are the product promise, so they are asserted instead of only used.

    The test above derives its expectation from `TRANSIT_KMH` and cannot notice that number moving; a
    route planned at city speed would offer twelve minutes for a transfer that takes twenty-five on foot,
    and no ordering rule fixes a timeline built on the wrong arithmetic.
    """
    assert TRANSIT_KMH <= 5.0, "4.8 km/h is a walking pace; city traffic has no place in a foot route"
    assert FIXED_TRANSFER_MINUTES <= 5.0

    one_km_away = Location(lat=CENTER.lat + 0.009, lon=CENTER.lon)
    assert haversine_km(CENTER, one_km_away) == pytest.approx(1.0, abs=0.05)
    assert travel_minutes(CENTER, one_km_away) >= 12, "a kilometre on foot is a quarter of an hour"


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


def test_a_landmark_worth_visiting_alone_does_not_eat_the_hour_it_blocks() -> None:
    """One object rated 5.0 sits 1.7 km from three small ones rated 4.0, and an hour fits all three.

    A step that appends the best value first starts at the landmark and then cannot pay for the walk to
    the cluster: one stop out of 60 minutes. Searching the cluster as a starting point returns three
    stops in 51, which is what the hour was asking for.
    """
    landmark = place("landmark", rating=5.0, minutes=30, lat=47.235, lon=39.72)
    cluster = [place(name, rating=4.0, minutes=15) for name in "abc"]
    payload = request(duration_hours=1.0)

    stops, minutes, _cost = assemble_route(filter_candidates([landmark, *cluster], payload), payload)

    assert [stop.place.id for stop in stops] == ["a", "b", "c"]
    assert minutes == 15 + 3 + 15 + 3 + 15, "three short visits and the two transfers between them"
    assert travel_minutes(landmark.location, cluster[0].location) + 30 + 15 > 60, (
        "the scene itself: starting at the landmark does not fit into this hour"
    )


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
    # About three kilometres apart: on foot that is a real transfer of under an hour, which a
    # four-hour walk can afford. It used to be ten times further, back when transfers were driven.
    first, second = place("first", minutes=30), place("second", lat=47.24, lon=39.75, minutes=45)
    candidates = filter_candidates([first, second], request(duration_hours=4))
    stops, total, _cost = assemble_route(candidates, request(duration_hours=4))

    assert [stop.place.id for stop in stops] == ["first", "second"]
    assert [stop.order for stop in stops] == [1, 2]
    assert stops[0].travel_minutes_from_prev == 0
    assert stops[0].arrival_offset_minutes == 0
    assert stops[0].distance_m_from_prev == 0
    transfer = travel_minutes(first.location, second.location)
    assert stops[1].travel_minutes_from_prev == transfer > FIXED_TRANSFER_MINUTES
    assert stops[1].distance_m_from_prev == round(haversine_km(first.location, second.location) * 1000)
    # Arrival is measured from the start of the day, so it carries the previous visit as well.
    assert stops[1].arrival_offset_minutes == 30 + transfer
    assert total == 30 + transfer + 45


# --- density, on the real catalog ---------------------------------------------------------------
#
# The shipped generator used to answer a four-hour request with two stops: a greedy step spends 150
# minutes on the drama theatre and cannot see that the rest of the day is already gone, so four hours
# returned a shorter walk than three. These tests run on `data/places.json` rather than on a synthetic
# grid because the property is about these coordinates — a made-up pool of evenly spaced places would
# pass with any algorithm.

# 0.5-4 h is what the Mini App offers; the three longer ones are reachable through `duration_hours`
# directly, and they are the only cells where the search cap stops being the whole story.
DURATIONS = [0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 6.0, 8.0, 12.0]
CATEGORY_SETS = [
    [],
    ["Парк"],
    ["Театр"],
    ["Музей", "Галерея"],
    ["Архитектура"],
    ["Памятник"],
    ["Парк", "Памятник", "Архитектура"],
]


def catalog() -> list[Place]:
    return list(load_places())


def walk(hours: float, categories: list[str] | None = None) -> tuple[list[RouteStop], int, float]:
    payload = request(duration_hours=hours, categories=categories or [])
    return assemble_route(filter_candidates(catalog(), payload), payload)


def occupied(route: list[Place]) -> int:
    """Visits plus transfers, in the given order — the same arithmetic the engine budgets against."""
    return sum(item.visit_duration_minutes for item in route) + travel_chain(route)


@pytest.mark.parametrize("categories", CATEGORY_SETS, ids=lambda value: "+".join(value) or "все")
def test_more_time_never_means_a_shorter_walk(categories: list[str]) -> None:
    previous_hours, previous_stops = DURATIONS[0], 0
    for hours in DURATIONS:
        stops, _minutes, _cost = walk(hours, categories)
        assert len(stops) >= previous_stops, (
            f"{categories or 'all categories'}: {hours} h returns {len(stops)} stops, "
            f"{previous_hours} h returned {previous_stops}"
        )
        previous_hours, previous_stops = hours, len(stops)


def test_the_requests_the_mini_app_opens_with_are_measured_in_stops() -> None:
    # The old engine gave 1 / 2 / 3 / 2 here. One hour is still a single stop and that is geography,
    # not the search: the two nearest objects are a 13-minute walk apart, so 25 + 13 + 25 = 63 minutes
    # does not fit in 60. It stops being true when the center fills up (#32), not before.
    for hours, floor in ((1.0, 1), (2.0, 3), (3.0, 4), (4.0, 6)):
        stops, minutes, _cost = walk(hours)
        assert len(stops) >= floor, f"{hours} h assembled only {len(stops)} stops ({minutes} min occupied)"


def test_a_day_longer_than_the_search_cap_is_still_filled() -> None:
    """`MAX_SEARCH_STOPS` bounds the beam, not the route — filling the gaps is what goes past it.

    Turn that pass off and a twelve-hour request stops dead at the cap with more than an hour of the
    day still free: the search is not allowed to look for a thirteenth place, so somebody has to put
    one in afterwards.
    """
    stops, minutes, _cost = walk(12.0)
    assert len(stops) > MAX_SEARCH_STOPS, (
        f"the beam stopped at {MAX_SEARCH_STOPS} stops and the fill pass added nothing"
    )
    assert minutes <= 12 * 60


def test_nothing_that_would_have_fit_is_left_behind() -> None:
    # Position 0 is deliberately not treated as a gap: the place the walk starts at is the search's
    # decision, and the fill pass must not undo it. On the current catalog that costs exactly one route —
    # an eight-hour park walk leaves the zoo out because the zoo would only have fitted in front.
    for hours, categories in product(DURATIONS, CATEGORY_SETS):
        stops, _minutes, _cost = walk(hours, categories)
        order = [stop.place for stop in stops]
        chosen = {stop.place.id for stop in stops}
        budget = int(hours * 60)
        # No money cap in these requests, so time alone decides whether a gap was real.
        candidates = filter_candidates(catalog(), request(duration_hours=hours, categories=categories))
        for candidate in candidates:
            if candidate.place.id in chosen:
                continue
            for index in range(1, len(order) + 1):
                gap = [*order[:index], candidate.place, *order[index:]]
                assert occupied(gap) > budget, (
                    f"{hours} h {categories}: {candidate.place.id} fitted at position {index} — "
                    f"{occupied(gap)} min of a {budget} min budget"
                )


def test_a_multi_stop_walk_is_not_one_stop_with_decoration() -> None:
    for hours, categories in product(DURATIONS, CATEGORY_SETS):
        stops, _minutes, _cost = walk(hours, categories)
        if len(stops) < 2:
            continue
        longest = max(stop.visit_duration_minutes for stop in stops)
        assert longest <= hours * 30, (
            f"{hours} h {categories}: one stop takes {longest} of {int(hours * 60)} minutes"
        )


def test_the_time_and_the_distance_fields_describe_the_same_walk() -> None:
    stops, minutes, _cost = walk(4.0)

    transfers = sum(stop.travel_minutes_from_prev for stop in stops)
    visits = sum(stop.visit_duration_minutes for stop in stops)
    assert transfers + visits == minutes
    assert stops[-1].arrival_offset_minutes + stops[-1].visit_duration_minutes == minutes

    assert stops[0].distance_m_from_prev == 0
    for previous, current in zip(stops, stops[1:]):
        assert current.distance_m_from_prev == round(
            haversine_km(previous.place.location, current.place.location) * 1000
        )
    # Four hours of walking is a few kilometres. If this ever reads like a marathon, `TRANSIT_KMH`
    # has drifted away from the pedestrian promise the onboarding screen makes.
    assert 1.5 < sum(stop.distance_m_from_prev for stop in stops) / 1000 < 6


def test_the_real_catalog_gives_the_same_walk_whatever_order_the_records_come_in() -> None:
    def shape(stops, minutes, _cost) -> tuple[list[str], int, int]:
        return (
            [stop.place.id for stop in stops],
            minutes,
            sum(stop.distance_m_from_prev for stop in stops),
        )

    for hours in (2.0, 3.0, 4.0):
        payload = request(duration_hours=hours)
        baseline = shape(*walk(hours))
        for seed in range(4):
            shuffled = catalog()
            random.Random(seed).shuffle(shuffled)
            compared = shape(*assemble_route(filter_candidates(shuffled, payload), payload))
            assert compared == baseline, f"{hours} h, shuffle {seed}: {compared[0]} != {baseline[0]}"


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


def test_the_radius_test_is_a_straight_line_from_the_center() -> None:
    near = Location(lat=47.23, lon=39.73)
    far = Location(lat=47.9, lon=39.73)
    assert within_radius_km(near, CENTER, 5.0)
    assert not within_radius_km(far, CENTER, 5.0)
    assert within_radius_km(CENTER, CENTER, 0.001), "the center itself sits at zero distance"
    # The boundary counts as inside: an object exactly `radius_km` away is still offered.
    assert within_radius_km(near, CENTER, haversine_km(CENTER, near))
    assert not within_radius_km(near, CENTER, haversine_km(CENTER, near) - 0.001)

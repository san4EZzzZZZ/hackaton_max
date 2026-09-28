"""The routing engine, tested as the pure function it is — no HTTP, no database.

The engine's useful properties are determinism and that it never promises more time or money than was
asked for. Both are easy to lose in a heuristic tweak, and both are invisible in a hand-run. The third
group of tests below guards the property the Mini App is judged on: the walk has to be full.
"""

from __future__ import annotations

import math
import random
from itertools import combinations, product

import pytest
from pydantic import ValidationError

from server.catalog import load_places
from server.routing import (
    FIXED_TRANSFER_MINUTES,
    MAX_SEARCH_STOPS,
    MIN_SPACING_M,
    TRANSIT_KMH,
    admissible,
    assemble_route,
    filter_candidates,
    haversine_km,
    optimize_order,
    travel_minutes,
    within_radius_km,
    _chain_minutes,
    _final_key,
)
from server.schemas import Location, Place, RouteRequest, RouteStop

CENTER = Location(lat=47.2222, lon=39.7185)

# Where a walk begins is a second axis for the catalog-wide properties below. The origin is a corner of
# the densest part of the catalog rather than one of its addresses, so a rule that only holds when the
# visitor happens to stand on a door does not pass here.
STARTS = [None, CENTER]


def _start_id(start: Location | None) -> str:
    return "без старта" if start is None else f"от {start.lat},{start.lon}"


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
    tags: tuple[str, ...] = (),
) -> Place:
    return Place(
        id=identifier,
        title=f"Тест {identifier}",
        category=category,
        tags=list(tags),
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
    stops in 57, which is what the hour was asking for.
    """
    landmark = place("landmark", rating=5.0, minutes=30, lat=47.235, lon=39.72)
    # A short step apart rather than three objects at one address, which the planner now reads as one
    # place: this test is about the hour being spent on three stops, not about the spacing rule.
    cluster = [
        place(name, rating=4.0, minutes=15, lon=39.72 + 0.003 * index)
        for index, name in enumerate("abc")
    ]
    payload = request(duration_hours=1.0)

    stops, minutes, _cost = assemble_route(filter_candidates([landmark, *cluster], payload), payload)

    assert [stop.place.id for stop in stops] == ["a", "b", "c"]
    step = travel_minutes(cluster[0].location, cluster[1].location)
    assert minutes == 3 * 15 + 2 * step, "three short visits and the two transfers between them"
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


def test_admission_rejects_one_address_before_it_rejects_the_clock() -> None:
    """The three reasons a set of stops cannot be an answer, checked on the predicate itself.

    `admissible` is what the search, the fill pass and the audits all consult, so a rule that only shows
    up in a full run is a rule nobody can point at.
    """
    # 227 m apart: two objects at one address would be a third of this distance, and under the threshold.
    apart = [place("cafe", lon=39.72, minutes=60), place("monument", lon=39.723, minutes=60)]
    same_address = [place("cafe", lon=39.72, minutes=60), place("arcade", lon=39.7202, minutes=60)]
    expensive = [
        place("theatre", lon=39.72, minutes=60, price=400),
        place("museum", lon=39.723, minutes=60, price=300),
    ]

    assert haversine_km(apart[0].location, apart[1].location) * 1000 > MIN_SPACING_M
    assert admissible(apart, time_budget=600, budget=None) is True
    assert admissible(same_address, time_budget=600, budget=None) is False
    assert admissible(apart, time_budget=100, budget=None) is False
    assert admissible(expensive, time_budget=600, budget=500) is False


def test_the_wider_walk_wins_when_the_stops_already_count_the_same() -> None:
    """Spread ranks above time in `_final_key`, and that is the whole answer to the fourth hour.

    Two routes, two equally rated stops each: the tight pair is cheaper to walk, the wide pair covers
    more of the city. Ranking by time here — which is what the key used to do — flips this choice and
    every request-level test still passes, so the ordering itself is what is being pinned.
    """
    tight = [place("t1", lat=47.22, lon=39.72), place("t2", lat=47.22, lon=39.723)]
    wide = [place("w1", lat=47.22, lon=39.72), place("w2", lat=47.25, lon=39.78)]

    assert travel_chain(tight) < travel_chain(wide)
    assert haversine_km(tight[0].location, tight[1].location) < haversine_km(
        wide[0].location, wide[1].location
    )
    assert min([tight, wide], key=_final_key) is wide


def test_the_start_is_a_cost_in_time_not_a_label() -> None:
    """The same two stops are affordable or not depending on where the visitor begins."""
    pair = [
        place("cafe", lat=47.22, lon=39.72, minutes=60),
        place("museum", lat=47.22, lon=39.723, minutes=60),
    ]
    at_the_door = Location(lat=47.22, lon=39.72)
    across_the_river = Location(lat=47.30, lon=39.72)

    assert admissible(pair, time_budget=200, budget=None, start=at_the_door) is True
    assert admissible(pair, time_budget=200, budget=None, start=across_the_river) is False


def test_a_place_the_visitor_cannot_walk_to_in_time_is_not_a_candidate() -> None:
    """The direct hop is the cheapest arrival there is, so a place beyond it can never be in the answer."""
    near = place("near", lat=47.2205, lon=39.7205, minutes=30)
    far = place("far", lat=47.35, lon=39.72, minutes=30)
    with_start = filter_candidates(
        [near, far], request(duration_hours=2, start_lat=47.22, start_lon=39.72)
    )
    without_start = filter_candidates([near, far], request(duration_hours=2))

    assert [candidate.place.id for candidate in with_start] == ["near"]
    assert [candidate.place.id for candidate in without_start] == ["near", "far"], (
        "the far place is a perfectly good stop while the walk has no origin"
    )


def test_the_walk_begins_with_the_stop_nearest_the_visitor() -> None:
    """Three places in a row and the visitor at one end: the chain has to start at that end.

    Without a start both directions of the same set cost the same, so nothing in the ranking decides
    between them; the head hop breaks the tie, and it is also what makes the far end unaffordable here.
    """
    line = [place(f"p{index}", lat=47.229 + 0.009 * index, lon=39.72, minutes=20) for index in range(3)]
    start = Location(lat=47.22, lon=39.72)
    payload = request(duration_hours=2, start_lat=start.lat, start_lon=start.lon)

    stops, minutes, _cost = assemble_route(filter_candidates(line, payload), payload)

    assert [stop.place.id for stop in stops] == ["p0", "p1", "p2"]
    head = travel_minutes(start, line[0].location)
    assert stops[0].travel_minutes_from_prev == head > 0
    assert stops[0].arrival_offset_minutes == head
    assert stops[0].distance_m_from_prev == round(haversine_km(start, line[0].location) * 1000) > 0
    assert minutes == head + sum(item.visit_duration_minutes for item in line) + travel_chain(line)


def test_the_start_is_not_a_stop_for_the_spacing_rule() -> None:
    """Standing in front of the object you asked to be taken to is the point of a start, not a duplicate."""
    doorstep = place("doorstep", lat=47.2201, lon=39.7201, minutes=20)
    opposite = place("opposite", lat=47.2201, lon=39.7226, minutes=20)
    payload = request(duration_hours=2, start_lat=47.22, start_lon=39.72)

    stops, _minutes, _cost = assemble_route(filter_candidates([doorstep, opposite], payload), payload)

    # Thirteen metres from the start: counted as a stop of its own, not as the start repeated.
    assert [stop.place.id for stop in stops] == ["doorstep", "opposite"]


def test_the_start_lets_the_order_move_the_first_stop_too() -> None:
    """With an origin, 2-opt is allowed to reverse from position 0; without one it is not.

    Both facts need pinning. The reversal is worth a stop on the real catalog — a walk starting north of
    the center gets six places for four hours instead of five — and it is only legitimate because the
    head of the chain is now the visitor rather than a place the search chose for itself.
    """
    near = place("near", lat=47.22, lon=39.72, minutes=20)
    middle = place("middle", lat=47.23, lon=39.72, minutes=20)
    far = place("far", lat=47.24, lon=39.72, minutes=20)
    backwards = [far, middle, near]
    start = Location(lat=47.22, lon=39.72)

    with_origin = optimize_order(backwards, start)
    assert [p.id for p in with_origin] == ["near", "middle", "far"]
    assert _chain_minutes(with_origin, start) < _chain_minutes(backwards, start)

    # The same three stops with no origin: the first place is the search's own choice and stays put, so
    # the chain keeps walking away from the visitor and back.
    assert [p.id for p in optimize_order(backwards)] == ["far", "middle", "near"]


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
# The chips `GET /chips` actually offers. Unlike categories these are the mood axis the setup screen
# sells, and a chip that reaches the client must be walkable: an hour of «Перекусить» is a route, not
# an empty card.
CHIP_SETS = [["coffee"], ["culture"], ["walk"], ["food"], ["photo"]]
CHIP_DURATIONS = [1.0, 2.0, 4.0, 8.0]


def catalog() -> list[Place]:
    return list(load_places())


def walk(
    hours: float,
    categories: list[str] | None = None,
    tags: list[str] | None = None,
    start: Location | None = None,
) -> tuple[list[RouteStop], int, float]:
    origin = {} if start is None else {"start_lat": start.lat, "start_lon": start.lon}
    payload = request(duration_hours=hours, categories=categories or [], tags=tags or [], **origin)
    return assemble_route(filter_candidates(catalog(), payload), payload)


def occupied(route: list[Place], start: Location | None = None) -> int:
    """Visits plus transfers, in the given order — the same arithmetic the engine budgets against."""
    chain = sum(item.visit_duration_minutes for item in route) + travel_chain(route)
    return chain if start is None else chain + travel_minutes(start, route[0].location)


# Where the walk begins is a second axis of the same property: an origin costs the first segment, and a
# longer day must still never return fewer places than a shorter one.
@pytest.mark.parametrize("start", STARTS, ids=_start_id)
@pytest.mark.parametrize("categories", CATEGORY_SETS, ids=lambda value: "+".join(value) or "все")
def test_more_time_never_means_a_shorter_walk(
    categories: list[str], start: Location | None
) -> None:
    previous_hours, previous_stops = DURATIONS[0], 0
    for hours in DURATIONS:
        stops, _minutes, _cost = walk(hours, categories, start=start)
        assert len(stops) >= previous_stops, (
            f"{categories or 'all categories'} from {start or 'no origin'}: {hours} h returns "
            f"{len(stops)} stops, {previous_hours} h returned {previous_stops}"
        )
        previous_hours, previous_stops = hours, len(stops)


@pytest.mark.parametrize("tags", CHIP_SETS, ids=lambda value: "+".join(value))
def test_a_chip_walks_only_to_places_marked_with_it(tags: list[str]) -> None:
    """Whatever `/chips` counts, `/routes/generate` has to deliver — the same predicate, not a guess.

    The screen used to promise «Взять кофе» and send no filter at all, so the route came back with a
    zoo in it. Every stop now carries the tag it was asked for, and none of them is invented.
    """
    for hours in CHIP_DURATIONS:
        stops, minutes, _cost = walk(hours, tags=tags)
        for stop in stops:
            assert set(tags) & set(stop.place.tags), f"{stop.place.id} попал в подборку {tags}"
        assert minutes <= int(hours * 60)
        assert stops, f"чип {tags} на {hours} h не даёт ни одной точки — экран обязан был его не показать"


@pytest.mark.parametrize("tags", CHIP_SETS, ids=lambda value: "+".join(value))
def test_a_chip_gets_no_shorter_a_walk_for_more_time(tags: list[str]) -> None:
    previous_stops = 0
    for hours in CHIP_DURATIONS:
        stops, _minutes, _cost = walk(hours, tags=tags)
        assert len(stops) >= previous_stops, f"{tags}: {hours} h вернул {len(stops)} точек, {previous_stops} было"
        previous_stops = len(stops)


def test_the_two_axes_narrow_the_pool_together_instead_of_one_replacing_the_other() -> None:
    """`tags` AND `categories`, OR inside each list — and an empty list means that axis is not filtering."""
    pool = catalog()
    all_of_it = {c.place.id for c in filter_candidates(pool, request(duration_hours=4))}
    assert all_of_it == {place.id for place in pool}, "пустые фильтры не должны ничего отсекать"

    tagged = {c.place.id for c in filter_candidates(pool, request(duration_hours=4, tags=["walk"]))}
    typed = {
        c.place.id
        for c in filter_candidates(pool, request(duration_hours=4, categories=["Парк"]))
    }
    both = {
        c.place.id
        for c in filter_candidates(
            pool, request(duration_hours=4, categories=["Парк"], tags=["walk"])
        )
    }
    assert both == tagged & typed
    assert both < tagged, "парки — не все прогулки, иначе категорийная ось перестала работать"
    assert both, "пересечение двух осей обязано быть непустым на реальных данных"


def test_an_or_list_of_tags_widens_the_way_the_chip_screen_reads_it() -> None:
    """ChipSpec.tags is a list so a chip can want several moods; inside one axis it is OR, never AND."""
    pool = catalog()
    single = {c.place.id for c in filter_candidates(pool, request(duration_hours=4, tags=["coffee"]))}
    joined = {
        c.place.id
        for c in filter_candidates(pool, request(duration_hours=4, tags=["coffee", "photo"]))
    }
    assert single < joined, "второй тег обязан добавлять места, а не требовать оба сразу"


@pytest.mark.parametrize("start", STARTS, ids=_start_id)
def test_the_requests_the_mini_app_opens_with_are_measured_in_stops(
    start: Location | None
) -> None:
    # The old engine gave 1 / 2 / 3 / 2 here, and one hour was a single stop because of geography:
    # the two nearest objects were a 13-minute walk apart, so 25 + 13 + 25 = 63 minutes does not fit
    # in 60. Filling the center (#32) is what made an hour worth two places instead of one.
    #
    # From an origin the first hour is the one cell that changes, and it changes for the same
    # geographic reason: 12 of its 60 minutes go before any stop (732 m to the nearest door), which
    # leaves 30 for the visit and not enough for the 150 m and the next visit on top of it. Two hours
    # and up are the promise the screen sells, and they hold either way.
    floors = ((1.0, 2 if start is None else 1), (2.0, 3), (3.0, 4), (4.0, 6))
    for hours, floor in floors:
        stops, minutes, _cost = walk(hours, start=start)
        assert len(stops) >= floor, (
            f"{hours} h from {start or 'no origin'} assembled only {len(stops)} stops "
            f"({minutes} min occupied)"
        )


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


# Where the walk begins changes which gaps are real: with an origin the first stop is chosen for that
# point, so position 0 becomes a legal gap and the head hop has to be paid for by every candidate.
@pytest.mark.parametrize("start", STARTS, ids=_start_id)
def test_nothing_that_would_have_fit_is_left_behind(start: Location | None) -> None:
    # Without an origin, position 0 is deliberately not treated as a gap: the place the walk starts at is
    # the search's decision, and the fill pass must not undo it. Checked against the engine's own
    # predicate rather than the time arithmetic, because a left-out place can now be excluded by spacing
    # as well as by the clock — and this is what proves `_fill_gaps` consults the same rules.
    for hours, categories in product(DURATIONS, CATEGORY_SETS):
        stops, _minutes, _cost = walk(hours, categories, start=start)
        order = [stop.place for stop in stops]
        chosen = {stop.place.id for stop in stops}
        budget = int(hours * 60)
        origin = {} if start is None else {"start_lat": start.lat, "start_lon": start.lon}
        # No money cap in these requests, so time and spacing alone decide whether a gap was real.
        candidates = filter_candidates(
            catalog(), request(duration_hours=hours, categories=categories, **origin)
        )
        for candidate in candidates:
            if candidate.place.id in chosen:
                continue
            for index in range(0 if start else 1, len(order) + 1):
                gap = [*order[:index], candidate.place, *order[index:]]
                assert not admissible(gap, budget, None, start), (
                    f"{hours} h {categories} {'' if start is None else f'from {start.lat},{start.lon}'}: "
                    f"{candidate.place.id} was admissible at position {index} — "
                    f"{occupied(gap, start)} min of a {budget} min budget"
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
    # Four hours of walking is a walk, not a lap of one square and not a marathon. The floor sits above
    # the radius of the densest cluster in the catalog: below it the planner is back to spending the hour
    # on addresses inside one arcade, which is what the spacing rule in `admissible` exists to prevent.
    assert 2.5 < sum(stop.distance_m_from_prev for stop in stops) / 1000 < 6


def test_two_stops_are_never_the_same_address() -> None:
    """The catalog has cafés standing metres from the monument next door; a route must not read them as two.

    Non-vacuous by construction: the requests below all return several stops, and before the spacing rule
    the four-hour walk put nine of them inside 1.5 km with pairs 21 m apart.
    """
    checked = 0
    for hours, categories in product(DURATIONS, CATEGORY_SETS):
        stops, _minutes, _cost = walk(hours, categories)
        for first, second in combinations([stop.place for stop in stops], 2):
            checked += 1
            # 150 m spelled here rather than read from MIN_SPACING_M: the promise is stated in metres, and
            # a test that imported the threshold would pass happily the moment somebody set it to zero.
            assert haversine_km(first.location, second.location) * 1000 >= 150.0, (
                f"{hours} h {categories}: {first.id} and {second.id} stand at one address"
            )
    assert checked > 100, "the parametrized walks returned single-stop routes, so nothing was compared"


def test_the_four_hour_walk_spreads_over_the_streets() -> None:
    """The regression this guards is the one a visitor sees: the fourth hour added 40 metres of walking.

    Four hours without categories is the request the Mini App opens with, so the numbers here are the
    difference between «покажу город за четыре часа» and «провёл четыре часа в одной аркаде».
    """
    stops, minutes, _cost = walk(4.0)
    order = [stop.place for stop in stops]

    spread = max(
        haversine_km(first.location, second.location) for first, second in combinations(order, 2)
    )
    assert spread > 1.0, f"four hours of stops all sit inside {spread:.2f} km of each other"
    assert minutes <= 240
    assert len(stops) >= 6, "spreading the walk may cost a stop or two, but not the fullness of the day"


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

"""The seven pilot metrics of §8, computed from the events the Mini App reports.

Every number here is a hypothesis of the pilot, not a result: the product document states the targets
as thresholds to check after the field test, and the whole point of this module is that a threshold
nobody can read out of the database cannot be checked. So the report answers the table row by row, in
the table's own order, and keeps the wording of each row next to its arithmetic.

Two decisions shape the arithmetic:

* **Everything is per session.** A ratio of two raw event counts would drift the moment one client
  fires an event twice on a retry, so both halves of a fraction count *sessions* that reached a step.
* **A metric without observations is `null`, not `0`.** Zero completion and unstarted completion are
  different facts, and a dashboard that paints the second one red sends someone to fix the wrong thing.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from statistics import median

from server.database import AnalyticsEvent
from server.schemas import FunnelMetric, FunnelReport

# «Время до готового маршрута — не более 2 минут»: the threshold is a ceiling, the other six are floors.
TIME_TO_ROUTE_TARGET_MINUTES = 2.0
SETUP_TO_ROUTE_TARGET = 0.70
ROUTE_TO_START_TARGET = 0.55
COMPLETION_TARGET = 0.40
TIME_ADHERENCE_TARGET = 0.70
USEFULNESS_TARGET = 0.70
STABILITY_TARGET = 0.95

# «±15 % от выбранного времени» — from the same table; a walk that ran 20 % long is a miss even when
# the visitor finished it.
ADHERENCE_TOLERANCE = 0.15
# «Оценка 4–5 / все оценки».
USEFUL_RATING_FLOOR = 4

# A fraction rounds to three decimals and a duration to one: enough to compare with a threshold
# written as «70 %» or «2 минуты», not enough to read a median of 47 events as a measurement.
_RATIO_DIGITS = 3
_MINUTE_DIGITS = 1


def _first_moments(events: list[AnalyticsEvent]) -> dict[str, dict[str, datetime]]:
    """For each session, the first moment it reached each step.

    The earliest of each name is picked rather than the first row read: the funnel is about the order
    visitors acted in, and it should not depend on how a query happened to sort a retry.
    """
    moments: dict[str, dict[str, datetime]] = defaultdict(dict)
    for event in events:
        steps = moments[event.session_id]
        known = steps.get(event.name)
        if known is None or event.occurred_at < known:
            steps[event.name] = event.occurred_at
    return moments


def _reached(value: float | None, target: float, *, ceiling: bool = False) -> bool | None:
    if value is None:
        return None
    return value <= target if ceiling else value >= target


def _ratio(numerator: int, denominator: int, threshold: float, **row: str) -> FunnelMetric:
    """A share of two session counts; `null` rather than 0/0 when the funnel has no bottom row yet."""
    value = round(numerator / denominator, _RATIO_DIGITS) if denominator else None
    return FunnelMetric(
        unit="ratio",
        value=value,
        reached=_reached(value, threshold),
        sample=denominator,
        **row,
    )


def build_funnel(
    events: list[AnalyticsEvent], *, window_days: int, generated_at: datetime
) -> FunnelReport:
    """Turn the events of one window into the product table, hypotheses and all."""
    moments = _first_moments(events)

    opened = {session for session, steps in moments.items() if "miniapp_open" in steps}
    setup = {session for session, steps in moments.items() if "route_setup_started" in steps}
    generated = {session for session, steps in moments.items() if "route_generated" in steps}
    started = {session for session, steps in moments.items() if "route_started" in steps}
    completed = {session for session, steps in moments.items() if "route_completed" in steps}
    broken = {session for session, steps in moments.items() if "client_error" in steps}

    # A route shown *before* the app was opened is a client that sent its events out of order; the
    # negative is dropped rather than averaged in, and the smaller `sample` is where that shows up.
    walks = [
        minutes
        for session, steps in moments.items()
        if session in opened and session in generated
        and (minutes := (steps["route_generated"] - steps["miniapp_open"]).total_seconds() / 60) >= 0
    ]
    on_time = [
        event
        for event in events
        if event.name == "route_completed"
        and event.requested_minutes is not None
        and event.actual_minutes is not None
    ]
    ratings = [event.rating for event in events if event.name == "route_rated" and event.rating]

    # A duration gets the median, not the mean: one visitor who opened the app, went to make coffee and
    # came back would otherwise drag the whole city's number past its target.
    time_to_route = round(median(walks), _MINUTE_DIGITS) if walks else None

    adherent = sum(
        1
        for event in on_time
        if abs(event.actual_minutes - event.requested_minutes)
        <= event.requested_minutes * ADHERENCE_TOLERANCE
    )
    useful = sum(1 for rating in ratings if rating >= USEFUL_RATING_FLOOR)
    stable = len(set(moments) - broken)

    return FunnelReport(
        generated_at=generated_at,
        window_days=window_days,
        sessions=len(moments),
        events=len(events),
        metrics=[
            FunnelMetric(
                key="time_to_route",
                label="Время до готового маршрута",
                formula="От открытия мини-приложения до показа маршрута",
                target="Не более 2 минут",
                unit="minutes",
                value=time_to_route,
                reached=_reached(time_to_route, TIME_TO_ROUTE_TARGET_MINUTES, ceiling=True),
                sample=len(walks),
            ),
            _ratio(
                len(setup & generated),
                len(setup),
                SETUP_TO_ROUTE_TARGET,
                key="setup_to_route",
                label="Конверсия в построение",
                formula="Начали настройку и получили маршрут / начали настройку",
                target="Не ниже 70%",
            ),
            _ratio(
                len(generated & started),
                len(generated),
                ROUTE_TO_START_TARGET,
                key="route_to_start",
                label="Конверсия в старт",
                formula="Нажали «Начать» и получили маршрут / получили маршрут",
                target="Не ниже 55%",
            ),
            _ratio(
                len(started & completed),
                len(started),
                COMPLETION_TARGET,
                key="completion",
                label="Завершение маршрута",
                formula="Начали и завершили / начали маршрут",
                target="Не ниже 40%",
            ),
            _ratio(
                adherent,
                len(on_time),
                TIME_ADHERENCE_TARGET,
                key="time_adherence",
                label="Соответствие времени",
                formula=(
                    "Завершённые прогулки не длиннее и не короче заказа более чем на "
                    f"{int(ADHERENCE_TOLERANCE * 100)}% / все завершённые прогулки"
                ),
                target="Не ниже 70%",
            ),
            _ratio(
                useful,
                len(ratings),
                USEFULNESS_TARGET,
                key="usefulness",
                label="Полезность",
                formula=f"Оценки от {USEFUL_RATING_FLOOR} до 5 / все оценки",
                target="Не ниже 70%",
            ),
            _ratio(
                stable,
                len(moments),
                STABILITY_TARGET,
                key="stability",
                label="Техническая стабильность",
                formula="Сессии без критической ошибки / все сессии",
                target="Не ниже 95%",
            ),
        ],
    )

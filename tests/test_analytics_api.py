"""Analytics events: what the API accepts, what it stores, and what §8 reads back out of it."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

import server.routers.analytics as analytics_module
from server.database import AnalyticsEvent, engine, session_scope
from server.schemas import EVENT_NAMES

EVENTS = "/api/v1/events"
FUNNEL = "/api/v1/metrics/funnel"


class _DeadDatabase:
    """Stands in for the sqlite file going away mid-request; the handler must answer 503, not 500."""

    async def __aenter__(self):
        raise SQLAlchemyError("database is gone")

    async def __aexit__(self, *_exc) -> bool:
        return False


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def report(client: TestClient, name: str, session_id: str = "session-01", **fields: object) -> dict:
    response = client.post(EVENTS, json={"name": name, "session_id": session_id, **fields})
    assert response.status_code == 201, (name, response.text)
    return response.json()


def walk_session(client: TestClient, session_id: str) -> None:
    """One visitor who did everything the product asks of them."""
    report(client, "miniapp_open", session_id)
    report(client, "route_setup_started", session_id)
    report(client, "route_generated", session_id, route_id="route-abc")
    report(client, "route_started", session_id, route_id="route-abc")
    report(client, "poi_opened", session_id, place_id="theatre-square")
    report(client, "route_completed", session_id, requested_minutes=240, actual_minutes=250)
    report(client, "route_rated", session_id, rating=5)


def funnel(client: TestClient, **query: object) -> dict[str, dict]:
    response = client.get(FUNNEL, params=query)
    assert response.status_code == 200, response.text
    return {metric["key"]: metric for metric in response.json()["metrics"]}


def _age_events(session_id: str, age: timedelta) -> None:
    """Move a session's events into the past — the one thing a client cannot do and a test must.

    Both the reporting window and the median of `time_to_route` are about *when* a step happened, and a
    store whose every row is stamped `now` can show neither of them being wrong.
    """

    async def run() -> None:
        try:
            async with session_scope() as session:
                rows = (await session.execute(select(AnalyticsEvent))).scalars().all()
                for row in rows:
                    if row.session_id == session_id:
                        row.occurred_at -= age
        finally:
            await engine.dispose()

    asyncio.run(run())


# ── приём событий ──────────────────────────────────────────────────────────────────────────────


def test_every_name_of_the_product_list_is_accepted(client: TestClient) -> None:
    """A name the spec lists and the API rejects is a metric nobody can fill.

    Each payload below is the minimum that event is allowed to carry, so this pins the required-field
    table as well: an event that starts demanding one more number has to change here too.
    """
    minimum = {
        "miniapp_open": {},
        "route_setup_started": {},
        "route_generated": {},
        "route_started": {},
        "poi_opened": {"place_id": "theatre-square"},
        "route_completed": {"requested_minutes": 240, "actual_minutes": 250},
        "route_rated": {"rating": 4},
        "client_error": {"detail": "карта не встала"},
    }
    assert set(minimum) == set(EVENT_NAMES), "the event list and the contract drifted apart"
    for name, fields in minimum.items():
        row = report(client, name, **fields)
        assert row["name"] == name
        assert row["event_id"] > 0


def test_a_stored_event_comes_back_with_every_field_it_carried(client: TestClient) -> None:
    row = report(
        client, "route_completed", requested_minutes=240, actual_minutes=250, route_id="r-1"
    )
    assert row == {
        "event_id": 1,
        "name": "route_completed",
        "session_id": "session-01",
        "user_id": None,
        "route_id": "r-1",
        "place_id": None,
        "rating": None,
        "requested_minutes": 240,
        "actual_minutes": 250,
        "detail": None,
        "occurred_at": row["occurred_at"],
    }


def test_the_server_stamps_the_time_and_the_client_cannot(client: TestClient) -> None:
    """§8 measures intervals between events, so a client clock would be measuring its own error.

    The forged time below is a year ahead: if it ever reached storage, the funnel would report a
    device setting instead of a duration.
    """
    rejected = client.post(
        EVENTS,
        json={
            "name": "miniapp_open",
            "session_id": "session-01",
            "occurred_at": (_utcnow() + timedelta(days=365)).isoformat(),
        },
    )
    assert rejected.status_code == 422

    row = report(client, "miniapp_open")
    assert abs(datetime.fromisoformat(row["occurred_at"]) - _utcnow()) < timedelta(minutes=5)


def test_a_second_visit_of_the_app_is_a_second_event(client: TestClient) -> None:
    """Deduplication would be a lie about what happened; the funnel groups by session, not by row."""
    first = report(client, "miniapp_open")
    second = report(client, "miniapp_open")
    assert first["event_id"] != second["event_id"]


def test_an_unknown_event_name_is_rejected(client: TestClient) -> None:
    response = client.post(EVENTS, json={"name": "purchase_made", "session_id": "session-01"})
    assert response.status_code == 422


def test_an_unknown_field_is_rejected_rather_than_dropped(client: TestClient) -> None:
    """A typo'd `rate` would otherwise arrive as a rating-less `route_rated` and vanish from the metric."""
    response = client.post(
        EVENTS, json={"name": "route_rated", "session_id": "session-01", "rating": 5, "rate": 5}
    )
    assert response.status_code == 422
    assert client.get(FUNNEL).json()["events"] == 0


def test_a_session_id_everyone_shares_would_break_the_funnel(client: TestClient) -> None:
    for session_id in ("1", "sess", "a" * 65, "session with spaces", "session.id"):
        response = client.post(EVENTS, json={"name": "miniapp_open", "session_id": session_id})
        assert response.status_code == 422, session_id


def test_an_invented_owner_is_stored_as_claimed_but_still_bounded(client: TestClient) -> None:
    """`user_id` is not authentication — but it has to fit 8 bytes, or sqlite answers 500 not 422."""
    assert (
        client.post(
            EVENTS,
            json={"name": "miniapp_open", "session_id": "session-01", "user_id": 10**19},
        ).status_code
        == 422
    )
    assert report(client, "miniapp_open", user_id=42)["user_id"] == 42


def test_a_number_only_the_rating_metric_reads_cannot_hide_in_another_event(client: TestClient) -> None:
    """`rating` on `miniapp_open` would be counted into «полезность» and look like a real answer."""
    response = client.post(
        EVENTS, json={"name": "miniapp_open", "session_id": "session-01", "rating": 5}
    )
    assert response.status_code == 422
    assert "rating" in response.text


def test_an_event_needs_the_numbers_that_make_it_useful(client: TestClient) -> None:
    for name, fields in (
        ("route_rated", {}),
        ("poi_opened", {}),
        ("route_completed", {"requested_minutes": 240}),
        ("route_completed", {"actual_minutes": 250}),
    ):
        response = client.post(EVENTS, json={"name": name, "session_id": "session-01", **fields})
        assert response.status_code == 422, (name, fields)


def test_an_impossible_rating_and_an_impossible_duration_are_rejected(client: TestClient) -> None:
    for name, fields in (
        ("route_rated", {"rating": 0}),
        ("route_rated", {"rating": 6}),
        ("route_completed", {"requested_minutes": 0, "actual_minutes": 5}),
        ("route_completed", {"requested_minutes": 240, "actual_minutes": -1}),
        ("client_error", {"detail": "x" * 501}),
    ):
        response = client.post(EVENTS, json={"name": name, "session_id": "session-01", **fields})
        assert response.status_code == 422, (name, fields)


# ── чтение сырых событий ───────────────────────────────────────────────────────────────────────


def test_events_can_be_read_back_one_step_of_the_funnel_at_a_time(client: TestClient) -> None:
    walk_session(client, "session-01")
    report(client, "miniapp_open", "session-02")

    by_name = client.get(EVENTS, params={"name": "route_rated"})
    assert by_name.status_code == 200
    assert {row["session_id"] for row in by_name.json()} == {"session-01"}
    assert by_name.headers["X-Total-Count"] == "1"

    by_session = client.get(EVENTS, params={"session_id": "session-02"})
    assert [row["name"] for row in by_session.json()] == ["miniapp_open"]
    assert by_session.headers["X-Total-Count"] == "1"


def test_the_total_counts_the_selection_not_the_page(client: TestClient) -> None:
    walk_session(client, "session-01")
    response = client.get(EVENTS, params={"limit": 3})
    assert len(response.json()) == 3
    assert response.headers["X-Total-Count"] == "7"
    assert response.headers["X-Offset"] == "0"


def test_a_store_nobody_has_written_to_is_empty_rather_than_missing(client: TestClient) -> None:
    response = client.get(EVENTS)
    assert response.status_code == 200
    assert response.json() == []
    assert response.headers["X-Total-Count"] == "0"


# ── воронка §8 ──────────────────────────────────────────────────────────────────────────────────


def test_an_uneventful_pilot_reports_that_nothing_happened_not_that_it_went_well(
    client: TestClient,
) -> None:
    """`null` and `0` are different news: nobody started a walk is not everybody failed one."""
    body = client.get(FUNNEL).json()
    assert (body["sessions"], body["events"]) == (0, 0)
    assert len(body["metrics"]) == 7
    for metric in body["metrics"]:
        assert metric["value"] is None, metric
        assert metric["reached"] is None, metric
        assert metric["sample"] == 0, metric


def test_a_visitor_who_did_everything_moves_every_number(client: TestClient) -> None:
    walk_session(client, "session-01")
    metrics = funnel(client)
    for key, metric in metrics.items():
        assert metric["value"] is not None, key
        assert metric["reached"] is True, (key, metric)
    assert metrics["time_adherence"]["value"] == 1.0, "250 мин против заказа на 240 — это ±4 %"


def test_the_funnel_is_a_set_of_sessions_and_not_a_count_of_clicks(client: TestClient) -> None:
    """A client that retries `route_started` three times must not report thrice the conversion."""
    report(client, "miniapp_open")
    report(client, "route_setup_started")
    report(client, "route_generated")
    for _ in range(3):
        report(client, "route_started")
    metric = funnel(client)["route_to_start"]
    assert (metric["value"], metric["sample"]) == (1.0, 1)


def test_a_step_reached_without_the_step_before_it_is_not_converted(client: TestClient) -> None:
    """Half a funnel belongs to the step it reached and not to the one it skipped."""
    report(client, "route_started")
    metrics = funnel(client)
    assert metrics["route_to_start"]["value"] is None, "никто не получил маршрут — делить не на что"
    assert metrics["completion"]["value"] == 0.0, "прогулку начали и не завершили"


def test_time_to_route_is_a_median_because_one_philosopher_should_not_move_a_city(
    client: TestClient,
) -> None:
    """0.1, 0.2 and 60 minutes against a target of «не более 2 минут»: the mean would fail the pilot."""
    for session_id, minutes in (("session-01", 0.1), ("session-02", 0.2), ("session-03", 60.0)):
        report(client, "miniapp_open", session_id)
        _age_events(session_id, timedelta(minutes=minutes))
        report(client, "route_generated", session_id)

    metric = funnel(client)["time_to_route"]
    assert metric["value"] == 0.2, metric
    assert (metric["sample"], metric["reached"]) == (3, True)
    assert round(sum([0.1, 0.2, 60.0]) / 3, 1) == 20.1, "the mean this test rules out"


def test_a_walk_longer_than_the_order_by_more_than_fifteen_percent_is_a_miss(client: TestClient) -> None:
    """The tolerance is §8's own ±15 %, and it is inclusive at the edge on both sides."""
    for session_id, actual in (("session-01", 240), ("session-02", 276), ("session-03", 277), ("session-04", 204)):
        report(client, "route_completed", session_id, requested_minutes=240, actual_minutes=actual)
    metric = funnel(client)["time_adherence"]
    assert (metric["value"], metric["sample"]) == (0.75, 4)
    assert metric["reached"] is True


def test_the_four_and_the_five_are_useful_and_the_three_is_not(client: TestClient) -> None:
    for session_id, rating in (
        ("session-01", 5),
        ("session-02", 4),
        ("session-03", 3),
        ("session-04", 1),
    ):
        report(client, "route_rated", session_id, rating=rating)
    metric = funnel(client)["usefulness"]
    assert metric["value"] == 0.5, "две оценки из четырёх попали в полосу 4–5"
    assert metric["reached"] is False


def test_one_broken_session_out_of_twenty_is_exactly_the_five_percent_allowed(client: TestClient) -> None:
    for index in range(20):
        report(client, "miniapp_open", f"session-{index:02d}")
    report(client, "client_error", "session-00", detail="карта не загрузилась")
    metric = funnel(client)["stability"]
    assert (metric["value"], metric["sample"]) == (0.95, 20)
    assert metric["reached"] is True
    assert funnel(client)["setup_to_route"]["value"] is None, "поиск не начинал никто"


def test_the_window_reaches_back_only_as_far_as_it_is_asked(client: TestClient) -> None:
    report(client, "miniapp_open", "session-01")
    _age_events("session-01", timedelta(days=45))
    report(client, "route_rated", "session-02", rating=5)

    assert client.get(FUNNEL).json()["events"] == 1, "окно по умолчанию — 30 дней"
    assert client.get(FUNNEL, params={"days": 60}).json()["events"] == 2


def test_a_window_of_one_day_is_legal_and_a_year_is_a_scan_of_everything(client: TestClient) -> None:
    assert client.get(FUNNEL, params={"days": 1}).status_code == 200
    assert client.get(FUNNEL, params={"days": 0}).status_code == 422
    assert client.get(FUNNEL, params={"days": 367}).status_code == 422


def test_the_report_ships_the_product_table_verbatim(client: TestClient) -> None:
    """Labels and thresholds are the contract with the pilot, not decoration for a slide."""
    metrics = funnel(client)
    assert list(metrics) == [
        "time_to_route",
        "setup_to_route",
        "route_to_start",
        "completion",
        "time_adherence",
        "usefulness",
        "stability",
    ]
    assert metrics["setup_to_route"]["label"] == "Конверсия в построение"
    assert metrics["setup_to_route"]["target"] == "Не ниже 70%"
    assert metrics["time_to_route"]["target"] == "Не более 2 минут"


def test_a_ratio_never_escapes_the_unit_it_claims(client: TestClient) -> None:
    walk_session(client, "session-01")
    report(client, "route_rated", "session-02", rating=2)
    for metric in funnel(client).values():
        assert metric["value"] is not None, metric
        if metric["unit"] == "ratio":
            assert 0.0 <= metric["value"] <= 1.0, metric
        else:
            assert metric["value"] >= 0, metric


def test_the_spec_documents_the_codes_clients_must_handle(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]
    assert paths[EVENTS]["post"]["responses"]["201"]["content"]["application/json"]["schema"][
        "$ref"
    ].endswith("EventRecord")
    assert "422" in paths[EVENTS]["post"]["responses"]
    assert "503" in paths[EVENTS]["post"]["responses"]
    assert paths[FUNNEL]["get"]["responses"]["200"]["content"]["application/json"]["schema"][
        "$ref"
    ].endswith("FunnelReport")


def test_a_database_that_does_not_answer_is_a_503(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(analytics_module, "session_scope", lambda: _DeadDatabase())
    for call in (
        lambda: client.post(EVENTS, json={"name": "miniapp_open", "session_id": "session-01"}),
        lambda: client.get(EVENTS),
        lambda: client.get(FUNNEL),
    ):
        assert call().status_code == 503

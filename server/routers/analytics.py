"""Analytics ingestion and the pilot metrics built on top of it.

§8 of the product document lists seven events and seven numbers a two-week pilot has to answer with.
Without somewhere to put those events every number in the table is a vibe, so this router is the
storage side of «мы проверили гипотезу»: the Mini App reports what happened, `GET /metrics/funnel`
reads the table back.

**Nothing here is authenticated, and nothing here pretends to be.** `session_id` and `user_id` are
supplied by the caller and stored verbatim — the only authenticated surface in this repo is
`/webhook`, and that is MAX calling us. Anyone can post a thousand `route_rated` events with a rating
of 5 and move the «полезность» number. That is acceptable while the audience of the pilot is 30–50
invited people and the report is read by the team that invited them; it stops being acceptable the
moment these numbers are quoted to anybody as a result. Same caveat as the saved routes one level up.

Timestamps are the server's, not the client's, for the same reason: the funnel measures intervals
*between* events of one session, so a client-supplied clock would turn «время до маршрута» into
«погрешность часов устройства». The consequence is that events have to be sent as they happen —
buffering a day of them and posting the batch at midnight reports the whole funnel as instant.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException, Query, Response
from sqlalchemy.exc import SQLAlchemyError

from server.analytics import build_funnel
from server.database import AnalyticsEvent, events_since, list_events, log_event, session_scope
from server.schemas import (
    DATABASE_UNAVAILABLE_RESPONSE,
    PAGINATION_HEADERS,
    EventRecord,
    EventName,
    EventRequest,
    FunnelReport,
)

router = APIRouter(tags=["Analytics"])

# The pilot itself runs two weeks (§9); a window a hundred times longer than that is the widest
# question the table can be asked without turning "how did the pilot go" into an unbounded scan.
MAX_WINDOW_DAYS = 366


def _record(row: AnalyticsEvent) -> EventRecord:
    return EventRecord(
        name=row.name,
        session_id=row.session_id,
        user_id=row.user_id,
        route_id=row.route_id,
        place_id=row.place_id,
        rating=row.rating,
        requested_minutes=row.requested_minutes,
        actual_minutes=row.actual_minutes,
        detail=row.detail,
        event_id=row.event_id,
        occurred_at=row.occurred_at,
    )


@router.post(
    "/events",
    response_model=EventRecord,
    status_code=201,
    summary="Передать событие аналитики",
    description=(
        "Одно из восьми событий пути: открытие приложения, начало настройки, показ маршрута, старт "
        "прохождения, карточка места, завершение, оценка и критическая ошибка клиента. Время ставит "
        "сервер в момент приёма, поэтому событию нужно идти сразу, а не копиться до устойчивой связи. "
        "`client_error` — восьмое имя, которого нет в списке продукта: без него метрика «техническая "
        "стабильность» не из чего считать."
    ),
    responses={
        201: {"description": "Событие записано; `event_id` и `occurred_at` выдаёт база"},
        422: {
            "description": (
                "Тело не проходит проверку: имя вне списка, идентификатор сессии ненадлежащего вида "
                "или поле пришло не со своим событием"
            ),
            "content": {
                "application/json": {
                    "schema": {"$ref": "#/components/schemas/HTTPValidationError"}
                }
            },
        },
        503: DATABASE_UNAVAILABLE_RESPONSE,
    },
)
async def report_event(request: EventRequest) -> EventRecord:
    """Append one event. A second, identical post is a second row: retries are the client's problem to damp.

    Deliberately forgiving about *ordering* and about *duplicates* — a client that reports a route it
    never got is wrong data, not a broken request, and rejecting it would lose the rest of the session.
    """
    try:
        async with session_scope() as session:
            row = await log_event(
                session,
                name=request.name,
                session_id=request.session_id,
                user_id=request.user_id,
                route_id=request.route_id,
                place_id=request.place_id,
                rating=request.rating,
                requested_minutes=request.requested_minutes,
                actual_minutes=request.actual_minutes,
                detail=request.detail,
            )
            return _record(row)
    except SQLAlchemyError as error:
        raise HTTPException(status_code=503, detail="База данных не отвечает") from error


@router.get(
    "/events",
    response_model=list[EventRecord],
    summary="Принятые события",
    description=(
        "Свежие сверху. Нужно, чтобы разобрать одну метрику: `GET /metrics/funnel` показывает долю, а "
        "почему она такая — видно только по строкам. `session_id` собирает воронку одного посетителя."
    ),
    responses={
        200: {"description": "Список событий", "headers": PAGINATION_HEADERS},
        503: DATABASE_UNAVAILABLE_RESPONSE,
    },
)
async def read_events(
    response: Response,
    name: EventName | None = Query(None, description="Только это событие"),
    session_id: str | None = Query(
        None, min_length=8, max_length=64, description="Только эта сессия"
    ),
    limit: int | None = Query(None, ge=1, le=1000, description="Сколько записей вернуть"),
    offset: int = Query(0, ge=0, description="Пропустить N первых записей"),
) -> list[EventRecord]:
    try:
        async with session_scope() as session:
            rows, total = await list_events(
                session, name=name, session_id=session_id, limit=limit, offset=offset
            )
    except SQLAlchemyError as error:
        raise HTTPException(status_code=503, detail="База данных не отвечает") from error

    response.headers["X-Total-Count"] = str(total)
    response.headers["X-Offset"] = str(offset)
    return [_record(row) for row in rows]


@router.get(
    "/metrics/funnel",
    response_model=FunnelReport,
    summary="Метрики пилота",
    description=(
        "Та же таблица, что в продуктовой проработке: семь строк, у каждой — как считаем, целевая "
        "гипотеза, текущее значение и знаменатель. Пустая выборка даёт `null`, а не ноль: «некому "
        "было завершать маршрут» и «никто не завершил» — разные новости для разных людей. "
        "Отсчёт идёт от серверных меток времени, потому что клиентские часы воронку искажают."
    ),
    responses={
        200: {"description": "Все семь метрик за окно; `value: null` — под формулу не попал никто"},
        503: DATABASE_UNAVAILABLE_RESPONSE,
    },
)
async def read_funnel(
    days: int = Query(30, ge=1, le=MAX_WINDOW_DAYS, description="За сколько дней считать"),
) -> FunnelReport:
    """One row per metric of §8, over the events of the window."""
    now = datetime.now(UTC).replace(tzinfo=None)
    try:
        async with session_scope() as session:
            events = await events_since(session, now - timedelta(days=days))
    except SQLAlchemyError as error:
        raise HTTPException(status_code=503, detail="База данных не отвечает") from error

    return build_funnel(events, window_days=days, generated_at=now)

"""Endpoints that keep a generated route beyond one request.

`POST /routes/generate` answers with a throwaway uuid4: refresh the Mini App and the route is gone.
These endpoints give a route a real, database-issued identity, so it can be listed and fetched again
after a restart.

**`user_id` is not authentication.** The HTTP API has no session, no token and no cookie — the only
authenticated surface in this repo is `/webhook`, and that is MAX calling us. Whoever posts a route
claims whatever owner they like, and whoever knows a `route_id` can read it. Filtering by `user_id` is
a convenience scope for one visitor's own list, nothing more; treating it as access control would be a
security bug. If real accounts ever matter here, auth has to be added in front of these routes first.

The `route_id` of a saved route is a different thing from the `route_id` inside its payload: the payload
keeps the generator's uuid4 for traceability, while the outer integer is the persistent identity.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Path, Query, Response
from sqlalchemy.exc import SQLAlchemyError

from server.catalog import CatalogError, load_places
from server.database import (
    SavedRoute,
    get_saved_route,
    list_saved_routes,
    save_route,
    session_scope,
)
from server.schemas import (
    BIGINT_MAX,
    DATABASE_UNAVAILABLE_RESPONSE,
    ApiError,
    PAGINATION_HEADERS,
    RouteResponse,
    SaveRouteRequest,
    SavedRouteResponse,
)

router = APIRouter(tags=["Saved routes"])

# Two shapes on one endpoint, and the contract has to say so: FastAPI's error list for a malformed
# body, a plain sentence for ids the catalog never knew.
REJECTED_BODY_RESPONSE = {
    "description": (
        "Тело не проходит проверку: список ошибок валидации либо строка с перечислением "
        "неизвестных каталогу идентификаторов мест"
    ),
    "content": {
        "application/json": {
            "schema": {
                "oneOf": [
                    {"$ref": "#/components/schemas/ApiError"},
                    {"$ref": "#/components/schemas/HTTPValidationError"},
                ]
            }
        }
    },
}

ROUTE_NOT_FOUND_RESPONSE = {
    "model": ApiError,
    "description": "Сохранённого маршрута с таким идентификатором нет",
}


def _known_place_ids() -> set[str]:
    try:
        return {place.id for place in load_places()}
    except CatalogError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


def _as_response(row: SavedRoute) -> SavedRouteResponse:
    return SavedRouteResponse(
        route_id=row.route_id,
        user_id=row.user_id,
        title=row.title,
        city=row.city,
        total_cost=row.total_cost,
        stop_count=row.stop_count,
        created_at=row.created_at,
        route=RouteResponse.model_validate_json(row.payload),
    )


@router.post(
    "/routes",
    response_model=SavedRouteResponse,
    status_code=201,
    summary="Сохранить собранный маршрут",
    description=(
        "Тело — ответ `POST /routes/generate` без изменений, плюс необязательный `user_id`. "
        "Идентификатор выдаёт база: присланный внутри маршрута uuid4 остаётся только в полезной "
        "нагрузке, для обращения к записи он не годится."
    ),
    responses={
        201: {"description": "Запись создана и лежит в базе"},
        422: REJECTED_BODY_RESPONSE,
        503: DATABASE_UNAVAILABLE_RESPONSE,
    },
)
async def create_saved_route(request: SaveRouteRequest) -> SavedRouteResponse:
    """A route is stored as the JSON the generator produced, so a fetch cannot disagree with a generate.

    Place ids are checked against the catalog on the way in: an id that never existed means a hand-made
    body, and saving it would produce a route no client can render. A place that leaves the catalog
    later is not retroactively invalid — the payload is a snapshot, not a live join.
    """
    known = _known_place_ids()
    unknown = [place.id for place in request.places if place.id not in known]
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"В каталоге нет мест с идентификаторами: {', '.join(unknown)}",
        )

    route = RouteResponse(**request.model_dump(exclude={"user_id"}))
    try:
        async with session_scope() as session:
            row = await save_route(
                session,
                city=route.city,
                title=route.title,
                payload=route.model_dump_json(),
                total_cost=route.total_cost,
                stop_count=len(route.places),
                user_id=request.user_id,
            )
            return _as_response(row)
    except SQLAlchemyError as error:
        raise HTTPException(status_code=503, detail="База данных не отвечает") from error


@router.get(
    "/routes",
    response_model=list[SavedRouteResponse],
    summary="Сохранённые маршруты",
    description=(
        "Свежие сверху. `user_id` фильтрует по заявленному владельцу — это удобство, а не защита: "
        "без него видно чужие сохранённые маршруты тоже. Всего подходящих записей — в X-Total-Count."
    ),
    responses={
        200: {"description": "Список сохранённых маршрутов", "headers": PAGINATION_HEADERS},
        503: DATABASE_UNAVAILABLE_RESPONSE,
    },
)
async def read_saved_routes(
    response: Response,
    user_id: int | None = Query(
        None, ge=1, le=BIGINT_MAX, description="Только маршруты этого владельца"
    ),
    limit: int | None = Query(None, ge=1, le=200, description="Сколько записей вернуть"),
    offset: int = Query(0, ge=0, description="Пропустить N первых записей"),
) -> list[SavedRouteResponse]:
    """Empty is `200 []` here as well: the collection exists, this visitor just saved nothing."""
    try:
        async with session_scope() as session:
            rows, total = await list_saved_routes(
                session, user_id=user_id, limit=limit, offset=offset
            )
    except SQLAlchemyError as error:
        raise HTTPException(status_code=503, detail="База данных не отвечает") from error

    response.headers["X-Total-Count"] = str(total)
    response.headers["X-Offset"] = str(offset)
    return [_as_response(row) for row in rows]


@router.get(
    "/routes/{route_id}",
    response_model=SavedRouteResponse,
    summary="Карточка сохранённого маршрута",
    responses={404: ROUTE_NOT_FOUND_RESPONSE, 503: DATABASE_UNAVAILABLE_RESPONSE},
)
async def read_saved_route(
    route_id: int = Path(..., ge=1, le=BIGINT_MAX, description="Идентификатор записи, выдаёт база"),
) -> SavedRouteResponse:
    try:
        async with session_scope() as session:
            row = await get_saved_route(session, route_id)
    except SQLAlchemyError as error:
        raise HTTPException(status_code=503, detail="База данных не отвечает") from error

    if row is None:
        raise HTTPException(status_code=404, detail=f"Маршрут {route_id} не найден")
    return _as_response(row)

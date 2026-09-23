"""Route generation endpoint."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException

from server.catalog import CatalogError, load_places
from server.routing import assemble_route, filter_candidates
from server.schemas import RouteRequest, RouteResponse

router = APIRouter(tags=["Routes"])


@router.post(
    "/routes/generate",
    response_model=RouteResponse,
    summary="Собрать маршрут выходного дня",
)
async def generate_route(request: RouteRequest) -> RouteResponse:
    try:
        places = list(load_places())
    except CatalogError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error

    candidates = filter_candidates(places, request)
    if not candidates:
        raise HTTPException(
            status_code=404,
            detail="No places match the requested city, categories, budget or duration",
        )

    stops, total_minutes, total_cost = assemble_route(candidates, request)
    return RouteResponse(
        route_id=str(uuid.uuid4()),
        title=f"Маршрут выходного дня: {request.city}",
        city=request.city,
        total_duration_hours=round(total_minutes / 60, 2),
        total_cost=round(total_cost, 2),
        places=[stop.place for stop in stops],
        stops=stops,
    )

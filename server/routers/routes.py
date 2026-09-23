"""Route generation endpoints.

The selection logic here is the placeholder from the API skeleton (issue #2); the proximity-aware
algorithm lands in issue #4.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException

from server.catalog import CatalogError, load_places
from server.schemas import Place, RouteRequest, RouteResponse

router = APIRouter(tags=["Routes"])


def _filter_places(places: list[Place], request: RouteRequest) -> list[Place]:
    wanted_city = request.city.strip().lower()
    results = [place for place in places if place.city.lower() == wanted_city]
    if request.categories:
        wanted = {category.strip().lower() for category in request.categories}
        results = [place for place in results if place.category.lower() in wanted]
    if request.is_pushkin_card_only:
        results = [place for place in results if place.is_pushkin_card]
    if request.max_budget is not None:
        results = [place for place in results if place.price <= request.max_budget]
    return sorted(results, key=lambda place: place.rating, reverse=True)


@router.post("/routes/generate", response_model=RouteResponse, summary="Собрать маршрут выходного дня")
async def generate_route(request: RouteRequest) -> RouteResponse:
    try:
        places = list(load_places())
    except CatalogError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error

    candidates = _filter_places(places, request)
    if not candidates:
        raise HTTPException(status_code=404, detail="No places match the requested filters")

    budget_minutes = int(request.duration_hours * 60)
    selected: list[Place] = []
    spent_minutes = 0
    for place in candidates:
        if spent_minutes + place.visit_duration_minutes > budget_minutes:
            continue
        selected.append(place)
        spent_minutes += place.visit_duration_minutes

    return RouteResponse(
        route_id=str(uuid.uuid4()),
        title=f"Маршрут выходного дня: {request.city}",
        city=request.city,
        total_duration_hours=round(spent_minutes / 60, 2),
        total_cost=round(sum(place.price for place in selected), 2),
        places=selected,
    )

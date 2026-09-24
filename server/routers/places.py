"""Read-only endpoints exposing the places catalog."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from server.catalog import CatalogError, city_matches, known_categories, load_places
from server.schemas import (
    CATALOG_UNAVAILABLE_RESPONSE,
    PLACE_NOT_FOUND_RESPONSE,
    Place,
)

router = APIRouter(tags=["Places"])


def _places_or_503() -> tuple[Place, ...]:
    try:
        return load_places()
    except CatalogError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


@router.get(
    "/places",
    response_model=list[Place],
    summary="Список достопримечательностей",
    responses={503: CATALOG_UNAVAILABLE_RESPONSE},
)
async def list_places(
    city: str | None = Query(None, description="Фильтр по городу, подходит краткая форма — «Ростов»"),
    category: str | None = Query(None, description="Фильтр по категории"),
    is_pushkin_card: bool | None = Query(None, description="Только по Пушкинской карте"),
    max_price: float | None = Query(None, ge=0, description="Максимальная стоимость, руб."),
) -> list[Place]:
    """Пустая выборка — это `200 []`, а не ошибка: коллекция по фильтру пуста, сама она существует.
    Исключение — `POST /routes/generate`, где нечего собирать, поэтому отвечает 404."""
    results = list(_places_or_503())
    if city:
        results = [place for place in results if city_matches(city, place.city)]
    if category:
        wanted_category = category.strip().lower()
        results = [place for place in results if place.category.lower() == wanted_category]
    if is_pushkin_card is not None:
        results = [place for place in results if place.is_pushkin_card == is_pushkin_card]
    if max_price is not None:
        results = [place for place in results if place.price <= max_price]
    return results


@router.get(
    "/places/{place_id}",
    response_model=Place,
    summary="Карточка места",
    responses={404: PLACE_NOT_FOUND_RESPONSE, 503: CATALOG_UNAVAILABLE_RESPONSE},
)
async def get_place(place_id: str) -> Place:
    for place in _places_or_503():
        if place.id == place_id:
            return place
    raise HTTPException(status_code=404, detail=f"Место {place_id!r} не найдено")


@router.get(
    "/categories",
    response_model=list[str],
    summary="Доступные категории мест",
    responses={503: CATALOG_UNAVAILABLE_RESPONSE},
)
async def list_categories() -> list[str]:
    try:
        return list(known_categories())
    except CatalogError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error

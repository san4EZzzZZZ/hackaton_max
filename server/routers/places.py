"""Read-only endpoints exposing the places catalog."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Response

from server.catalog import (
    CatalogError,
    city_matches,
    known_categories,
    known_cities,
    load_places,
    search_matches,
)
from server.routing import within_radius_km
from server.schemas import (
    CATALOG_UNAVAILABLE_RESPONSE,
    PAGINATION_HEADERS,
    PLACE_NOT_FOUND_RESPONSE,
    CitySummary,
    Location,
    Place,
)

router = APIRouter(tags=["Places"])

# `radius_km` without a center is a combination of individually valid parameters, so it is raised by
# hand with a plain message instead of FastAPI's error list. Both shapes can occur on this endpoint,
# which is what the contract has to say.
BAD_COMBINATION_RESPONSE = {
    "description": (
        "Некорректный параметр либо их комбинация: тело — список ошибок валидации, а для `radius_km` "
        "без центра и неполного центра — строка с объяснением"
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


def _places_or_503() -> tuple[Place, ...]:
    try:
        return load_places()
    except CatalogError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


def _center_or_none(near_lat: float | None, near_lon: float | None) -> Location | None:
    if (near_lat is None) != (near_lon is None):
        raise HTTPException(
            status_code=422, detail="near_lat и near_lon задаются только парой"
        )
    if near_lat is None or near_lon is None:
        return None
    return Location(lat=near_lat, lon=near_lon)


@router.get(
    "/places",
    response_model=list[Place],
    summary="Список достопримечательностей",
    description=(
        "Фильтры комбинируются между собой. Порядок ответа — порядок в каталоге, радиус ничего не "
        "пересортировывает. Сколько объектов подошло до limit/offset — в заголовке X-Total-Count."
    ),
    responses={
        200: {"description": "Каталог, отфильтрованный по запросу", "headers": PAGINATION_HEADERS},
        422: BAD_COMBINATION_RESPONSE,
        503: CATALOG_UNAVAILABLE_RESPONSE,
    },
)
async def list_places(
    response: Response,
    city: str | None = Query(None, description="Фильтр по городу, подходит краткая форма — «Ростов»"),
    category: str | None = Query(None, description="Фильтр по категории"),
    is_pushkin_card: bool | None = Query(None, description="Только по Пушкинской карте"),
    max_price: float | None = Query(None, ge=0, description="Максимальная стоимость, руб."),
    q: str | None = Query(
        None, min_length=2, description="Поиск по названию, описанию, адресу и категории"
    ),
    min_rating: float | None = Query(None, ge=0, le=5, description="Минимальный рейтинг"),
    near_lat: float | None = Query(None, ge=-90, le=90, description="Широта центра поиска"),
    near_lon: float | None = Query(None, ge=-180, le=180, description="Долгота центра поиска"),
    radius_km: float | None = Query(None, gt=0, le=200, description="Радиус от центра, км"),
    limit: int | None = Query(None, ge=1, le=200, description="Сколько объектов вернуть"),
    offset: int = Query(0, ge=0, description="Пропустить N первых объектов"),
) -> list[Place]:
    """Пустая выборка — это `200 []`, а не ошибка: коллекция по фильтру пуста, сама она существует.
    Исключение — `POST /routes/generate`, где нечего собирать, поэтому отвечает 404."""
    center = _center_or_none(near_lat, near_lon)
    if center is None and radius_km is not None:
        raise HTTPException(
            status_code=422, detail="radius_km задаётся только вместе с near_lat и near_lon"
        )

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
    if q:
        results = [place for place in results if search_matches(place, q)]
    if min_rating is not None:
        results = [place for place in results if place.rating >= min_rating]
    if center is not None and radius_km is not None:
        results = [place for place in results if within_radius_km(place.location, center, radius_km)]

    response.headers["X-Total-Count"] = str(len(results))
    response.headers["X-Offset"] = str(offset)
    if limit is not None:
        return results[offset : offset + limit]
    return results[offset:]


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


@router.get(
    "/cities",
    response_model=list[CitySummary],
    summary="Города, представленные в каталоге",
    responses={503: CATALOG_UNAVAILABLE_RESPONSE},
)
async def list_cities() -> list[CitySummary]:
    """Города, для которых в данных реально есть объекты, — селектор города не предлагает пустой выбор."""
    try:
        grouped = known_cities()
    except CatalogError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error

    return [
        CitySummary(
            city=city,
            place_count=len(places),
            categories=list(dict.fromkeys(place.category for place in places)),
        )
        for city, places in grouped.items()
    ]

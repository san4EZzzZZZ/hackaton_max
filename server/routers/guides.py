"""«Справка по пути»: что это за место, зачем на него смотрят и что видно рядом."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from server.catalog import city_matches
from server.guides import GuideError, guide_for, load_guides, nearby_for
from server.routers.places import places_or_503
from server.schemas import (
    GUIDE_MISSING_RESPONSE,
    GUIDE_UNAVAILABLE_RESPONSE,
    ApiError,
    GuideRecord,
    GuideSummary,
    Place,
    PlaceGuide,
)

router = APIRouter(tags=["Guides"])


def _guides_or_503() -> tuple[GuideRecord, ...]:
    try:
        return load_guides()
    except GuideError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


def _paired() -> list[tuple[Place, GuideRecord]]:
    """Every guide with the catalog place it belongs to.

    A guide whose `place_id` no longer resolves is a broken data file rather than a missing record:
    answering it would mean either inventing a title or quietly dropping a badge the client was told
    to draw, so the whole feature reports itself unavailable instead.
    """
    catalog = {place.id: place for place in places_or_503()}
    paired: list[tuple[Place, GuideRecord]] = []
    for guide in _guides_or_503():
        place = catalog.get(guide.place_id)
        if place is None:
            raise HTTPException(
                status_code=503,
                detail=(
                    f"Справка для {guide.place_id!r} ссылается на место, "
                    "которого нет в каталоге"
                ),
            )
        paired.append((place, guide))
    return paired


@router.get(
    "/guides",
    response_model=list[GuideSummary],
    summary="Места, для которых справка уже написана",
    description=(
        "Указатель к GET /places/{place_id}/guide: плашку «📖» вешают на этот список, а не на 404. "
        "Отсутствующая справка — не ошибка, а место, очередь которого ещё не дошла."
    ),
    responses={503: GUIDE_UNAVAILABLE_RESPONSE},
)
async def list_guides(
    city: str | None = Query(
        None, description="Фильтр по городу, подходит краткая форма — «Ростов»"
    ),
) -> list[GuideSummary]:
    """Order is the order of `data/place_guides.json`, which is the editor's priority list."""
    summaries: list[GuideSummary] = []
    for place, guide in _paired():
        if city and not city_matches(city, place.city):
            continue
        summaries.append(
            GuideSummary(
                place_id=place.id,
                title=place.title,
                category=place.category,
                city=place.city,
                status=guide.status,
                cover_url=place.image_url,
                media_count=len(guide.media),
                last_verified=guide.provenance.last_verified,
            )
        )
    return summaries


@router.get(
    "/places/{place_id}/guide",
    response_model=PlaceGuide,
    summary="Справка по месту",
    description=(
        "Экран чтения для одной точки маршрута. `price`, `working_hours` и `title` приходят из "
        "каталога, а не из текста справки, поэтому не могут с ним разойтись; `nearby` считает тот же "
        "пеший ход, что и маршрутизатор."
    ),
    responses={
        404: GUIDE_MISSING_RESPONSE,
        503: {
            "model": ApiError,
            "description": (
                "Файл справок или файл каталога недоступны либо повреждены; в `detail` назван тот, "
                "который не читается"
            ),
        },
    },
)
async def get_place_guide(place_id: str) -> PlaceGuide:
    catalog = places_or_503()
    place = next((item for item in catalog if item.id == place_id), None)
    if place is None:
        raise HTTPException(status_code=404, detail=f"Место {place_id!r} не найдено")
    try:
        guide = guide_for(place_id)
    except GuideError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    if guide is None:
        raise HTTPException(
            status_code=404,
            detail=f"Справки по месту {place.title!r} ещё нет — она пишется по очереди",
        )

    return PlaceGuide(
        **guide.model_dump(),
        title=place.title,
        category=place.category,
        city=place.city,
        location=place.location,
        price=place.price,
        working_hours=place.working_hours,
        visit_duration_minutes=place.visit_duration_minutes,
        nearby=nearby_for(place, catalog),
    )

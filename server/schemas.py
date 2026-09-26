"""Public JSON contract of the places & routes API consumed by the Mini App."""

from __future__ import annotations

from datetime import datetime

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator


class ApiModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore", str_strip_whitespace=True)


class ApiError(ApiModel):
    """Body of every non-2xx answer these endpoints return."""

    detail: str = Field(..., description="Что пошло не так")


# Declared on the route decorators so the codes land in /openapi.json and clients can be generated
# against them instead of guessing from an "Undocumented" status.
NOT_FOUND_RESPONSE = {"model": ApiError, "description": "Ни один объект не подошёл под фильтры"}
PLACE_NOT_FOUND_RESPONSE = {"model": ApiError, "description": "Места с таким идентификатором нет"}
CATALOG_UNAVAILABLE_RESPONSE = {
    "model": ApiError,
    "description": "Файл каталога недоступен или повреждён",
}
DATABASE_UNAVAILABLE_RESPONSE = {
    "model": ApiError,
    "description": "База данных не отвечает",
}

# SQLite stores integers in 8 bytes and raises OverflowError past this bound instead of answering, so
# ids are capped at validation: an out-of-range id is a client mistake, not a 500.
BIGINT_MAX = 9_223_372_036_854_775_807

# Page totals travel as headers, not as a JSON envelope: `/places` answers a bare array today, and
# wrapping it would break the Mini App that is already wired to that shape.
PAGINATION_HEADERS = {
    "X-Total-Count": {
        "description": "Сколько объектов подошло под фильтры, до применения limit/offset",
        "schema": {"type": "integer", "minimum": 0},
    },
    "X-Offset": {
        "description": "Смещение, с которого взята текущая страница",
        "schema": {"type": "integer", "minimum": 0},
    },
}


class Location(ApiModel):
    lat: float = Field(..., ge=-90, le=90, description="Широта")
    lon: float = Field(..., ge=-180, le=180, description="Долгота")


class Place(ApiModel):
    id: str = Field(..., description="Уникальный идентификатор места")
    title: str = Field(..., description="Название места или события")
    description: str | None = Field(None, description="Краткое описание")
    category: str = Field(..., description="Категория объекта, см. GET /categories")
    location: Location
    address: str | None = Field(None, description="Адрес")
    city: str = Field("Ростов-на-Дону", description="Город")
    is_pushkin_card: bool = Field(False, description="Доступно по Пушкинской карте")
    price: float = Field(0.0, ge=0, description="Стоимость посещения, руб.")
    working_hours: str | None = Field(None, description="Часы работы")
    rating: float = Field(5.0, ge=0, le=5, description="Рейтинг 0..5")
    visit_duration_minutes: int = Field(60, ge=15, description="Рекомендуемая длительность визита")
    image_url: str | None = Field(None, description="Ссылка на изображение")


class CitySummary(ApiModel):
    """One city present in the catalog; the seed data holds a single one for now."""

    city: str = Field(..., description="Название города")
    place_count: int = Field(..., ge=1, description="Число объектов в этом городе")
    categories: list[str] = Field(..., description="Категории этих объектов")


class RouteRequest(ApiModel):
    # Unknown keys are rejected, not dropped: a silently ignored `budget` is indistinguishable from
    # an ignored budget limit on the response side (issue #14).
    model_config = ConfigDict(populate_by_name=True, extra="forbid", str_strip_whitespace=True)

    city: str = Field(
        ..., min_length=2, description="Город; допускается краткая форма — «Ростов»"
    )
    categories: list[str] = Field(default_factory=list, description="Фильтр по категориям")
    max_budget: float | None = Field(
        None,
        ge=0,
        validation_alias=AliasChoices("max_budget", "budget"),
        description="Максимальная суммарная стоимость маршрута, руб. Принимается и под именем `budget`",
    )
    is_pushkin_card_only: bool = Field(False, description="Только места по Пушкинской карте")
    duration_hours: float = Field(4.0, gt=0, le=12, description="Желаемая длительность, ч.")

    @field_validator("categories")
    @classmethod
    def _drop_blanks(cls, value: list[str]) -> list[str]:
        return [item.strip() for item in value if item.strip()]


class RouteStop(ApiModel):
    """One point of an assembled route with its place in the timeline."""

    place: Place
    order: int = Field(..., ge=1, description="Позиция точки в маршруте")
    arrival_offset_minutes: int = Field(..., ge=0, description="Момент прибытия от начала, мин.")
    visit_duration_minutes: int = Field(..., ge=15, description="Время на точку, мин.")
    travel_minutes_from_prev: int = Field(0, ge=0, description="Переход от предыдущей точки, мин.")


class RouteResponse(ApiModel):
    route_id: str
    title: str
    city: str
    total_duration_hours: float = Field(..., ge=0, description="Визиты и переходы между точками")
    total_cost: float = Field(..., ge=0, description="Суммарная стоимость посещения, руб.")
    places: list[Place] = Field(default_factory=list, description="Точки в порядке визита")
    stops: list[RouteStop] = Field(
        default_factory=list,
        description="Те же точки с таймингами переходов; пуст, если маршрут не собран",
    )


class SaveRouteRequest(RouteResponse):
    """Ответ генератора, отправленный обратно без изменений, плюс необязательный владелец.

    Наследование держит обещание буквальный: сохраняется ровно то, что вернул
    `POST /routes/generate`. Неизвестные ключи отвергаются по той же причине, что и у `RouteRequest`.
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid", str_strip_whitespace=True)

    user_id: int | None = Field(
        None,
        ge=1,
        le=BIGINT_MAX,
        description="Владелец маршрута. Не аутентифицируется: см. README, раздел про сохранённые маршруты",
    )


class SavedRouteResponse(ApiModel):
    """Сохранённый маршрут: исходный ответ генератора под собственным постоянным идентификатором."""

    route_id: int = Field(..., description="Идентификатор записи, выдаёт база")
    user_id: int | None = Field(None, description="Владелец, если его передали при сохранении")
    title: str
    city: str
    total_cost: float = Field(..., ge=0, description="Стоимость маршрута на момент сохранения, руб.")
    stop_count: int = Field(..., ge=0, description="Число точек в сохранённом маршруте")
    created_at: datetime = Field(..., description="Момент сохранения, UTC")
    route: RouteResponse = Field(..., description="Ответ генератора ровно так, как он сохранён")

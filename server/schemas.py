"""Public JSON contract of the places & routes API consumed by the Mini App."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ApiModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore", str_strip_whitespace=True)


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


class RouteRequest(ApiModel):
    city: str = Field(..., min_length=2, description="Город")
    categories: list[str] = Field(default_factory=list, description="Фильтр по категориям")
    max_budget: float | None = Field(None, ge=0, description="Максимальный бюджет, руб.")
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

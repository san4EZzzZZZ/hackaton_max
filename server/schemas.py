"""Public JSON contract of the places & routes API consumed by the Mini App."""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


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
GUIDE_UNAVAILABLE_RESPONSE = {
    "model": ApiError,
    "description": "Файл справок недоступен или повреждён",
}
# One status code for two different mistakes, because the client's screen is the same either way;
# the `detail` says which one it is.
GUIDE_MISSING_RESPONSE = {
    "model": ApiError,
    "description": "Места с таким идентификатором нет, либо справка для него ещё не написана",
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
    image_url: str | None = Field(
        None,
        description="Миниатюра с Wikimedia Commons, ссылка на чужой хост; null — подходящего фото нет",
    )


class CitySummary(ApiModel):
    """One city present in the catalog; the seed data holds a single one for now."""

    city: str = Field(..., description="Название города")
    place_count: int = Field(..., ge=1, description="Число объектов в этом городе")
    categories: list[str] = Field(..., description="Категории этих объектов")


# «Справка по пути»: текст пишет редактор в data/place_guides.json, всё остальное приходит из каталога.

_HistoryParagraph = Annotated[
    str,
    Field(min_length=120, description="Абзац истории места"),
]
_Highlight = Annotated[
    str,
    Field(min_length=12, description="Короткий факт, который проверяется прямо на месте"),
]

GuideStatus = Literal["ready", "seed"]


class GuideMedia(ApiModel):
    """Один снимок справки. Оба адреса ведут на Wikimedia Commons — чужие хосты сервер не раздаёт."""

    url: str = Field(..., description="Оригинал файла на Commons")
    thumb_url: str = Field(..., description="Миниатюра того же файла — показывать клиенту её")
    caption: str = Field(..., min_length=3, description="Что на снимке")
    credit: str = Field(..., min_length=2, description="Автор, как указан на странице файла")
    license: str = Field(..., min_length=2, description="Лицензия, например CC BY-SA 4.0")
    license_url: str | None = Field(
        None,
        description="Страница лицензии; null только у public domain, где ссылки на лицензию нет",
    )

    @field_validator("url", "thumb_url")
    @classmethod
    def _served_by_wikimedia(cls, value: str) -> str:
        """Rejected while the file loads, so a stray host turns into a 503 rather than a hot link.

        The rule is the same one the catalog images obey: the API never points a client at a host the
        project does not control, and it never needs an outbound request of its own to answer.
        """
        host = urlsplit(value).netloc.lower()
        if not value.startswith("https://") or not (
            host == "wikimedia.org" or host.endswith(".wikimedia.org")
        ):
            raise ValueError("иллюстрация обязана лежать на https://*.wikimedia.org")
        return value

    @model_validator(mode="after")
    def _a_license_needs_a_page(self) -> "GuideMedia":
        if self.license_url is None and not self.license.strip().lower().startswith("public domain"):
            raise ValueError(f"{self.caption!r}: у лицензии {self.license!r} обязан быть адрес")
        return self


class GuideProvenance(ApiModel):
    """Чем справка подтверждена и где именно редактор взял факты."""

    source_url: str | None = Field(
        None, description="Страница источника; без неё справка считается неподтверждённой"
    )
    last_verified: date | None = Field(
        None, description="Дата, когда редактор сверил запись с источником"
    )
    verified_fields: list[str] = Field(
        default_factory=list,
        description="Поля записи, сверенные с этим источником (history, highlights, media)",
    )

    @model_validator(mode="after")
    def _a_date_needs_a_source(self) -> "GuideProvenance":
        if self.last_verified is not None and not self.source_url:
            raise ValueError("last_verified без source_url: дату сверки ставят только вместе с источником")
        return self


class GuideRecord(ApiModel):
    """Запись `data/place_guides.json` ровно в том виде, в каком её оставляет редактор."""

    place_id: str = Field(..., description="Идентификатор места из data/places.json")
    status: GuideStatus = Field(
        "seed", description="ready — справка полная, seed — заготовка, которой делятся честно"
    )
    history: list[_HistoryParagraph] = Field(
        ..., min_length=2, max_length=3, description="2–3 абзаца истории"
    )
    highlights: list[_Highlight] = Field(
        ..., min_length=3, max_length=5, description="3–5 фактов для человека на месте"
    )
    media: list[GuideMedia] = Field(default_factory=list, description="Снимки; первым идёт обложка")
    media_note: str | None = Field(
        None,
        description="Почему снимков нет. Обязателен, если media пусто: пустая подборка не должна выглядеть решением",
    )
    provenance: GuideProvenance

    @model_validator(mode="after")
    def _absent_media_is_explained(self) -> "GuideRecord":
        if not self.media and not (self.media_note or "").strip():
            raise ValueError(f"{self.place_id}: у пустого media обязателен media_note")
        return self

    @model_validator(mode="after")
    def _verified_fields_are_fields_of_the_record(self) -> "GuideRecord":
        # Checked against the file's own keys, not the response's: `title` and the rest of the echoes
        # come from the catalog, so no guide source can vouch for them.
        unknown = set(self.provenance.verified_fields) - set(GuideRecord.model_fields)
        if unknown:
            raise ValueError(f"{self.place_id}: в verified_fields нет таких полей — {sorted(unknown)}")
        return self


class NearbyPlace(ApiModel):
    """Соседняя точка: экран справки обещает «рядом», и эти же минуты показывает маршрут."""

    id: str = Field(..., description="Идентификатор места — по нему просят и его справку")
    title: str = Field(..., description="Название места")
    category: str = Field(..., description="Категория объекта")
    distance_m: int = Field(..., ge=0, description="По прямой от этого места, м")
    travel_minutes: int = Field(
        ...,
        ge=1,
        description=(
            "Переход пешком в том же исчислении, что и `travel_minutes_from_prev` у маршрута: оценка "
            "планировщика, а не навигатора"
        ),
    )


class PlaceGuide(GuideRecord):
    """Справка для клиента: редакторский текст плюс поля, эхом взятые из каталога, и соседи."""

    title: str = Field(..., description="Название места из каталога")
    category: str = Field(..., description="Категория объекта из каталога")
    city: str = Field(..., description="Город из каталога")
    location: Location
    price: float = Field(..., ge=0, description="Стоимость посещения, руб. — то же число, что в карточке места")
    working_hours: str | None = Field(None, description="Часы работы — тоже эхом из каталога")
    visit_duration_minutes: int = Field(
        ..., ge=15, description="Время на точку, которое под это место закладывает маршрутизатор"
    )
    nearby: list[NearbyPlace] = Field(
        default_factory=list, description="Ближайшие к этому месту объекты каталога, по расстоянию"
    )


class GuideSummary(ApiModel):
    """Указатель на справку: плашку «📖» вешают на него, а не проверяют 404 на каждом месте."""

    place_id: str = Field(..., description="Идентификатор места — адрес GET /places/{place_id}/guide")
    title: str = Field(..., description="Название места из каталога")
    category: str = Field(..., description="Категория объекта")
    city: str = Field(..., description="Город")
    status: GuideStatus = Field(..., description="ready или seed")
    cover_url: str | None = Field(
        None, description="Обложка — `image_url` места; второй ссылки на картинку у клиента нет"
    )
    media_count: int = Field(..., ge=0, description="Сколько снимков лежит в справке")
    last_verified: date | None = Field(None, description="Дата последней сверки текста с источником")


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
    travel_minutes_from_prev: int = Field(
        0,
        ge=0,
        description=(
            "Переход от предыдущей точки, мин. Оценка планировщика (пеший ход плюс запас на "
            "ожидание), а не ETA навигатора: путь по тротуарам всегда дольше"
        ),
    )
    distance_m_from_prev: int = Field(
        0,
        ge=0,
        description=(
            "Расстояние от предыдущей точки по прямой, м; у первой точки 0. Длина тротуарного обхода "
            "всегда больше, поэтому подпись «N км пешком» честнее читать «N км напрямую»"
        ),
    )


class RouteResponse(ApiModel):
    route_id: str
    title: str
    city: str
    total_duration_hours: float = Field(..., ge=0, description="Визиты и переходы между точками")
    total_duration_minutes: int = Field(
        0,
        ge=0,
        description="То же число в минутах, без округления до сотых часа",
    )
    total_distance_m: int = Field(
        0,
        ge=0,
        description="Сумма прямых расстояний между соседними точками, м",
    )
    slack_minutes: int | None = Field(
        None,
        ge=0,
        description=(
            "Незанятые минуты из заказанной длительности. Запас не резервируется: маршрут плотный, "
            "но эти минуты уходят на темп прогулки и случайные остановки"
        ),
    )
    total_cost: float = Field(..., ge=0, description="Суммарная стоимость посещения, руб.")
    places: list[Place] = Field(default_factory=list, description="Точки в порядке визита")
    stops: list[RouteStop] = Field(
        default_factory=list,
        description="Те же точки с таймингами переходов; пуст, если маршрут не собран",
    )

    # Both aggregates are derived from `stops` rather than trusted from the caller. A route saved
    # before these fields existed comes back through SaveRouteRequest without them, and replaying it
    # must rebuild the numbers instead of failing validation or storing zeros.
    @model_validator(mode="after")
    def _recount_from_stops(self) -> "RouteResponse":
        if self.stops:
            last = self.stops[-1]
            self.total_duration_minutes = last.arrival_offset_minutes + last.visit_duration_minutes
            self.total_distance_m = sum(stop.distance_m_from_prev for stop in self.stops)
        return self


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

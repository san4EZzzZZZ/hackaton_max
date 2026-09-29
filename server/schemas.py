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

# Admin ingestion answers. The three codes are three different operator actions — set a token, retry
# later, or fix the header — and collapsing them into one would send the reader to the wrong place.
INGEST_DISABLED_RESPONSE = {
    "model": ApiError,
    "description": "INGEST_TOKEN не задан: автокаталог выключен на этом сервере",
}
INGEST_UNAUTHORIZED_RESPONSE = {
    "model": ApiError,
    "description": "Заголовок с токеном не передан или не совпадает",
}
INGEST_BUSY_RESPONSE = {"model": ApiError, "description": "Прогон уже идёт"}

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


# «Чем хочешь заняться?» is not a category: кофейня остаётся кофейней по типу, но берётся в подборку
# за кофе. Словарь закрыт — только так /chips может обещать ненулевой счётчик у каждого чипа, а
# опечатка в теге не превращается в молча расширенный фильтр.
TagId = Literal["coffee", "culture", "walk", "food", "photo"]


class Place(ApiModel):
    id: str = Field(..., description="Уникальный идентификатор места")
    title: str = Field(..., description="Название места или события")
    description: str | None = Field(None, description="Краткое описание")
    category: str = Field(..., description="Категория объекта, см. GET /categories")
    tags: list[TagId] = Field(
        default_factory=list,
        description=(
            "Зачем сюда идут: кофе, культура, прогулка, перекус, фото. id те же, что у `Chip.id` "
            "в GET /chips; пустой список — место вне настроенческих подборок"
        ),
    )
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


# --- Автокаталог (ingest) ---------------------------------------------------------------
# These models mirror `ingest.pipeline.CityReport`, which is a dataclass in the ingester. The copy is
# on purpose: the API contract has to stay readable without importing the collector, and a field
# renamed on one side fails `tests/test_ingest_api.py` rather than silently changing the answer.


class IngestRequest(ApiModel):
    """Запуск сбора мест. Неизвестные ключи отвергаются: молча проигнорированный город обиднее ошибки."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid", str_strip_whitespace=True)

    cities: list[str] = Field(
        ...,
        min_length=1,
        max_length=10,
        description=(
            "Города по именам, как их понимает Nominatim: «Казань», «Saint Petersburg». Больше десяти "
            "за раз не просить — прогон держит занятый слот на общем Overpass"
        ),
    )
    limit: int | None = Field(
        None, ge=1, le=200, description="Максимум мест на город; по умолчанию INGEST_MAX_PLACES_PER_CITY"
    )
    dry_run: bool = Field(
        False,
        description="Посчитать и показать, ничего не записывать. Отчёт при этом тот же, только без `written`",
    )

    @field_validator("cities")
    @classmethod
    def _drop_blanks(cls, value: list[str]) -> list[str]:
        cleaned = [city.strip() for city in value if city.strip()]
        if not cleaned:
            raise ValueError("cities: нужен хотя бы один непустой город")
        return cleaned


class IngestReport(ApiModel):
    """Итог обработки одного города."""

    requested: str = Field(..., description="Имя, как его просили")
    city: str | None = Field(None, description="Как город называется в каталоге после распознавания; null — не найден")
    source_url: str | None = Field(None, description="Объект города в OpenStreetMap")
    fetched: dict[str, int] = Field(
        default_factory=dict, description="Сколько элементов OSM найдено по каждой категории"
    )
    published: dict[str, int] = Field(
        default_factory=dict, description="Сколько мест вошло в файл города по каждой категории"
    )
    places: int = Field(0, ge=0, description="Итоговое число мест")
    written: str | None = Field(
        None, description="Путь к файлу города; null для dry-run и для города, по которому нечего записывать"
    )
    failures: list[str] = Field(
        default_factory=list, description="Категории и города, которые не удались; пуст, если всё прошло"
    )


class IngestStatus(ApiModel):
    """Состояние сборщика: идёт ли прогон сейчас и чем закончился предыдущий."""

    enabled: bool = Field(..., description="Настроен ли INGEST_TOKEN; без него запуска здесь нет")
    running: bool = Field(..., description="Есть ли прогон прямо сейчас")
    started_at: datetime | None = Field(None, description="Начало текущего или последнего прогона, UTC")
    finished_at: datetime | None = Field(None, description="Конец того же прогона, UTC")
    reports: list[IngestReport] = Field(
        default_factory=list, description="Отчёты последнего прогона, по одному на город"
    )
    error: str | None = Field(None, description="Почему прогон упал целиком; null, если не падал")


class IngestAccepted(ApiModel):
    """Ответ запуска: принят ли запрос и что с ним будет дальше."""

    status: Literal["started", "already_running"] = Field(
        ..., description="already_running — новый прогон не начат, потому что идёт старый"
    )
    cities: list[str] = Field(..., description="Города этого запроса")
    running_since: datetime | None = Field(None, description="Когда началась текущая обработка")


class Chip(ApiModel):
    """Один чип «Чем хочешь заняться?» — вместе с тем, что под него действительно лежит."""

    id: TagId = Field(..., description="Идентификатор чипа; его клиент и шлёт в `tags` запроса маршрута")
    label: str = Field(..., description="Подпись на чипе")
    emoji: str = Field(..., description="Значок чипа")
    tags: list[TagId] = Field(
        ..., description="Метки мест, которые выбирает чип; их пересечение с `Place.tags` непусто"
    )
    place_count: int = Field(
        ...,
        ge=1,
        description=(
            "Сколько мест каталога подходят под чип. Нуля здесь не бывает: чип, на котором нечего "
            "показать, до клиента не доезжает, а не приезжает пустым"
        ),
    )


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
    tags: list[str] = Field(
        default_factory=list,
        description=(
            "Фильтр по настроению: id из GET /chips. Внутри списка — ИЛИ (место годится, если у него "
            "есть хотя бы одна из запрошенных меток), между `tags` и `categories` — И. Пустой список "
            "ничего не ограничивает, а неизвестный id не находит мест и даёт 404 — как категория"
        ),
    )
    max_budget: float | None = Field(
        None,
        ge=0,
        validation_alias=AliasChoices("max_budget", "budget"),
        description="Максимальная суммарная стоимость маршрута, руб. Принимается и под именем `budget`",
    )
    is_pushkin_card_only: bool = Field(False, description="Только места по Пушкинской карте")
    duration_hours: float = Field(4.0, gt=0, le=12, description="Желаемая длительность, ч.")
    start_lat: float | None = Field(
        None,
        ge=-90,
        le=90,
        description=(
            "Широта точки, из которой начинают прогулку, deg — вместе с `start_lon`. Без неё маршрут "
            "строится как раньше: первой точкой планировщика, без перехода к ней. С ней первые "
            "`travel_minutes_from_prev` и `distance_m_from_prev` считаются от старта и съедают бюджет "
            "времени, а слишком далёкие места до фильтрации не доходят"
        ),
    )
    start_lon: float | None = Field(
        None, ge=-180, le=180, description="Долгота точки старта, deg — вместе с `start_lat`"
    )

    # Deliberately not `list[TagId]`: a typo here must answer 404 «нечего собирать», как это делает
    # неизвестная категория, а не 422 со списком ошибок — два фильтра одной оси не различаются кодами.
    @field_validator("categories", "tags")
    @classmethod
    def _drop_blanks(cls, value: list[str]) -> list[str]:
        return [item.strip() for item in value if item.strip()]

    # Half a coordinate pair locates nothing, and silently ignoring the one half that arrived would be
    # the same lie as an ignored `budget` — so 422, like `near_lat`/`near_lon` on GET /places.
    @model_validator(mode="after")
    def _start_is_a_pair(self) -> "RouteRequest":
        if (self.start_lat is None) != (self.start_lon is None):
            raise ValueError("start_lat и start_lon передают вместе: одна координата точку не задаёт")
        return self

    @property
    def start(self) -> Location | None:
        """The walk's origin as the engine wants it: a `Location` or nothing at all."""
        if self.start_lat is None or self.start_lon is None:
            return None
        return Location(lat=self.start_lat, lon=self.start_lon)


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
            "Переход от предыдущей точки, мин; у первой точки — от `start` запроса, или 0, если старта "
            "не было. Оценка планировщика (пеший ход плюс запас на ожидание), а не ETA навигатора: путь "
            "по тротуарам всегда дольше"
        ),
    )
    distance_m_from_prev: int = Field(
        0,
        ge=0,
        description=(
            "Расстояние от предыдущей точки по прямой, м; у первой точки — от точки старта, если она "
            "передана, иначе 0. Длина тротуарного обхода всегда больше, поэтому подпись «N км пешком» "
            "честнее читать «N км напрямую»"
        ),
    )
    walk_distance_m_from_prev: int | None = Field(
        None,
        ge=0,
        description=(
            "Длина этого перехода по пешеходной сети, м — то, что человек реально пройдёт. null при "
            "`geometry_source = straight_line`: прямую между маркерами измерить не у кого, и подписывать "
            "её как путь по тротуарам было бы враньём. Не замена `travel_minutes_from_prev`: минуты "
            "планировщика остаются обещанием, на котором маршрут сошёл в заказанный бюджет"
        ),
    )
    geometry_from_prev: list[list[float]] | None = Field(
        None,
        description=(
            "Линия перехода от предыдущей точки к этой, координаты GeoJSON как [lon, lat]; у первой "
            "точки — от `start`, если он передавался. null бывает у первой точки без старта и у "
            "одномаршрутной подборки — вписать переход некуда. При `geometry_source = straight_line` "
            "это хорда между маркерами, и рисовать её надо пунктиром. Линия начинается у ближайшей "
            "точки пешеходной сети, а не у маркера: места стоят в 5–150 м от тротуара"
        ),
    )


class RouteResponse(ApiModel):
    route_id: str
    title: str
    city: str
    # Echoed rather than left in the request: a saved route comes back through GET /routes without its
    # request, and the map needs the start to draw its marker and to explain the first hop.
    start: Location | None = Field(
        None, description="Точка старта из запроса; None, если старт не передавали"
    )
    total_duration_hours: float = Field(
        ..., ge=0, description="Визиты и переходы между точками, включая переход от старта к первой точке"
    )
    total_duration_minutes: int = Field(
        0,
        ge=0,
        description="То же число в минутах, без округления до сотых часа",
    )
    total_distance_m: int = Field(
        0,
        ge=0,
        description=(
            "Сумма прямых расстояний между соседними точками, м, включая отрезок от старта до первой "
            "точки"
        ),
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
    geometry_source: Literal["osrm", "straight_line"] = Field(
        "straight_line",
        description=(
            "Откуда линия между точками: `osrm` — пешеходный маршрут, посчитанный для этой цепочки, "
            "`straight_line` — хорда между маркерами, потому что сервер маршрутов не ответил или "
            "выключен. Определяет и подпись длины на экране: «по тротуарам» против «напрямую»"
        ),
    )
    total_walk_distance_m: int | None = Field(
        None,
        ge=0,
        description=(
            "Сумма измеренных переходов, м; null, если ни один переход не измерен (весь маршрут — "
            "хорды). С `total_distance_m` сравнивать напрямую нельзя: тротуар огибает квартал, а прямая "
            "режет его по диагонали"
        ),
    )
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
            measured = [
                stop.walk_distance_m_from_prev
                for stop in self.stops
                if stop.walk_distance_m_from_prev is not None
            ]
            self.total_walk_distance_m = sum(measured) if measured else None
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

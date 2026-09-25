# MAX Bot MVP — цифровой навигатор маршрутов выходного дня

Асинхронный бот для мессенджера **MAX**: FastAPI-webhook для сервера, Long Polling для локальной
разработки, SQLite (WAL) с прозрачным переходом на PostgreSQL, и кнопки запуска Mini App.

На любое входящее событие (`bot_started`, `/start`, `/help`, `/route`, произвольный текст) бот
отвечает приветствием и отдаёт inline-клавиатуру с кнопкой открытия мини-приложения.

## Архитектура

```text
core/config.py        pydantic-settings: валидация .env, нормализация DATABASE_URL
bot/models.py         Pydantic-схемы MAX: Update/Message/Keyboard/SendMessagePayload
bot/client.py         httpx.AsyncClient: единый пул, ретраи, rate-limit 2 msg/sec на диалог
bot/keyboards.py      билдеры inline_keyboard (open_app / link / callback / message)
bot/dispatcher.py     роутинг по update_type и командам + дедупликация событий + upsert пользователя
bot/handlers/start.py приветствие, фолбэк на любой текст, ответы на callback
server/database.py    AsyncEngine + aiosqlite, PRAGMA WAL/synchronous/foreign_keys, модель User
server/schemas.py     публичные контракты: Place / RouteRequest / RouteResponse + описания для OpenAPI
server/catalog.py     загрузка data/places.json (кэш), нестрогое сравнение города, список категорий
server/routing.py     сборка маршрута: отбор по рейтингу на минуту затрат + 2-opt порядок переходов
server/routers/       /api/v1: places, categories, routes/generate
server/cors.py        CORS для браузерных запросов Mini App (CORS_ALLOW_ORIGINS)
server/app.py         FastAPI: lifespan, POST /webhook, /health
scripts/export_openapi.py
                      генерация DATA-API.yaml из живого приложения (--check для CI)
frontend/             Mini App (React + Vite): онбординг → выбор времени/категорий → маршрут
main.py               единая точка входа: --mode=webhook|polling|setup-webhook
```

Запросы к MAX идут на `https://platform-api2.max.ru` (токен в заголовке `Authorization`, сырой
строкой — query-параметр `access_token` больше не поддерживается).

## Запуск

Локальная разработка (нужен только токен, публичный HTTPS не требуется):

```bash
python -m venv .venv && .venv\Scripts\activate   # Linux: source .venv/bin/activate
pip install -r requirements.txt
copy .env.example .env                            # вписать BOT_TOKEN
python main.py --mode=polling
```

Корневой сертификат Минцифры уже лежит в репозитории (`certs/russian-trusted-root-ca.pem`), он
подмешивается к системным корням на лету — отдельный шаг не нужен.

Сервер (Docker, одна команда):

```bash
docker compose up --build -d
```

Подробности — в шпаргалке в конце.

## Клавиатуры в MAX: что реально поддерживает API

MAX не знает про Telegram-подобную `reply_markup`/постоянную клавиатуру у поля ввода. Есть три
нативных механизма, и все три здесь используются:

| Механизм | Где отображается | Реализация |
| --- | --- | --- |
| `attachments/type=inline_keyboard` | под сообщением, до 30 рядов | `bot/keyboards.py:mini_app_keyboard()` |
| кнопка мини-приложения в настройках бота | у поля ввода («Открыть/Старт/Играть») | включается в кабинете MAX |
| `PATCH /me/commands` (меню команд) | у поля ввода, как постоянная клавиатура | `bot_commands()` + `AUTO_SETUP=true` |

Кнопка запуска Mini App — тип `open_app` с полем `web_app` (нужна привязка мини-приложения к боту).
Если `MINI_APP_URL` пустой, билдер честно откатывается на `link`-кнопку с диплинком
`https://max.ru/<BOT_USERNAME>?startapp=<payload>`, и кнопка продолжает работать.

Жёсткие ограничения, проверенные на живом API (нарушение = `400 proto.payload`, а не тишина):

| Правило | Как соблюдается в коде |
| --- | --- |
| в одном сообщении ровно **один** `inline_keyboard` | `merge_keyboards()` склеивает ряды; `SendMessagePayload` не даст собрать два |
| `open_app` без `web_app` не проходит («Field 'webApp' cannot be null») | `OpenAppButton.web_app` обязателен в модели, билдер падает на `link` |
| `open_app.payload` — только `[A-Za-z0-9_-]` (`start:menu`, URL, `a.b` — отказ) | `sanitize_app_payload()` + `pattern` в модели |
| `callback.payload` свободный (`action:refresh`, JSON, кириллица) | как есть |
| `POST /answers` требует тело: `message` или `notification` (`{}` → `400 proto.payload`) | `CallbackAnswerPayload.message` обязателен; тап отвечает `Context.answer()` — MAX подменяет сообщение с кнопками, при отказе уходит новое сообщение |
| текст ≤ 4000 символов, ≤ 2 сообщения в секунду в диалог | `max_length` в модели + `_throttle()` в клиенте |

## Конфигурация

| Переменная | Назначение |
| --- | --- |
| `BOT_TOKEN` | токен бота из business.max.ru |
| `BOT_API_URL` | по умолчанию `https://platform-api2.max.ru` |
| `BOT_USERNAME` | используется для диплинка мини-приложения |
| `MINI_APP_URL` | адрес Mini App для кнопки `open_app` |
| `BOT_WEBHOOK_URL` / `PUBLIC_BASE_URL` | адрес вебхука (второй — базовый, `/webhook` добавится сам) |
| `WEBHOOK_SECRET` | MAX вернёт его в заголовке `X-Max-Bot-Api-Secret`; обязателен с вебхуком |
| `DATABASE_URL` | `sqlite+aiosqlite:///data/bot.db` или `postgresql+asyncpg://...` |
| `SSL_CA_BUNDLE` / `SSL_VERIFY` | доп. доверенный корень MAX (уже лежит в `certs/`) / строгость TLS |
| `AUTO_SETUP` | регистрировать меню команд и подписку при старте |
| `POLLING_TIMEOUT` / `POLLING_LIMIT` | параметры long polling (0..90 / 1..1000) |
| `CORS_ALLOW_ORIGINS` | список origin'ов через запятую, которым разрешено звать `/api/v1` из браузера; пусто — `*` |

Смена SQLite → PostgreSQL не требует кода: только `DATABASE_URL` (движок сам подставит
`asyncpg`, upsert работает на обеих диалектах).

## TLS

MAX отдаёт цепочку, корень которой — «Russian Trusted Root CA» УЦ Минцифры (НУЦ Госуслуг). Такого
корня нет ни в хранилище Windows, ни в базовом образе (`unable to get local issuer certificate` —
проверено локально). Корень лежит в `certs/russian-trusted-root-ca.pem` и подгружается к корням
`certifi`: проверка сертификата остаётся строгой и на Windows, и в контейнере, `SSL_VERIFY=false`
не требуется.

Скачан по адресу AIA из промежуточного сертификата MAX, оба зеркала отдали байт-в-байт один файл:

```
http://nuc-cdp.digital.gov.ru/cdp/rootca_ssl_rsa2022.crt
http://nuc-cdp.voskhod.ru/cdp/rootca_ssl_rsa2022.crt
sha256: 936a43fea6e8e525bcc0f81acd9c3d21b4fc4b9b68acea7906d698005afc6504
fingerprint: D2:6D:2D:02:31:B7:C3:9F:92:CC:73:85:12:BA:54:10:35:19:E4:40:5D:68:B5:BD:70:3E:97:88:CA:8E:CF:31
```

Перед заменой файла сверьте отпечаток с публикацией УЦ. Свой путь — `SSL_CA_BUNDLE`; если файл не
найден, клиент честно предупреждает в лог и остаётся только системное хранилище.

Отдельно: **с 25 мая 2026 MAX не принимает вебхуки по HTTP и самоподписанные сертификаты** —
нужен валидный сертификат от доверенного ЦС (в том числе Минцифры) на 443 порту.

## Endpoints

| Метод | Путь | Назначение |
| --- | --- | --- |
| `POST` | `/webhook` | принимает `Update`, отвечает 200 сразу, обрабатывает в фоне |
| `GET` | `/health` | статус БД и число пользователей |
| `GET` | `/readyz` | готовность: 503, если база не отвечает |
| `GET` | `/api/v1/places` | каталог с фильтрами `city`, `category`, `is_pushkin_card`, `max_price`, `q`, `min_rating`, `near_lat`+`near_lon`+`radius_km`, `limit`/`offset` |
| `GET` | `/api/v1/places/{place_id}` | карточка места, 404 при промахе |
| `GET` | `/api/v1/categories` | категории, фактически присутствующие в данных |
| `GET` | `/api/v1/cities` | города каталога: `city`, `place_count`, `categories` |
| `POST` | `/api/v1/routes/generate` | собранный маршрут с таймингами переходов |
| `GET` | `/docs` | OpenAPI/Swagger |

Контракт для фронтенда — `DATA-API.yaml` в корне. Он не правится руками: после любого изменения эндпоинтов
пересоберите его из живого приложения и коммитьте вместе с кодом.

## Mini App (frontend/) — сырая альфа-версия

React 18 + Vite, CSS-модули, без сторонних UI-библиотек. Фронтенд **не меняет бэкенд**: только
существующие эндпоинты `/api/v1` из `DATA-API.yaml`. Статус — рабочий прототип: три экрана
(онбординг по макету → выбор времени 1–4 ч и категорий → маршрут с таймингами переходов),
состояния loading/error/empty, safe-area и haptics при открытии внутри WebView MAX/Telegram
(feature-detection в `src/lib/messenger.js`). Что ещё не делалось: тёмная тема, карта, экран
справки по пути из макета, тесты.

Запуск для ревью (три процесса):

```bash
# 1. API бэкенда (из корня репозитория, .env с BOT_TOKEN не обязателен для одних только /api)
python main.py --mode=webhook            # или: uvicorn server.app:app --port 8080

# 2. Mini App: http://localhost:5173, /api проксируется на VITE_BACKEND_URL (по умолчанию :8080)
cd frontend && npm install && npm run dev

# 3. (только для демо в мессенджере) HTTPS-туннель на фронтенд
cloudflared tunnel --url http://localhost:5173
```

Прод-сборка статики — `npm run build` в `frontend/` (результат в `dist/`, адрес API задаётся
переменной сборки `VITE_API_URL`).

### Ограничение MAX: регистрация ссылок

Проверено живым запросом (`tools/test_buttons.py`, не коммитится): кнопка `open_app` с URL,
не привязанным к боту в кабинете `business.max.ru`, роняет всё сообщение с
`404 not.found / Link not found (LinkPK ... space=TAMTAM)`. Обычные `link`-кнопки принимают
любой HTTPS-URL. Поэтому в `.env`:

- `MINI_APP_URL` — адрес мини-приложения **после** привязки в кабинете; тогда «🧭 Открыть
  навигатор» открывается нативным WebView MAX;
- без привязки `MINI_APP_URL` держится закомментированным, а `PUBLIC_BASE_URL` указывает на
  публичный HTTPS-адрес фронтенда — «🌐 Веб-версия» открывает то же приложение в браузере.

Для туннельного URL нужен `allowedHosts: ['.trycloudflare.com']` в `vite.config.js` (уже
настроено), а адрес меняется при каждом перезапуске туннеля — после смены обновите `.env` и
перезапустите бота.

```bash
pip install -r dev-requirements.txt
python scripts/export_openapi.py            # перегенерировать
python scripts/export_openapi.py --check    # проверка, которая будет крутить CI
```

Тесты бэкенда — `pytest`, они не трогают ни `.env`, ни живую базу (`data/pytest.db`), ни MAX API:

```bash
python -m pytest tests -q
```

CI (`.github/workflows/ci.yml`) на каждом PR прогоняет тесты на Python 3.12, сверяет `DATA-API.yaml` с
кодом и собирает образ.

Семантика ответов, на которую стоит опираться:

- `city` сопоставляется нестрого: `Ростов`, `ростов-на-Дону`, `Ростов на Дону` и `Ростов-на-Дона` — один
  и тот же город. Достаточно, чтобы введённые слова были началом названия из каталога.
- `GET /places` с пустой выборкой отдаёт `200 []` (коллекция по фильтру пуста — это не ошибка), а
  `POST /routes/generate` — `404`, потому что маршрута не существует.
- Бюджет в `/routes/generate` ограничивает **суммарную** стоимость маршрута и принимается под именем
  `max_budget` или `budget`.
- Неизвестное поле в теле запроса — `422` со списком ошибок, а не молчаливое игнорирование: проглоченный
  `budget` неотличим от проигнорированного лимита.
- `503` означает, что файл каталога недоступен или повреждён.
- `q` ищет подстрокой по `title`, `description`, `address` и `category`: регистр, пунктуация и лишние
  пробелы не важны, но найтись должны **все** слова запроса. Пустой по словам запрос (`?`, `«»`) не находит
  ничего.
- `radius_km` — прямое расстояние от `near_lat`/`near_lon`, а не время в пути; задаётся только вместе с
  обеими координатами, иначе `422`.
- Итоги пагинации у `/places` лежат в заголовках: `X-Total-Count` — размер выборки до `limit`, `X-Offset` —
  сдвиг ответа. Тело остаётся голым массивом, конверт `{items, total}` не вводится.
- `GET /cities` — готовый источник для селектора города: список `[{city, place_count, categories}]` в порядке
  появления городов в каталоге.
- `/health` отвечает `200` и при недоступной базе — на него висит `HEALTHCHECK` контейнера, и валить бота
  из-за короткого сбоя SQLite не нужно. Проверяют готовность отдельно: `/readyz` отдаёт `503`.

## Шпаргалка: деплой на сервер одной командой

```bash
git clone <repo> && cd hackaton_max \
 && cp .env.example .env && nano .env \
 && docker compose up --build -d && docker compose logs -f bot
```

Дальше — HTTPS на 443 (MAX не принимает http-вебхуки и сам порт 8080), например через Caddy:

```bash
docker run -d --name caddy -p 443:443 -v $PWD/Caddyfile:/etc/caddy/Caddyfile -v caddy_data:/data caddy
```

после чего включить авто-подписку и проверить:

```bash
docker compose exec bot python main.py --mode=setup-webhook   # подписка + меню команд
curl https://your-domain.com/health
```

Полезно знать: MAX **копит** подписки (не заменяет) и **отписывается через 8 часов** без успешного
ответа. `--mode=setup-webhook` и старт вебхук-режима сами чистят устаревшие URL перед подпиской.
Локальная база живёт в `./data/bot.db` и переживает пересборку образа.

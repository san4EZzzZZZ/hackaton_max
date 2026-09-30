#!/usr/bin/env python
"""Render the submitted API contract from the live application, or verify that it is current.

    python scripts/export_openapi.py            # regenerate DATA-API.yaml and openapi.json
    python scripts/export_openapi.py --check    # CI: fail when either file lags behind the code

The document is exactly what `create_app()` reports, so a route change can no longer drift away from its
documentation. Two things are appended afterwards, because neither is something the code declares:

`servers` — where this build actually runs. The reviewer checks a hosted instance, so the public HTTPS
address is listed first and localhost second.

`x-check` — the checklist of the submission format: which endpoints a reviewer must call, with what
credentials, and what a correct answer looks like. OpenAPI describes every endpoint; it has no place to
say which ones the acceptance scenario depends on, and that is the question the format asks.

`openapi.json` is the same document in the other format the format allows, written for a checker who
reaches for a tool instead of a text editor.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT = REPO_ROOT / "DATA-API.yaml"
JSON_OUTPUT = REPO_ROOT / "openapi.json"

API_BASE = "https://max.chestnyidiplom.ru"
# Статика Mini App отдаётся nginx из корня сайта, /api/ проксируется на этот же бэкенд.
MINI_APP = "https://max.chestnyidiplom.ru/"

SERVERS = [
    {"url": API_BASE, "description": "Проверяемый развёрнутый экземпляр (HTTPS, доступен во время проверки)"},
    {"url": "http://localhost:8080", "description": "Локальный запуск: docker compose up --build"},
]

# Обязательные проверки основной пользовательской сценария: «открыть → выбрать город и интерес →
# получить маршрут → сохранить → отметить событие». Поле `check` — что именно должно сработать, чтобы
# сценарий считался пройденным; `fields` — обязательные ключи ответа, а не весь контракт.
MANDATORY_CHECKS = [
    {
        "check": "каталог не пустой и знает города",
        "method": "GET",
        "path": "/api/v1/cities",
        "role": "гость",
        "expected_status": [200],
        "content_type": "application/json",
        "fields": ["city", "place_count", "categories"],
    },
    {
        "check": "ряд «Чем хочешь заняться?» строится из ответа, пустого чипа не бывает",
        "method": "GET",
        "path": "/api/v1/chips",
        "params": {"query": "city=Ростов-на-Дону"},
        "role": "гость",
        "expected_status": [200],
        "content_type": "application/json",
        "fields": ["id", "label", "emoji", "tags", "place_count"],
    },
    {
        "check": "фильтр по городу и тегу находит места, итог пагинации — в заголовке",
        "method": "GET",
        "path": "/api/v1/places",
        "params": {"query": "city=Ростов-на-Дону&tag=coffee&limit=5"},
        "role": "гость",
        "expected_status": [200],
        "content_type": "application/json",
        "fields": ["id", "title", "category", "city", "location"],
        "headers": {"X-Total-Count": "размер выборки до limit"},
    },
    {
        "check": "маршрут собирается под бюджет времени и interest, переходы измерены по тротуарам",
        "method": "POST",
        "path": "/api/v1/routes/generate",
        "params": {
            "body": {
                "city": "Ростов-на-Дону",
                "tags": ["culture"],
                "duration_hours": 3,
                "start_lat": 47.2219,
                "start_lon": 39.7139,
            }
        },
        "role": "гость",
        "expected_status": [200, 404, 422],
        "expected_status_note": (
            "200 — маршрут есть; 404 — под фильтр ничего не подошло (в том числе города нет в данных); "
            "422 — теле запроса некорректно, например передана только одна координата старта"
        ),
        "content_type": "application/json",
        "fields": [
            "route_id",
            "title",
            "city",
            "stops",
            "total_duration_minutes",
            "total_distance_m",
            "total_walk_distance_m",
            "geometry_source",
            "slack_minutes",
        ],
    },
    {
        "check": "маршрут переживает перезапуск процесса",
        "method": "POST",
        "path": "/api/v1/routes",
        "params": {"body": "дословный ответ генератора, плюс необязательный целый user_id"},
        "role": "гость",
        "expected_status": [201, 503],
        "expected_status_note": "503 — база не отвечает",
        "content_type": "application/json",
        "fields": ["route_id", "user_id", "stop_count", "created_at", "route"],
    },
    {
        "check": "справка по месту отдаётся там, где она написана, и честно говорит 404 где ещё нет",
        "method": "GET",
        "path": "/api/v1/places/{place_id}/guide",
        "params": {"path": "place_id=theatre-square"},
        "role": "гость",
        "expected_status": [200, 404],
        "expected_status_note": "404 — справки ещё нет, это очередь а не ошибка",
        "content_type": "application/json",
        "fields": ["history", "highlights", "nearby"],
    },
    {
        "check": "событие воронки принимается, метрики считаются по сессиям",
        "method": "POST",
        "path": "/api/v1/events",
        "params": {"body": {"name": "miniapp_open", "session_id": "check-00000001"}},
        "role": "гость",
        "expected_status": [201, 422],
        "expected_status_note": "422 — поле чужого события (например rating у miniapp_open)",
        "content_type": "application/json",
        "fields": ["event_id", "name", "session_id", "occurred_at"],
    },
    {
        "check": "семь метрик пилота отдаются одним запросом, пустая выборка — null а не 0",
        "method": "GET",
        "path": "/api/v1/metrics/funnel",
        "params": {"query": "days=30"},
        "role": "гость",
        "expected_status": [200],
        "content_type": "application/json",
        "fields": ["generated_at", "window_days", "sessions", "events", "metrics"],
        "metrics_item_fields": ["key", "label", "formula", "target", "unit", "value", "reached", "sample"],
    },
    {
        "check": "поиск мест по городу запускается по HTTP и не трогает каталог в dry_run",
        "method": "POST",
        "path": "/api/v1/admin/ingest",
        "params": {"headers": "X-Ingest-Token", "body": {"cities": ["Ростов-на-Дону"], "dry_run": True}},
        "role": "редактор каталога",
        "expected_status": [200, 401, 503],
        "expected_status_note": "503 — INGEST_TOKEN не задан; 401 — заголовок X-Ingest-Token не совпал",
        "content_type": "application/json",
        "fields": ["requested", "city", "places", "published", "dropped", "failures"],
    },
    {
        "check": "готовность отличается от живости: /health отвечает всегда, /readyz — только когда база отвечает",
        "method": "GET",
        "path": "/readyz",
        "role": "гость",
        "expected_status": [200, 503],
        "content_type": "application/json",
        "fields": ["status"],
    },
]

CHECKLIST = {
    "config_version": "1.0",
    "solution": "Цифровой навигатор и конструктор маршрутов выходного дня в мессенджере MAX",
    "team": "Хакатон МАХ 91",
    "repository": "https://github.com/san4EZzzZZZ/hackaton_max",
    "base_url": f"{API_BASE}/api/v1",
    "mini_app_url": MINI_APP,
    "test_accounts": [
        {
            "role": "гость",
            "login": "не требуется",
            "note": (
                "У публичных эндпоинтов /api/v1 аутентификации нет намеренно: ни сессии, ни токена, ни куки. "
                "Все проверки основного сценария выполняются без учётной записи."
            ),
        },
        {
            "role": "редактор каталога",
            "login": "заголовок X-Ingest-Token",
            "note": (
                "Единственная защищённая операция — POST /api/v1/admin/ingest. Значение токена выдаётся "
                "проверяющему вместе с доступом к .env проверяемого экземпляра; без INGEST_TOKEN эндпоинт "
                "отвечает 503 и это не ошибка проверки."
            ),
        },
    ],
    "test_data": {
        "places": "data/places.json — 43 вручную сверенных места Ростова-на-Дону (в репозитории)",
        "generated_places": (
            "data/places.d/rostov-na-donu.json — 47 мест того же города, собранных `python -m ingest`; "
            "после клона в каталоге один город и 90 мест"
        ),
        "guides": "data/place_guides.json — 8 написанных справок",
        "cities": "data/cities.json — список городов для `python -m ingest --all`",
        "note": (
            "База (`data/bot.db`) для проверки не нужна: каталог, маршруты и справки читаются из JSON. "
            "В репозитории лежат только тестовые данные, ни одного персонального."
        ),
    },
    "mandatory_checks": MANDATORY_CHECKS,
}

# Importing the application pulls in core.config, which validates the environment on import. The spec
# needs no bot credentials and must never reach the MAX API.
os.environ.setdefault("BOT_TOKEN", "spec-export-placeholder-token")
os.environ.setdefault("AUTO_SETUP", "false")


def build_spec() -> dict:
    sys.path.insert(0, str(REPO_ROOT))
    from server.app import create_app

    schema = create_app().openapi()
    schema["servers"] = SERVERS
    # `x-check` goes first so a human opening the file sees the checklist before 2400 lines of schemas.
    return {"x-check": CHECKLIST, **schema}


def render_yaml() -> str:
    return yaml.safe_dump(build_spec(), allow_unicode=True, sort_keys=False)


def render_json() -> str:
    return json.dumps(build_spec(), ensure_ascii=False, indent=2) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="compare against the files on disk instead of writing them",
    )
    args = parser.parse_args(argv)

    rendered = {"DATA-API.yaml": render_yaml(), "openapi.json": render_json()}
    if args.check:
        stale = []
        for name, text in rendered.items():
            path = REPO_ROOT / name
            if path.is_file() and path.read_text(encoding="utf-8") == text:
                continue
            stale.append(name)
        if stale:
            print(
                f"{', '.join(stale)} out of date — run: python scripts/export_openapi.py",
                file=sys.stderr,
            )
            return 1
        print("API contracts match the application")
        return 0

    for name, text in rendered.items():
        (REPO_ROOT / name).write_text(text, encoding="utf-8", newline="\n")
        print(f"Wrote {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

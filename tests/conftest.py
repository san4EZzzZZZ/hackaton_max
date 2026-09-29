"""Application wired to a throwaway database, with no outbound traffic and no `.env`.

The environment has to be complete before the first application import: `core.config` validates it on
import and `server.database` builds its engine from that value, so a fixture would already be too late.
`pydantic-settings` prefers `os.environ` over `.env`, which is what keeps the developer's real token and
real `data/bot.db` out of the run.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Relative on purpose, exactly like the production default: the sqlite driver resolves it against the
# working directory, and the path stays inside `data/`, which .gitignore already covers.
TEST_DB_URL = "sqlite+aiosqlite:///data/pytest.db"

os.environ["BOT_TOKEN"] = "pytest-token-not-a-real-one"
os.environ["BOT_USERNAME"] = "pytest_bot"
os.environ["WEBHOOK_SECRET"] = "pytest-webhook-secret"
os.environ["DATABASE_URL"] = TEST_DB_URL
# Without this the lifespan would call platform-api2.max.ru on every test that builds an app.
os.environ["AUTO_SETUP"] = "false"
# Автокаталог — единственная запись в API: на машине разработчика он включён своим токеном из .env,
# в тестах он выключен всегда, иначе 503-ветка зависела бы от локального файла.
os.environ["INGEST_TOKEN"] = ""
# Линия маршрута приходит с общего демо-сервера OSRM. Живой запрос из теста — это и медленнее, и
# несопоставимо: числа линии меняются между прогонами. Ветки «сервер ответил» проверяются
# подменой транспорта, а здесь — детерминированный fallback на хордах.
os.environ["OSRM_URL"] = ""
# Leftover developer settings would silently change what the CORS tests assert.
os.environ.pop("CORS_ALLOW_ORIGINS", None)

from fastapi.testclient import TestClient  # noqa: E402

import server.catalog as catalog_module  # noqa: E402
import server.guides as guides_module  # noqa: E402
from server.app import create_app  # noqa: E402
from server.catalog import invalidate_cache, load_places  # noqa: E402
from server.database import Base, engine  # noqa: E402
from server.guides import invalidate_cache as invalidate_guides  # noqa: E402

# `data/places.d/` holds whatever `python -m ingest` fetched on this machine, and it must not decide
# how many places a test expects. The autouse fixture below redirects it per test — but a
# session-scoped fixture is set up before any function fixture runs, so the same isolation has to
# hold from the import of this file on. The path is one no code ever creates.
catalog_module.EXTRA_DIR = Path(tempfile.gettempdir()) / f"maxbot-pytest-{os.getpid()}-places.d"


@pytest.fixture(autouse=True)
def empty_database() -> Iterator[None]:
    """`/health` reports a user count, so rows left behind by one test would fail the next.

    `engine` is a module-level singleton while every test gets its own event loop, so whatever opens a
    connection has to hand the pool back before that loop closes — otherwise the next loop inherits a
    connection bound to a dead one.
    """

    async def reset() -> None:
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.drop_all)
                await connection.run_sync(Base.metadata.create_all)
        finally:
            await engine.dispose()

    asyncio.run(reset())
    yield


@pytest.fixture(autouse=True)
def uncached_data_files(tmp_path, monkeypatch) -> Iterator[None]:
    """Both seed loaders are `lru_cache`d, so a test that points one at a broken file must not leak.

    The generated-cities directory is moved into the test's own `tmp_path` while we are at it: it is
    gitignored and full of whatever `python -m ingest` found on this machine, and a suite whose
    counts depend on that would pass here and fail in CI.
    """
    monkeypatch.setattr(catalog_module, "EXTRA_DIR", tmp_path / "places.d")
    invalidate_cache()
    invalidate_guides()
    yield
    invalidate_cache()
    invalidate_guides()


def guide_record(place_id: str = "theatre-square", **overrides: object) -> dict:
    """One complete, valid guide record that a test can break in exactly one place.

    The two history paragraphs are as long as they are because the schema floors a paragraph at
    120 characters; a stub shorter than that would fail the very test that only means to check
    something else.
    """
    record = {
        "place_id": place_id,
        "status": "ready",
        "history": [
            "Первый абзац. Место появилось в конце XIX века на углу главной улицы и получило имя "
            "своего первого владельца, чей промысел и определил облик всего квартала.",
            "Второй абзац. В советское время здание национализировали, вернули его только в 2000-х, "
            "после чего началась реставрация фасадов и утраченных ранее деталей кровли.",
        ],
        "highlights": [
            "Остроконечная башня на углу",
            "Даты, выбитые в кирпиче фасада",
            "Мемориальная доска первому владельцу",
        ],
        "media": [],
        "media_note": "Свободных снимков этого фасада на Commons нет",
        "provenance": {"source_url": None, "last_verified": None, "verified_fields": []},
    }
    record.update(overrides)
    return record


def write_guides(monkeypatch, tmp_path: Path, *records: dict) -> Path:
    """Point the guide loader at a throwaway file holding exactly these records."""
    path = tmp_path / "place_guides.json"
    monkeypatch.setattr(guides_module, "DATA_FILE", path)
    path.write_text(json.dumps(list(records), ensure_ascii=False), encoding="utf-8")
    invalidate_guides()
    return path


@contextmanager
def app_client() -> Iterator[TestClient]:
    """Entering the context is what runs the lifespan; a bare `TestClient(app)` never starts the DB."""
    with TestClient(create_app()) as client:
        yield client


@pytest.fixture
def client() -> Iterator[TestClient]:
    with app_client() as test_client:
        yield test_client


@pytest.fixture(scope="session")
def places() -> list[dict]:
    """The seed catalog as plain dicts, for tests that count instead of hardcoding numbers."""
    return [place.model_dump() for place in load_places()]


@pytest.fixture(scope="session")
def one_place(places: list[dict]) -> dict:
    """A real record, for a test that needs a valid object rather than the whole file."""
    return dict(places[0])

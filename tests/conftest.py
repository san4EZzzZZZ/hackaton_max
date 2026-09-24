"""Application wired to a throwaway database, with no outbound traffic and no `.env`.

The environment has to be complete before the first application import: `core.config` validates it on
import and `server.database` builds its engine from that value, so a fixture would already be too late.
`pydantic-settings` prefers `os.environ` over `.env`, which is what keeps the developer's real token and
real `data/bot.db` out of the run.
"""

from __future__ import annotations

import asyncio
import os
import sys
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
# Leftover developer settings would silently change what the CORS tests assert.
os.environ.pop("CORS_ALLOW_ORIGINS", None)

from fastapi.testclient import TestClient  # noqa: E402

from server.app import create_app  # noqa: E402
from server.catalog import invalidate_cache, load_places  # noqa: E402
from server.database import Base, engine  # noqa: E402


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
def uncached_catalog() -> Iterator[None]:
    """`load_places` is an `lru_cache`, so a test that points it at a broken file must not leak."""
    invalidate_cache()
    yield
    invalidate_cache()


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

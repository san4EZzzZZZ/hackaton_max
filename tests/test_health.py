"""Liveness, readiness and the root pointer.

`/health` and `/readyz` deliberately disagree: the first is watched by the container `HEALTHCHECK` and
must keep answering 200 through a short database hiccup, the second is what a load balancer probes and
must not. Both directions are pinned here, because swapping them is the edit that looks harmless.
"""

from __future__ import annotations

import asyncio

import server.app as app_module
from conftest import app_client
from fastapi.testclient import TestClient
from server.database import engine, session_scope, upsert_user


def break_database(monkeypatch) -> None:
    async def dead() -> bool:
        return False

    monkeypatch.setattr(app_module, "ping_db", dead)


async def _seed_users(*user_ids: int) -> None:
    try:
        for user_id in user_ids:
            async with session_scope() as session:
                await upsert_user(session, user_id=user_id, username=f"user{user_id}")
    finally:
        # See conftest.empty_database: the loop that touched the pool has to release it.
        await engine.dispose()


def test_health_reports_a_healthy_empty_database(client: TestClient) -> None:
    body = client.get("/health").json()
    assert body == {
        "status": "ok",
        "database": "up",
        "users": 0,
        "bot": "pytest_bot",
        "mode": "webhook",
    }


def test_health_counts_users_from_the_database() -> None:
    asyncio.run(_seed_users(1001, 1002))
    with app_client() as client:
        assert client.get("/health").json()["users"] == 2


def test_health_stays_200_when_the_database_is_down(
    client: TestClient, monkeypatch
) -> None:
    break_database(monkeypatch)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "degraded"
    assert response.json()["database"] == "down"


def test_readyz_accepts_traffic_when_the_database_answers(client: TestClient) -> None:
    assert client.get("/readyz").json() == {"status": "ready"}


def test_readyz_refuses_traffic_when_the_database_is_down(
    client: TestClient, monkeypatch
) -> None:
    break_database(monkeypatch)
    response = client.get("/readyz")
    assert response.status_code == 503
    assert response.json() == {"detail": "База данных не отвечает"}


def test_root_points_at_the_docs_and_the_webhook(client: TestClient) -> None:
    assert client.get("/").json() == {
        "service": "MAX Messenger Bot MVP",
        "webhook": "POST /webhook",
        "docs": "/docs",
    }


def test_swagger_ui_is_served(client: TestClient) -> None:
    assert client.get("/docs").status_code == 200

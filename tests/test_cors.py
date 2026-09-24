"""CORS: the Mini App calls these endpoints from a browser, so the response headers are the contract.

The default is a wildcard because every endpoint here is read-only or idempotent-by-request and sends no
cookies. Once `CORS_ALLOW_ORIGINS` is set the middleware must go strict — a deployment that forgot the
variable would be reported as working while exposing the API to any origin, and vice versa.
"""

from __future__ import annotations

from conftest import app_client
from fastapi.testclient import TestClient

BROWSER_ORIGIN = "https://minis.max.ru"


def preflight(client: TestClient, path: str = "/api/v1/places"):
    return client.options(
        path,
        headers={
            "Origin": BROWSER_ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )


def test_preflight_is_answered_without_reaching_a_handler(client: TestClient) -> None:
    # OPTIONS is not a declared method on /places; only the middleware can answer this.
    response = preflight(client)
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "*"
    assert "POST" in response.headers["access-control-allow-methods"]
    assert response.headers["access-control-max-age"] == "600"


def test_simple_requests_carry_the_wildcard_by_default(client: TestClient) -> None:
    for path in ("/api/v1/places", "/api/v1/categories", "/health"):
        response = client.get(path, headers={"Origin": BROWSER_ORIGIN})
        assert response.status_code == 200, path
        assert response.headers["access-control-allow-origin"] == "*", path


def test_generate_route_is_reachable_cross_origin(client: TestClient) -> None:
    response = client.post(
        "/api/v1/routes/generate",
        headers={"Origin": BROWSER_ORIGIN},
        json={"city": "Ростов-на-Дону", "duration_hours": 3},
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "*"


def test_configured_origins_are_echoed_and_others_are_rejected(
    monkeypatch,
) -> None:
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", "https://max.ru, https://minis.max.ru")
    with app_client() as client:
        allowed = client.get("/api/v1/places", headers={"Origin": BROWSER_ORIGIN})
        assert allowed.headers["access-control-allow-origin"] == BROWSER_ORIGIN
        # Echoing one origin per request only makes sense if the cache key is the origin.
        assert allowed.headers["vary"] == "Origin"

        refused = client.get("/api/v1/places", headers={"Origin": "https://evil.example"})
        assert "access-control-allow-origin" not in refused.headers
        assert refused.status_code == 200  # the data itself is public; only the browser is blocked

        denied = client.options(
            "/api/v1/places",
            headers={
                "Origin": "https://evil.example",
                "Access-Control-Request-Method": "POST",
            },
        )
        assert denied.status_code == 400


def test_a_request_without_an_origin_gets_no_cors_headers(client: TestClient) -> None:
    response = client.get("/api/v1/places")
    assert "access-control-allow-origin" not in response.headers


def test_blank_configuration_falls_back_to_the_wildcard(monkeypatch) -> None:
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", " , ,")
    with app_client() as client:
        assert preflight(client).headers["access-control-allow-origin"] == "*"


def test_credentials_are_never_allowed(client: TestClient) -> None:
    # `Access-Control-Allow-Credentials: true` alongside a wildcard origin is a vulnerability, and the
    # endpoints have no cookie session to protect.
    assert "access-control-allow-credentials" not in preflight(client).headers

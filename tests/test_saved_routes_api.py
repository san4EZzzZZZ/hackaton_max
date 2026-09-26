"""Saved routes: a route has to outlive the request that produced it, and outlive the process.

The contract worth storing here is the boring one — what comes back from `GET /routes/{id}` must be
byte-for-byte the answer `POST /routes/generate` gave, and it must still be there after the app is
rebuilt. Everything else on this endpoint is a variation on "the row is in the database": the 404 that
is not a 403 (there is no auth to fail), the totals header that counts rows the page cut away, and the
id the client must not confuse with the uuid4 inside the payload.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from conftest import app_client
from server.routers import saved_routes as saved_routes_module

CITY = "Ростов-на-Дону"
GENERATE = "/api/v1/routes/generate"
ROUTES = "/api/v1/routes"
BROWSER_ORIGIN = "https://minis.max.ru"


def generated(client: TestClient, **body) -> dict:
    return client.post(GENERATE, json={"city": CITY, "duration_hours": 4, **body}).json()


def save(client: TestClient, route: dict, **extra) -> dict:
    response = client.post(ROUTES, json={**route, **extra})
    assert response.status_code == 201, response.text
    return response.json()


def test_a_saved_route_comes_back_as_the_answer_it_was_saved_from(client: TestClient) -> None:
    route = generated(client)
    stored = save(client, route)

    fetched = client.get(f"{ROUTES}/{stored['route_id']}")
    assert fetched.status_code == 200
    assert fetched.json() == stored, "a fetch must not restyle the route it read back"
    assert fetched.json()["route"] == route, "the payload is the generator's answer, verbatim"


def test_the_row_survives_a_restart(client: TestClient) -> None:
    """The point of the whole endpoint: an in-memory store would pass every other test here and fail."""
    with app_client() as first:
        stored = save(first, generated(first), user_id=777)

    with app_client() as second:
        revived = second.get(f"{ROUTES}/{stored['route_id']}")
        assert revived.status_code == 200, "the route was written to the database, not to the process"
        assert revived.json() == stored


def test_the_database_id_and_the_generator_uuid_are_two_different_things(client: TestClient) -> None:
    stored = save(client, generated(client))
    assert isinstance(stored["route_id"], int)
    assert stored["route_id"] != stored["route"]["route_id"], "uuid4 is not addressable here"
    # The uuid4 the client already holds is not a key: fetching by it is a 422 about an integer.
    assert client.get(f"{ROUTES}/{stored['route']['route_id']}").status_code == 422


def test_a_route_about_nothing_the_catalog_knows_is_refused(client: TestClient) -> None:
    route = generated(client)
    route["places"] = [dict(route["places"][0], id="never-existed")]

    response = client.post(ROUTES, json=route)
    assert response.status_code == 422
    assert "never-existed" in response.json()["detail"], "the answer has to name the id it rejected"


def test_a_key_nobody_recognises_is_refused(client: TestClient) -> None:
    response = client.post(ROUTES, json={**generated(client), "owner": 12})
    assert response.status_code == 422
    errors = response.json()["detail"]
    assert any("owner" in str(error["loc"]) for error in errors), "the answer must name the bad key"


def test_an_id_too_large_for_the_column_is_a_422_and_not_a_500(client: TestClient) -> None:
    """sqlite raises OverflowError past 8 bytes, and that is not a SQLAlchemyError.

    So the exception escapes the handlers and answers 500. The bound has to be enforced before the
    query, because the same number arrives both as a path id and as a claimed owner.
    """
    too_big = 2**63
    assert client.get(f"{ROUTES}/{too_big}").status_code == 422
    assert client.get(ROUTES, params={"user_id": too_big}).status_code == 422
    assert client.post(ROUTES, json={**generated(client), "user_id": too_big}).status_code == 422

    # The largest addressable id still reaches the database and is answered as a plain miss.
    assert client.get(f"{ROUTES}/{too_big - 1}").status_code == 404


def test_a_missing_route_is_a_404_and_never_a_403(client: TestClient) -> None:
    # Nothing on this API authenticates, so "not yours" cannot exist as an answer — only "not there".
    response = client.get(f"{ROUTES}/4242")
    assert response.status_code == 404
    assert isinstance(response.json()["detail"], str) and response.json()["detail"].strip()


def test_an_empty_list_is_not_an_error(client: TestClient) -> None:
    response = client.get(ROUTES)
    assert response.status_code == 200
    assert response.json() == []
    assert response.headers["X-Total-Count"] == "0"


def test_the_owner_filter_is_a_scope_not_a_lock(client: TestClient) -> None:
    mine = save(client, generated(client), user_id=11)
    theirs = save(client, generated(client), user_id=22)

    only_mine = client.get(ROUTES, params={"user_id": 11})
    assert [item["route_id"] for item in only_mine.json()] == [mine["route_id"]]
    assert only_mine.headers["X-Total-Count"] == "1"

    # Without the filter the same rows are readable by anyone who asks — documented, not accidental,
    # and reading one route by id does not consult its owner at all.
    everyone = client.get(ROUTES).json()
    assert {item["route_id"] for item in everyone} == {mine["route_id"], theirs["route_id"]}
    assert client.get(f"{ROUTES}/{theirs['route_id']}").status_code == 200


def test_routes_come_back_newest_first(client: TestClient) -> None:
    ids = [save(client, generated(client))["route_id"] for _ in range(3)]
    listed = [item["route_id"] for item in client.get(ROUTES).json()]
    assert listed == list(reversed(ids))


def test_the_total_counts_the_selection_not_the_page(client: TestClient) -> None:
    for _ in range(3):
        save(client, generated(client), user_id=5)

    # The Origin makes this look like the Mini App: the middleware only speaks CORS at requests that
    # carry one.
    page = client.get(
        ROUTES, params={"user_id": 5, "limit": 1}, headers={"Origin": BROWSER_ORIGIN}
    )
    assert len(page.json()) == 1
    assert page.headers["X-Total-Count"] == "3"
    assert page.headers["X-Offset"] == "0"

    # A browser cannot read a custom header the server did not expose; see server/cors.py.
    exposed = {
        name.strip().lower()
        for name in page.headers["access-control-expose-headers"].split(",")
    }
    assert {"x-total-count", "x-offset"} <= exposed

    beyond = client.get(ROUTES, params={"user_id": 5, "offset": 3})
    assert beyond.json() == []
    assert beyond.headers["X-Total-Count"] == "3"


class _DeadDatabase:
    """Stands in for the sqlite file going away mid-request; the handler must not turn that into a 500."""

    async def __aenter__(self):
        raise SQLAlchemyError("database is gone")

    async def __aexit__(self, *_exc) -> bool:
        return False


def test_a_database_that_does_not_answer_is_a_503(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(saved_routes_module, "session_scope", lambda: _DeadDatabase())
    for call in (
        lambda: client.post(ROUTES, json=generated(client)),
        lambda: client.get(ROUTES),
        lambda: client.get(f"{ROUTES}/1"),
    ):
        assert call().status_code == 503


def test_the_spec_documents_the_codes_clients_must_handle(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]
    assert paths[ROUTES]["post"]["responses"]["201"]["content"]["application/json"]["schema"][
        "$ref"
    ].endswith("SavedRouteResponse")
    assert "422" in paths[ROUTES]["post"]["responses"]
    assert "503" in paths[ROUTES]["post"]["responses"]
    assert "404" not in paths[ROUTES]["get"]["responses"], "an empty list is not a missing resource"
    assert "404" in paths[f"{ROUTES}/{{route_id}}"]["get"]["responses"]
    assert "503" in paths[f"{ROUTES}/{{route_id}}"]["get"]["responses"]
    headers = paths[ROUTES]["get"]["responses"]["200"]["headers"]
    assert {"X-Total-Count", "X-Offset"} <= set(headers)

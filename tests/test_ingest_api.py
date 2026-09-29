"""The collector wired to a fake internet, and the admin endpoints that start it.

Nothing in this file may reach the network: the upstreams are shared public services that rate-limit
this IP, and a suite that depends on Overpass answering would fail for reasons that have nothing to
do with the code under test. `httpx.MockTransport` is the seam — `PublicApi` takes a transport
precisely so that the retry, pacing and parsing paths run for real against a canned answer.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

import ingest.job as job_module
from core.config import Settings
from ingest.pipeline import CityReport, ingest_city
from ingest.taxonomy import INTERESTS
from ingest.transport import IngestSourceError, PublicApi
from server.app import create_app
from server.routers import ingest as ingest_router

TOKEN = "sekret-ingest-token"

NOMINATIM = [
    {
        "osm_type": "relation",
        "osm_id": 3437391,
        "lat": "55.79",
        "lon": "49.11",
        "category": "place",
        "type": "city",
        "name": "Тестов",
        "display_name": "Тестов, Тестовская область",
        "boundingbox": ["55.60", "55.95", "48.80", "49.40"],
        "extratags": {"wikidata": "Q9139"},
    }
]

ELEMENTS = [
    {
        "type": "node",
        "id": 11,
        "lat": 55.796,
        "lon": 49.106,
        "tags": {
            "name": "Национальный музей",
            "tourism": "museum",
            "wikidata": "Q4315032",
            "opening_hours": "Mo-Fr 10:00-18:00",
            "addr:street": "улица Кремлёвская",
            "addr:housenumber": "2",
        },
    },
    {
        "type": "way",
        "id": 12,
        "center": {"lat": 55.79, "lon": 49.12},
        "tags": {"name": "Парк крыльев", "leisure": "park"},
    },
    {
        "type": "node",
        "id": 13,
        "lat": 55.78,
        "lon": 49.13,
        "tags": {"name": "Кофейня «Пироги»", "amenity": "cafe", "brand": "Пироги"},
    },
    {
        "type": "node",
        "id": 14,
        "lat": 55.77,
        "lon": 49.14,
        # A parish church with nothing to point at: architecture by tag, not a destination.
        "tags": {"name": "Церковь у вокзала", "amenity": "place_of_worship"},
    },
    {
        "type": "node",
        "id": 15,
        "lat": 55.76,
        "lon": 49.15,
        "tags": {"amenity": "restaurant"},  # unnamed: cannot be shown to anybody
    },
]

SPARQL = {
    "results": {
        "bindings": [
            {
                "item": {"value": "http://www.wikidata.org/entity/Q4315032"},
                "label": {"value": "Национальный музей РТ"},
                "description": {"value": "крупнейший музей Республики Татарстан"},
                "image": {"value": "https://commons.wikimedia.org/wiki/File:Museum.jpg"},
                "ruwiki": {"value": "https://ru.wikipedia.org/wiki/Национальный_музей_РТ"},
            }
        ]
    }
}

COMMONS = {
    "query": {
        "pages": {
            "1": {
                "title": "File:Museum.jpg",
                "imageinfo": [
                    {
                        "thumburl": "https://upload.wikimedia.org/wikipedia/commons/thumb/1/1a/Museum.jpg/960px-Museum.jpg",
                        "url": "https://upload.wikimedia.org/wikipedia/commons/1/1a/Museum.jpg",
                        "extmetadata": {
                            "LicenseShortName": {"value": "CC BY-SA 4.0"},
                            "Artist": {"value": '<a href="https://example.org">Иван Петров</a>'},
                        },
                    }
                ],
            }
        }
    }
}


def _respond(request: httpx.Request) -> httpx.Response:
    host = request.url.host
    if host == "nominatim.test":
        return httpx.Response(200, json=NOMINATIM)
    if host == "overpass.test":
        return httpx.Response(200, json={"elements": ELEMENTS})
    if host == "wikidata.test":
        return httpx.Response(200, json=SPARQL)
    if host == "commons.test":
        return httpx.Response(200, json=COMMONS)
    return httpx.Response(404, text=f"unmocked host {host}")


def api_with(handler, *, retries: int = 0) -> PublicApi:
    return PublicApi(
        user_agent="pytest", gap=0.0, retries=retries, transport=httpx.MockTransport(handler)
    )


def settings_for(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "bot_token": "test-token-not-a-real-one",
        "nominatim_url": "https://nominatim.test",
        "overpass_url": "https://overpass.test/api/interpreter",
        "wikidata_sparql_url": "https://wikidata.test/sparql",
        "commons_api_url": "https://commons.test/w/api.php",
        # A test that slept between requests would be testing the timer, not the code.
        "ingest_request_gap": 0.0,
        "ingest_http_retries": 0,
    }
    base.update(overrides)
    return Settings(**base)


def run_ingest(tmp_path: Path, **overrides: Any):
    """One city pass into the directory this test's catalog is reading — see `uncached_data_files`."""

    async def go():
        async with api_with(_respond) as api:
            return await ingest_city(
                "Тестов",
                settings=settings_for(),
                api=api,
                out_dir=tmp_path / "places.d",
                **overrides,
            )

    return asyncio.run(go())


def written_records(report) -> list[dict]:
    assert report.written is not None, "прогон ничего не записал"
    return json.loads(Path(report.written).read_text(encoding="utf-8"))


# --- the pipeline ----------------------------------------------------------


def test_a_city_pass_writes_places_the_api_serves(tmp_path: Path, client: TestClient) -> None:
    report = run_ingest(tmp_path)

    assert report.city == "Тестов"
    assert report.places == 3, "музей, парк и кофейня: безымянный объект и приход не проходят"
    assert report.published == {"Кафе": 1, "Музей": 1, "Парк": 1}

    records = written_records(report)
    assert len({entry["id"] for entry in records}) == 3, "идентификаторы уникальны"

    served = client.get("/api/v1/places", params={"city": "Тестов"}).json()
    assert {place["title"] for place in served} == {"Национальный музей", "Парк крыльев", "Кофейня «Пироги»"}
    cities = client.get("/api/v1/cities").json()
    assert "Тестов" in {entry["city"] for entry in cities}, "новый город появляется в селекторе"


def test_enrichment_lands_in_the_record(tmp_path: Path) -> None:
    museum = next(entry for entry in written_records(run_ingest(tmp_path)) if entry["category"] == "Музей")

    assert museum["working_hours"] == "пн–пт 10:00–18:00"
    assert museum["address"] == "улица Кремлёвская, 2, Тестов"
    assert museum["image_url"].startswith("https://thumb.wikimedia.org/wikipedia/commons/thumb/")
    assert museum["description"] == "крупнейший музей Республики Татарстан."
    assert museum["tags"] == ["culture", "photo"]
    provenance = museum["provenance"]
    assert provenance["source_url"] == "https://www.wikidata.org/wiki/Q4315032"
    assert provenance["osm_url"] == "https://www.openstreetmap.org/node/11"
    assert provenance["image_credit"] == {
        "author": "Иван Петров",
        "license": "CC BY-SA 4.0",
        "file": "File:Museum.jpg",
    }
    assert "price" in provenance["unverified_fields"], "цену в OSM нет — её не выдают за проверенную"


def test_a_cafe_without_a_wiki_link_is_not_confused_with_the_museum(tmp_path: Path) -> None:
    records = written_records(run_ingest(tmp_path))
    cafe = next(entry for entry in records if entry["category"] == "Кафе")

    assert cafe["tags"] == ["coffee"], "кофейня — это «кофе», а не «перекусить»"
    assert cafe["provenance"]["source_url"] == "https://www.openstreetmap.org/node/13"
    assert "working_hours" in cafe["provenance"]["unverified_fields"]


def test_dry_run_reports_but_writes_nothing(tmp_path: Path) -> None:
    report = run_ingest(tmp_path, dry_run=True)

    assert report.places == 3 and report.written is None
    assert list((tmp_path / "places.d").rglob("*.json")) == []


def test_a_failed_category_is_reported_not_hidden(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "overpass.test":
            return httpx.Response(500, text="upstream exploded")
        return _respond(request)

    async def go():
        async with api_with(handler) as api:
            return await ingest_city(
                "Тестов", settings=settings_for(), api=api, out_dir=tmp_path / "places.d"
            )

    report = asyncio.run(go())

    assert report.places == 0 and report.fetched == {}
    assert report.written is None, "нечего записывать — город остался прежним"
    named = " ".join(report.failures)
    for interest in INTERESTS:
        assert interest.category in named, "отчёт называет каждую потерянную категорию"


def test_an_unknown_city_raises_rather_than_writing_a_neighbour(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "nominatim.test":
            return httpx.Response(200, json=[])
        return _respond(request)

    async def go():
        async with api_with(handler) as api:
            return await ingest_city(
                "Абвгд", settings=settings_for(), api=api, out_dir=tmp_path / "places.d"
            )

    with pytest.raises(IngestSourceError, match="Абвгд"):
        asyncio.run(go())


def test_transport_retries_until_the_upstream_recovers() -> None:
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] < 3:
            return httpx.Response(429, text="slow down", headers={"Retry-After": "0"})
        return httpx.Response(200, json={"ok": True})

    async def go():
        async with api_with(handler, retries=4) as api:
            return await api.json("GET", "https://overpass.test/api/interpreter")

    assert asyncio.run(go()) == {"ok": True}
    assert attempts["count"] == 3


def test_transport_gives_up_on_a_broken_query_without_retrying_it() -> None:
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        return httpx.Response(400, text="syntax error near ')'")

    async def go():
        async with api_with(handler, retries=4) as api:
            return await api.json("GET", "https://overpass.test/api/interpreter")

    with pytest.raises(IngestSourceError, match="syntax error"):
        asyncio.run(go())
    assert attempts["count"] == 1, "400 is the same mistake forever"


# --- admin endpoints -------------------------------------------------------


@pytest.fixture
def auth_client(monkeypatch) -> Iterator[TestClient]:
    """A server whose collector is a stub the test controls, and whose token is this file's.

    The job is a process singleton, so its run state is cleared on the way in: a pass left behind by
    another test would answer 409 before this one had asked for anything.
    """
    job = ingest_router.job
    monkeypatch.setattr(job, "settings", settings_for(ingest_token=TOKEN))
    monkeypatch.setattr(job, "_task", None)
    monkeypatch.setattr(job, "reports", [])
    monkeypatch.setattr(job, "requested", [])
    monkeypatch.setattr(job, "error", None)
    with TestClient(create_app()) as test_client:
        yield test_client


def wait_for_run(client: TestClient, headers: dict[str, str], *, timeout: float = 5.0) -> dict[str, Any]:
    """Sit until the background pass ends.

    The task runs on the event loop in the client's own thread, so it needs wall time rather than an
    `asyncio.sleep` from this one — the loop only advances while this thread is not holding it.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = client.get("/api/v1/admin/ingest/status", headers=headers).json()
        if not status["running"]:
            return status
        time.sleep(0.02)
    raise AssertionError("прогон так и не завершился")


def test_ingestion_is_off_without_a_token(client: TestClient) -> None:
    response = client.post("/api/v1/admin/ingest", json={"cities": ["Казань"]})

    assert response.status_code == 503
    assert "INGEST_TOKEN" in response.json()["detail"]


def test_a_wrong_token_is_rejected_by_both_endpoints(auth_client: TestClient) -> None:
    assert auth_client.get("/api/v1/admin/ingest/status").status_code == 401
    response = auth_client.post(
        "/api/v1/admin/ingest",
        json={"cities": ["Казань"]},
        headers={"X-Ingest-Token": "not-it"},
    )

    assert response.status_code == 401


def test_a_run_is_started_once_and_a_second_request_is_refused(auth_client: TestClient, monkeypatch) -> None:
    hold = threading.Event()

    async def slow_ingest_cities(cities, **kwargs):
        while not hold.is_set():
            await asyncio.sleep(0.01)
        return [
            CityReport(requested=city, city=city, places=5, written=f"data/places.d/{city}.json")
            for city in cities
        ]

    monkeypatch.setattr(job_module, "ingest_cities", slow_ingest_cities)
    headers = {"X-Ingest-Token": TOKEN}

    started = auth_client.post("/api/v1/admin/ingest", json={"cities": ["Казань", "Сочи"]}, headers=headers)
    assert started.status_code == 202
    assert started.json()["status"] == "started"

    again = auth_client.post("/api/v1/admin/ingest", json={"cities": ["Москва"]}, headers=headers)
    assert again.status_code == 409
    assert "Казань" in again.json()["detail"]

    running = auth_client.get("/api/v1/admin/ingest/status", headers=headers).json()
    assert running["enabled"] is True and running["running"] is True
    assert running["reports"] == [], "отчёт появляется только когда прогон закончен"

    hold.set()
    final = wait_for_run(auth_client, headers)
    assert [report["city"] for report in final["reports"]] == ["Казань", "Сочи"]
    assert final["reports"][0]["places"] == 5
    assert final["error"] is None


def test_a_broken_run_is_reported_instead_of_vanishing(auth_client: TestClient, monkeypatch) -> None:
    async def exploding_ingest_cities(cities, **kwargs):
        raise RuntimeError("нет свободного слота на Overpass")

    monkeypatch.setattr(job_module, "ingest_cities", exploding_ingest_cities)
    headers = {"X-Ingest-Token": TOKEN}

    auth_client.post("/api/v1/admin/ingest", json={"cities": ["Казань"]}, headers=headers)

    final = wait_for_run(auth_client, headers)
    assert "нет свободного слота" in final["error"]
    assert final["reports"] == []


def test_dry_run_and_limit_are_passed_through(auth_client: TestClient, monkeypatch) -> None:
    seen: dict[str, Any] = {}

    async def capture(cities, **kwargs):
        seen.update(cities=cities, **kwargs)
        return []

    monkeypatch.setattr(job_module, "ingest_cities", capture)
    auth_client.post(
        "/api/v1/admin/ingest",
        json={"cities": ["Сочи"], "limit": 7, "dry_run": True},
        headers={"X-Ingest-Token": TOKEN},
    )

    final = wait_for_run(auth_client, {"X-Ingest-Token": TOKEN})
    assert seen["cities"] == ["Сочи"]
    assert seen["total_limit"] == 7 and seen["dry_run"] is True
    assert final["reports"] == []


def test_a_city_list_rejects_an_empty_request(auth_client: TestClient) -> None:
    response = auth_client.post(
        "/api/v1/admin/ingest", json={"cities": ["   "]}, headers={"X-Ingest-Token": TOKEN}
    )

    assert response.status_code == 422

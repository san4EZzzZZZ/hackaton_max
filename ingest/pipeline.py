"""The operation, end to end: a city name in, a file of real places out.

Four services are involved and each can fail on its own, so the pipeline is written to degrade by
whole categories rather than by half a city: an interest whose Overpass query timed out is reported
in `failures` and the rest of the city is still published, while a city that resolves to no
settlement at all raises and writes nothing.

`api` and `out_dir` are injectable because this has three callers — the CLI, the admin endpoint and
the test suite — and the last of those must not reach the network.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.config import Settings, get_settings
from ingest.commons import fetch_thumbs
from ingest.geo import resolve_city
from ingest.normalize import Candidate, assign_ids, build_candidates, picture_title, select
from ingest.osm import Element, collect
from ingest.store import write_city
from ingest.taxonomy import INTERESTS
from ingest.transport import IngestSourceError, PublicApi
from ingest.wikidata import fetch_entities
from server import catalog

logger = logging.getLogger(__name__)


@dataclass
class CityReport:
    """What one city run did, in the shape both the CLI prints and the API answers."""

    requested: str
    city: str | None = None
    source_url: str | None = None
    fetched: dict[str, int] = field(default_factory=dict)
    published: dict[str, int] = field(default_factory=dict)
    places: int = 0
    written: str | None = None
    failures: list[str] = field(default_factory=list)

    def summary(self) -> str:
        by_category = ", ".join(f"{key}={value}" for key, value in self.published.items()) or "—"
        where = self.written or "(dry run)"
        return f"{self.city or self.requested}: {self.places} мест ({by_category}) → {where}"


async def ingest_city(
    requested: str,
    *,
    settings: Settings | None = None,
    api: PublicApi | None = None,
    out_dir: Path | None = None,
    total_limit: int | None = None,
    per_category: int | None = None,
    dry_run: bool = False,
) -> CityReport:
    """Discover, refine and store the places of one city. Raises only if the city itself is unknown."""
    config = settings or get_settings()
    report = CityReport(requested=requested)
    own_api = api is None
    client = api or PublicApi(
        user_agent=config.ingest_user_agent,
        timeout=config.ingest_timeout,
        retries=config.ingest_http_retries,
        gap=config.ingest_request_gap,
    )
    try:
        city = await resolve_city(client, config.nominatim_url, requested)
        report.city = city.name
        report.source_url = city.source_url
        logger.info("Resolved %s -> %s (%.3f, %.3f)", requested, city.name, city.lat, city.lon)

        grouped, failures = await collect(client, config.overpass_url, city.bbox, INTERESTS)
        report.failures.extend(failures)
        report.fetched = {category: len(elements) for category, elements in grouped.items()}

        entities = await fetch_entities(
            client, config.wikidata_sparql_url, {qid for elements in grouped.values() for qid in _qids(elements)}
        )
        titles = {
            title
            for elements in grouped.values()
            for element in elements
            if (title := picture_title(element.tags, entities.get(element.wikidata or "")))
        }
        pictures = await fetch_thumbs(client, config.commons_api_url, titles)

        candidates = build_candidates(city, grouped, entities, pictures)
        chosen = select(
            candidates,
            INTERESTS,
            total_limit=total_limit or config.ingest_max_places_per_city,
            per_category=per_category if per_category is not None else config.ingest_max_per_category,
        )
        numbered = assign_ids(chosen)
        report.published = {
            category: sum(1 for item in numbered if item.place.category == category)
            for category in sorted({item.place.category for item in numbered})
        }
        report.places = len(numbered)

        if not numbered:
            report.failures.append("Ни один объект не прошёл отбор — файл города не перезаписан")
            return report

        if dry_run:
            return report

        directory = out_dir or catalog.EXTRA_DIR
        report.written = str(write_city(directory, city.name, [record(item) for item in numbered]))
        catalog.invalidate_cache()
        return report
    finally:
        if own_api:
            await client.aclose()


def _qids(elements: tuple[Element, ...]) -> set[str]:
    return {element.wikidata for element in elements if element.wikidata}


def record(candidate: Candidate) -> dict[str, Any]:
    """A `Place` as JSON, plus the `provenance` block the seed already uses.

    The key is invisible to the API — `Place` is `extra="ignore"` — and exists for the person
    reviewing a generated city: it says where each number came from and which ones were inferred.
    """
    return {**candidate.place.model_dump(mode="json"), "provenance": candidate.provenance}


async def ingest_cities(
    requested: Iterable[str],
    *,
    settings: Settings | None = None,
    out_dir: Path | None = None,
    total_limit: int | None = None,
    per_category: int | None = None,
    dry_run: bool = False,
) -> list[CityReport]:
    """Several cities over one client, one report each.

    One `PublicApi` for the whole run is the point: the per-host pacing that keeps Overpass from
    answering 429 is only meaningful if the requests share a clock, and a second client would start
    its own timer at zero.
    """
    config = settings or get_settings()
    reports: list[CityReport] = []
    async with PublicApi(
        user_agent=config.ingest_user_agent,
        timeout=config.ingest_timeout,
        retries=config.ingest_http_retries,
        gap=config.ingest_request_gap,
    ) as api:
        for name in requested:
            try:
                reports.append(
                    await ingest_city(
                        name,
                        settings=config,
                        api=api,
                        out_dir=out_dir,
                        total_limit=total_limit,
                        per_category=per_category,
                        dry_run=dry_run,
                    )
                )
            except IngestSourceError as error:
                # A name that resolves to no settlement, an upstream that stayed down: reported as
                # that city's failure, because the other cities in the run are already paid for.
                logger.error("Ingest of %s failed: %s", name, error)
                reports.append(CityReport(requested=name, failures=[str(error)]))
    return reports

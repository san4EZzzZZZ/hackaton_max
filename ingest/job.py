"""The running job behind `POST /api/v1/admin/ingest`.

One city takes tens of seconds — Overpass is slow and the point is not to hammer it — so the
endpoint starts the work and returns. This module is the single place that knows whether a run is in
progress, which is what stops a second request from starting a parallel fetch that would trip the
upstream rate limit and have two writers replacing the same file.

State lives in the process, so it describes whichever container answers the request. That is enough
for what the endpoint is for — an operator's button and a status line — and it is why a finished run
leaves its report on disk as `data/places.d/<город>.json`, which is the durable answer.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from core.config import Settings, get_settings
from ingest.pipeline import CityReport, ingest_cities

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class IngestJob:
    """Starts a city pass, remembers the last one, refuses to overlap with itself."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._task: asyncio.Task[list[CityReport]] | None = None
        self.started_at: datetime | None = None
        self.finished_at: datetime | None = None
        self.reports: list[CityReport] = []
        self.error: str | None = None
        self.requested: list[str] = []

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self, cities: list[str], *, limit: int | None, dry_run: bool) -> bool:
        """Begin a pass; False when one is already going.

        No await between the guard and `create_task`, which is what makes the check sound: on one
        event loop no other coroutine can slip a start in between.
        """
        if self.running:
            return False
        self.requested = list(cities)
        self.reports = []
        self.error = None
        self.started_at = _utcnow()
        self.finished_at = None
        self._task = asyncio.create_task(self._run(cities, limit=limit, dry_run=dry_run))
        return True

    async def _run(self, cities: list[str], *, limit: int | None, dry_run: bool) -> list[CityReport]:
        try:
            reports = await ingest_cities(
                cities, settings=self.settings, total_limit=limit, dry_run=dry_run
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - the run must not die silently
            logger.exception("Ingest run failed")
            self.error = f"{type(error).__name__}: {error}"
            return []
        self.reports = reports
        return reports

    async def wait(self, timeout: float | None = None) -> None:
        """Let a caller — a test, or a shutdown hook — sit until the current pass is over."""
        if self._task is not None:
            await asyncio.wait_for(asyncio.shield(self._task), timeout)

    async def cancel(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                logger.info("Ingest run cancelled")
        self.finished_at = _utcnow()

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.settings.ingest_open,
            "running": self.running,
            "started_at": self.started_at,
            # A pass still in progress has no report yet: the list is filled when it finishes, so the
            # status line says `running` rather than showing a partial city count.
            "finished_at": None if self.running else self.finished_at,
            "reports": [report.__dict__ for report in self.reports],
            "error": self.error,
        }


#: One job per process — see the module docstring for what that does and does not promise.
job = IngestJob()

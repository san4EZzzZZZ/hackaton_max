"""One HTTP client for every open endpoint the ingester talks to.

The public OpenStreetMap and Wikimedia services are shared, rate-limited infrastructure: Overpass
answers 429 when a client comes too fast and 504 when a query is heavier than its slot, Nominatim
closes the connection on a generic User-Agent. So this layer owns the two things a single request
cannot do for itself — waiting its turn per host, and coming back after a backoff — and the modules
above it only see either JSON or an `IngestSourceError`.

TLS verification stays strict here. `SSL_CA_BUNDLE` exists for MAX's chain rooted in the Russian
Ministry of Digital Development CA; these are ordinary public certificates, and widening the trust
store to reach them would weaken both.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

#: Retried. 408 and 5xx are transient on shared instances; 400 is a broken query and will fail
#: identically forever, so it is not in this set.
RETRY_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})


class IngestSourceError(RuntimeError):
    """An upstream endpoint stayed unavailable — reported per city, never as a partial catalog."""


class PublicApi:
    """Paced and retried JSON requests against a set of public hosts."""

    def __init__(
        self,
        *,
        user_agent: str,
        timeout: float = 180.0,
        retries: int = 5,
        gap: float = 2.0,
        max_backoff: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._gap = gap
        self._retries = max(0, retries)
        self._max_backoff = max_backoff
        self._next_allowed: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._http = httpx.AsyncClient(
            headers={"User-Agent": user_agent},
            # Overpass holds a connection open while its queue drains, so a short connect timeout is
            # what turns a busy server into a failed city.
            timeout=httpx.Timeout(timeout, connect=30.0),
            follow_redirects=True,
            limits=httpx.Limits(max_connections=6, max_keepalive_connections=4),
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> PublicApi:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def json(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        """The decoded JSON body, or `IngestSourceError` once the retries run out."""
        host = httpx.URL(url).host
        last_error = ""
        for attempt in range(self._retries + 1):
            await self._wait_turn(host)
            try:
                response = await self._http.request(method, url, params=params, data=data, headers=headers)
            except httpx.HTTPError as error:
                last_error = f"{type(error).__name__}: {error}"
            else:
                if response.status_code < 400:
                    try:
                        return response.json()
                    except ValueError as error:
                        # A 200 that is not JSON is an HTML error page from a proxy: retryable, because
                        # the next attempt on these hosts usually is not it.
                        last_error = f"not JSON: {response.text[:160]!r} ({error})"
                elif response.status_code not in RETRY_STATUSES:
                    raise IngestSourceError(
                        f"{method} {url} -> HTTP {response.status_code}: {response.text[:300]}"
                    )
                else:
                    last_error = f"HTTP {response.status_code}: {response.text[:160]}"
                    retry_after = _retry_after(response)
                    if retry_after is not None:
                        await asyncio.sleep(min(retry_after, self._max_backoff))
                        continue
            if attempt < self._retries:
                delay = self._backoff(attempt)
                logger.warning("Retrying %s %s in %.1fs (%s)", method, url, delay, last_error)
                await asyncio.sleep(delay)
        raise IngestSourceError(f"{method} {url} failed after {self._retries + 1} attempts: {last_error}")

    def _backoff(self, attempt: int) -> float:
        """Exponential with jitter: a crowd of retriers that all wake at once is a second outage."""
        ceiling = min(self._max_backoff, 2.0 * 2**attempt)
        return ceiling * (0.6 + random.random() * 0.4)

    async def _wait_turn(self, host: str) -> None:
        """Space requests to one host by `gap`, serialised so concurrent callers cannot both go now.

        The wait happens inside the lock on purpose: two coroutines that computed the same sleep
        outside of it would both wake at the boundary and arrive together anyway.
        """
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            now = time.monotonic()
            earliest = self._next_allowed.get(host, 0.0)
            if now < earliest:
                await asyncio.sleep(earliest - now)
            self._next_allowed[host] = time.monotonic() + self._gap


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After", "")
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None

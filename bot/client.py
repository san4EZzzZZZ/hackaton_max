"""Async MAX Bot API client: one shared connection pool, retries and per-dialog pacing."""

from __future__ import annotations

import asyncio
import logging
import ssl
import time
from collections.abc import Sequence
from typing import Any, Self

import certifi
import httpx

from bot.models import (
    BotCommand,
    CallbackAnswerPayload,
    InlineKeyboardAttachment,
    SendMessagePayload,
    SubscriptionsList,
    Update,
    UpdatesPage,
)
from core.config import Settings
from core.config import settings as default_settings

logger = logging.getLogger(__name__)

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
MAX_SUBSCRIPTION_TYPES = ("message_created", "message_callback", "bot_started")


class MaxApiError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class MaxClient:
    """Wraps `https://platform-api2.max.ru` with the auth header MAX expects (raw token)."""

    def __init__(self, app_settings: Settings | None = None, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.settings = app_settings or default_settings
        self._verify: bool | ssl.SSLContext = self._resolve_tls_verify()
        self._http = httpx.AsyncClient(
            base_url=self.settings.bot_api_url,
            headers={"Authorization": self.settings.bot_token},
            timeout=httpx.Timeout(self.settings.http_timeout, connect=10.0),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            verify=self._verify,
            transport=transport,
        )
        self._last_sent: dict[str, float] = {}
        self._pacing_lock = asyncio.Lock()

    def _resolve_tls_verify(self) -> bool | ssl.SSLContext:
        """certifi roots plus MAX's Russian CA root, so verification never has to be disabled."""
        if not self.settings.ssl_verify:
            logger.warning("TLS verification disabled via SSL_VERIFY — development only")
            return False
        context = ssl.create_default_context(cafile=certifi.where())
        bundle = self.settings.ca_bundle_path
        if bundle is None and self.settings.ssl_ca_bundle:
            logger.warning(
                "Extra CA bundle %r not found; MAX's TLS chain is rooted in the Russian Ministry of "
                "Digital Development CA, which the default trust store does not contain",
                self.settings.ssl_ca_bundle,
            )
        elif bundle:
            context.load_verify_locations(cafile=str(bundle))
        return context

    # ------------------------------------------------------------------ transport

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        read_timeout: float | None = None,
    ) -> dict[str, Any]:
        clean_params = {k: v for k, v in (params or {}).items() if v is not None}
        attempts = self.settings.http_max_retries
        for attempt in range(attempts):
            try:
                response = await self._http.request(
                    method,
                    path,
                    params=clean_params or None,
                    json=json_body,
                    timeout=read_timeout or self.settings.http_timeout,
                )
            except httpx.TransportError as error:
                if attempt == attempts - 1:
                    raise MaxApiError(f"{method} {path}: {type(error).__name__}: {error}") from error
                delay = self._backoff(attempt, None)
                logger.warning("%s %s failed (%s), retrying in %.1fs", method, path, error, delay)
                await asyncio.sleep(delay)
                continue

            if response.status_code >= 400:
                body = response.text[:500]
                if response.status_code in RETRY_STATUSES and attempt < attempts - 1:
                    delay = self._backoff(attempt, response.headers.get("Retry-After"))
                    logger.warning(
                        "%s %s -> %d, retrying in %.1fs", method, path, response.status_code, delay
                    )
                    await asyncio.sleep(delay)
                    continue
                raise MaxApiError(
                    f"{method} {path} -> HTTP {response.status_code}: {body}",
                    status_code=response.status_code,
                )

            if not response.content:
                return {}
            return response.json()

        raise MaxApiError(f"{method} {path}: retries exhausted")  # pragma: no cover

    @staticmethod
    def _backoff(attempt: int, retry_after: str | None) -> float:
        if retry_after:
            try:
                return min(float(retry_after), 15.0)
            except ValueError:
                pass
        return min(0.5 * 2**attempt, 8.0)

    async def _throttle(self, key: str) -> None:
        """MAX allows 2 messages per second per dialog; keep a safety margin."""
        async with self._pacing_lock:
            wait = self.settings.rate_limit_per_chat - (time.monotonic() - self._last_sent.get(key, 0.0))
            if wait > 0:
                await asyncio.sleep(wait)
            if len(self._last_sent) > 4096:
                self._last_sent.clear()
            self._last_sent[key] = time.monotonic()

    # -------------------------------------------------------------------- messages

    async def send_message(
        self,
        text: str | None = None,
        *,
        user_id: int | None = None,
        chat_id: int | None = None,
        attachments: Sequence[InlineKeyboardAttachment] = (),
        format: str | None = "markdown",
        notify: bool = True,
    ) -> dict[str, Any]:
        if user_id is None and chat_id is None:
            raise ValueError("send_message requires user_id or chat_id")
        payload = SendMessagePayload(text=text, attachments=list(attachments), format=format, notify=notify)
        target = {"user_id": user_id, "chat_id": chat_id}
        await self._throttle(f"{user_id or chat_id}")
        return await self._request(
            "POST",
            "messages",
            params={k: v for k, v in target.items() if v is not None},
            json_body=payload.as_json(),
        )

    async def reply_update(
        self,
        update: Update,
        text: str,
        attachments: Sequence[InlineKeyboardAttachment] = (),
    ) -> dict[str, Any]:
        target = update.reply_target
        return await self.send_message(text, attachments=attachments, **target)

    async def answer_callback(
        self,
        callback_id: str,
        text: str,
        attachments: Sequence[InlineKeyboardAttachment] = (),
        *,
        notify: bool = False,
    ) -> dict[str, Any]:
        """Answer a button tap: MAX replaces the message that carried the buttons, so this both
        stops the spinner and refreshes the keyboard in place. An empty body is a 400."""
        if not callback_id:
            return {}
        message = SendMessagePayload(
            text=text, attachments=list(attachments), format="markdown", notify=notify
        )
        body = CallbackAnswerPayload(message=message).as_json()
        return await self._request("POST", "answers", params={"callback_id": callback_id}, json_body=body)

    # -------------------------------------------------------------------- updates

    async def get_updates(self, marker: int | None = None) -> UpdatesPage:
        timeout = self.settings.polling_timeout
        data = await self._request(
            "GET",
            "updates",
            params={"limit": self.settings.polling_limit, "timeout": timeout, "marker": marker},
            read_timeout=timeout + 15.0,
        )
        return UpdatesPage.model_validate(data)

    # ------------------------------------------------------------- bot and webhook

    async def get_me(self) -> dict[str, Any]:
        return await self._request("GET", "me")

    async def set_commands(self, commands: Sequence[BotCommand]) -> dict[str, Any]:
        return await self._request(
            "PATCH",
            "me/commands",
            json_body={"commands": [command.model_dump(exclude_none=True) for command in commands]},
        )

    async def list_subscriptions(self) -> list[str]:
        data = await self._request("GET", "subscriptions")
        return SubscriptionsList.model_validate(data or {"subscriptions": []}).urls

    async def subscribe(self, url: str, *, update_types: Sequence[str] = MAX_SUBSCRIPTION_TYPES) -> dict[str, Any]:
        body: dict[str, Any] = {"url": url, "update_types": list(update_types)}
        if self.settings.webhook_secret:
            body["secret"] = self.settings.webhook_secret
        return await self._request("POST", "subscriptions", json_body=body)

    async def unsubscribe(self, url: str) -> dict[str, Any]:
        return await self._request("DELETE", "subscriptions", params={"url": url})

    async def ensure_webhook(self, url: str) -> list[str]:
        """MAX keeps every subscription it ever got, so the list is rebuilt from scratch:
        one URL, the current secret and the current update types — no stale duplicates.
        Returns the URLs that were replaced."""
        removed: list[str] = []
        for stale in await self.list_subscriptions():
            await self.unsubscribe(stale)
            removed.append(stale)
        await self.subscribe(url)
        logger.info("Subscribed MAX webhook %s (replaced %d previous)", url, len(removed))
        return removed

    # --------------------------------------------------------------------- closing

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

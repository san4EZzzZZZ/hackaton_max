"""Routing layer: turns MAX updates into handler calls, once per event."""

from __future__ import annotations

import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import TypeAlias

from bot.client import MaxApiError, MaxClient
from bot.models import InlineKeyboardAttachment, Update, UserRef
from core.config import Settings
from server.database import session_scope, upsert_user

logger = logging.getLogger(__name__)

Handler: TypeAlias = Callable[["Context"], Awaitable[None]]
SEEN_LIMIT = 4096


@dataclass(slots=True)
class Context:
    """Everything a handler needs to answer one update."""

    update: Update
    client: MaxClient

    @property
    def settings(self) -> Settings:
        return self.client.settings

    @property
    def user(self) -> UserRef | None:
        return self.update.event_user

    @property
    def user_id(self) -> int:
        user = self.user
        return user.user_id if user else 0

    @property
    def text(self) -> str:
        return self.update.text

    async def reply(self, text: str, attachments: Sequence[InlineKeyboardAttachment] = ()) -> None:
        await self.client.reply_update(self.update, text, attachments)

    async def answer(self, text: str, attachments: Sequence[InlineKeyboardAttachment] = ()) -> bool:
        """Refresh the message the tapped button belongs to.

        Returns False when MAX refused the answer — callback ids expire, and the tap must still
        produce something, so the caller falls back to sending a new message.
        """
        callback = self.update.callback
        if callback is None or not callback.callback_id:
            return False
        try:
            await self.client.answer_callback(callback.callback_id, text, attachments)
        except MaxApiError as error:
            logger.info("Cannot answer callback %s: %s", callback.callback_id, error)
            return False
        return True


class Dispatcher:
    def __init__(self, client: MaxClient) -> None:
        self.client = client
        self._events: dict[str, Handler] = {}
        self._commands: dict[str, Handler] = {}
        self._fallback: Handler | None = None
        self._seen: OrderedDict[str, None] = OrderedDict()

    def event(self, update_type: str) -> Callable[[Handler], Handler]:
        def decorator(handler: Handler) -> Handler:
            self._events[update_type] = handler
            return handler

        return decorator

    def command(self, name: str) -> Callable[[Handler], Handler]:
        def decorator(handler: Handler) -> Handler:
            self._commands[name.lstrip("/").lower()] = handler
            return handler

        return decorator

    def fallback(self) -> Callable[[Handler], Handler]:
        def decorator(handler: Handler) -> Handler:
            self._fallback = handler
            return handler

        return decorator

    def _mark_seen(self, key: str) -> bool:
        """Return False when this event was already processed (MAX redelivers on slow ACKs)."""
        if key in self._seen:
            return False
        self._seen[key] = None
        while len(self._seen) > SEEN_LIMIT:
            self._seen.popitem(last=False)
        return True

    async def feed(self, update: Update) -> str:
        """Handle one update; never raises, so a bad event cannot stop polling."""
        key = update.dedupe_key
        if not self._mark_seen(key):
            logger.debug("Skipping duplicate update %s", key)
            return "duplicate"

        handler = self._resolve(update)
        if handler is None:
            logger.debug("No handler for update_type=%s", update.update_type)
            return "ignored"

        context = Context(update=update, client=self.client)
        try:
            await self._remember_user(context)
            await handler(context)
        except Exception:
            self._seen.pop(key, None)
            logger.exception("Failed to handle %s (%s)", update.update_type, key)
            return "error"
        return "handled"

    def _resolve(self, update: Update) -> Handler | None:
        if update.update_type == "message_created":
            command = update.command
            if command:
                return self._commands.get(command) or self._fallback
            return self._fallback
        return self._events.get(update.update_type)

    async def _remember_user(self, context: Context) -> None:
        user = context.user
        if user is None or user.is_bot or not context.user_id:
            return
        recipient = context.update.message.recipient if context.update.message else None
        async with session_scope() as session:
            await upsert_user(
                session,
                user_id=user.user_id,
                username=user.username,
                first_name=user.first_name or user.name,
                last_name=user.last_name,
                locale=context.update.user_locale,
                chat_id=recipient.chat_id if recipient else context.update.chat_id,
            )

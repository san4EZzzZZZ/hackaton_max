"""Keyboard builders: MAX renders inline keyboards as message attachments."""

from __future__ import annotations

import re
from collections.abc import Sequence
from urllib.parse import quote

from bot.models import (
    BotCommand,
    Button,
    CallbackButton,
    InlineKeyboardAttachment,
    KeyboardPayload,
    LinkButton,
    MessageButton,
    OpenAppButton,
)
from core.config import Settings
from core.config import settings as default_settings

START_ACTION = "start"
MAX_KEYBOARD_ROWS = 30
_APP_PAYLOAD_FORBIDDEN = re.compile(r"[^A-Za-z0-9_-]")


def sanitize_app_payload(payload: str) -> str:
    """MAX answers `proto.payload` for any `open_app` payload outside `[A-Za-z0-9_-]`."""
    return _APP_PAYLOAD_FORBIDDEN.sub("_", payload)


def mini_app_link(app_settings: Settings | None = None, payload: str = START_ACTION) -> str:
    """Deep link that opens this bot (and its bound Mini App) inside the MAX client."""
    cfg = app_settings or default_settings
    if cfg.mini_app_url:
        return cfg.mini_app_url
    if not cfg.bot_username:
        return cfg.public_base_url
    link = f"https://max.ru/{cfg.bot_username}"
    return f"{link}?startapp={quote(payload, safe='')}" if payload else link


def open_app_button(
    text: str, payload: str = START_ACTION, app_settings: Settings | None = None
) -> Button:
    """`open_app` launches the Mini App; MAX demands `web_app` and a `[A-Za-z0-9_-]` payload.

    Without `MINI_APP_URL` there is no app to open, so the same call-to-action degrades to a deep
    link that still opens the bot — the message never fails with `proto.payload`.
    """
    cfg = app_settings or default_settings
    if not cfg.mini_app_url:
        return LinkButton(text=text, url=mini_app_link(cfg))
    return OpenAppButton(text=text, web_app=cfg.mini_app_url, payload=sanitize_app_payload(payload))


def callback_button(text: str, payload: str) -> CallbackButton:
    return CallbackButton(text=text, payload=payload)


def message_button(text: str) -> MessageButton:
    return MessageButton(text=text)


def inline_keyboard(rows: Sequence[Sequence[Button]]) -> InlineKeyboardAttachment:
    return InlineKeyboardAttachment(payload=KeyboardPayload(buttons=[list(row) for row in rows]))


def merge_keyboards(*attachments: InlineKeyboardAttachment) -> InlineKeyboardAttachment:
    """MAX accepts exactly one inline keyboard per message, so several CTAs become one grid."""
    rows = [row for attachment in attachments for row in attachment.payload.buttons]
    if len(rows) > MAX_KEYBOARD_ROWS:
        raise ValueError(f"Keyboard has {len(rows)} rows, MAX allows {MAX_KEYBOARD_ROWS}")
    return inline_keyboard(rows)


def mini_app_keyboard(app_settings: Settings | None = None) -> InlineKeyboardAttachment:
    """Primary call-to-action under the message: open the Mini App, refresh, web version."""
    cfg = app_settings or default_settings
    rows: list[list[Button]] = [
        [open_app_button("🧭 Открыть навигатор", f"{START_ACTION}_menu", cfg)],
        [
            callback_button("🔄 Обновить", "action:refresh"),
            LinkButton(text="🌐 Веб-версия", url=cfg.public_base_url or mini_app_link(cfg)),
        ],
    ]
    return inline_keyboard(rows)


def suggested_actions_keyboard() -> InlineKeyboardAttachment:
    """Quick-reply row: MAX has no persistent keyboard, so taps send preset texts."""
    return inline_keyboard(
        [[message_button(text)] for text in ("Маршрут на выходные", "Что интересного рядом", "Помощь")]
    )


def bot_commands() -> list[BotCommand]:
    """Menu next to the input field — the MAX equivalent of a persistent command keyboard."""
    return [
        BotCommand(name="start", description="Открыть мини-приложение"),
        BotCommand(name="help", description="Что умеет навигатор"),
        BotCommand(name="route", description="Подобрать маршрут"),
    ]

"""Welcome flows: `/start`, the Mini App call-to-action and the free-text fallback."""

from __future__ import annotations

import logging

from bot.dispatcher import Context, Dispatcher
from bot.keyboards import merge_keyboards, mini_app_keyboard, suggested_actions_keyboard

logger = logging.getLogger(__name__)

GREETING = (
    "👋 Привет, {name}! Я **{bot}** — цифровой навигатор по маршрутам выходного дня.\n\n"
    "Соберу прогулку под твои интересы: музеи, парки, еда и события рядом. "
    "Открой мини-приложение кнопкой ниже или просто напиши, куда хочешь сходить."
)
HELP_TEXT = (
    "🧭 **Что я умею**\n\n"
    "• `/start` — показать кнопку мини-приложения\n"
    "• `/route` — быстрый запрос на подбор маршрута\n"
    "• обычное сообщение — расскажи, что интересно, и я предложу варианты\n\n"
    "Кнопки под сообщениями дублируют эти действия, печатать не обязательно."
)
ROUTE_TEXT = "🗺 Запускаю подбор маршрута — открой мини-приложение, там уже готова анкета."


def _greeting(context: Context) -> str:
    user = context.user
    name = user.display_name if user else "путешественник"
    return GREETING.format(name=name, bot=context.settings.bot_name or "МАХ навигатор")


async def send_welcome(context: Context) -> None:
    await context.reply(_greeting(context), [mini_app_keyboard(context.settings)])


async def handle_start(context: Context) -> None:
    logger.info("/start from user %s", context.user_id)
    await send_welcome(context)


async def handle_text(context: Context) -> None:
    """Any plain message (including taps on `message` buttons) answers with the Mini App CTA."""
    text = context.text.strip()
    logger.info("Message from %s: %.80s", context.user_id, text or "(empty)")
    if not text:
        await send_welcome(context)
        return
    await context.reply(
        f"Принял: «{text}».\n\nУже ищу варианты — детали в мини-приложении 👇",
        [merge_keyboards(mini_app_keyboard(context.settings), suggested_actions_keyboard())],
    )


async def handle_callback(context: Context) -> None:
    """A tap must be answered explicitly: MAX only stops the button spinner after `/answers`,
    and the answer body replaces the message that carried the buttons. `message` is required —
    an empty body is rejected with 400, so the refreshed keyboard is what we send back."""
    callback = context.update.callback
    payload = (callback.payload if callback else None) or ""
    logger.info("Callback %s from user %s", payload or "(empty)", context.user_id)
    if not await context.answer(_greeting(context), [mini_app_keyboard(context.settings)]):
        await send_welcome(context)


async def handle_help(context: Context) -> None:
    await context.reply(HELP_TEXT, [mini_app_keyboard(context.settings)])


async def handle_route(context: Context) -> None:
    await context.reply(ROUTE_TEXT, [mini_app_keyboard(context.settings)])


async def handle_bot_started(context: Context) -> None:
    """Fires when the user opens the bot — the primary entry point in MAX."""
    logger.info("bot_started by user %s", context.user_id)
    await send_welcome(context)


def register(dispatcher: Dispatcher) -> Dispatcher:
    dispatcher.event("bot_started")(handle_bot_started)
    dispatcher.event("message_callback")(handle_callback)
    dispatcher.command("start")(handle_start)
    dispatcher.command("help")(handle_help)
    dispatcher.command("route")(handle_route)
    dispatcher.fallback()(handle_text)
    return dispatcher

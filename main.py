"""Single entrypoint for the bot.

    python main.py --mode=webhook          # production: FastAPI + uvicorn behind HTTPS
    python main.py --mode=polling          # local development over long polling
    python main.py --mode=setup-webhook    # register/renew the MAX webhook subscription and exit
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

from bot.client import MaxApiError, MaxClient
from bot.dispatcher import Dispatcher
from bot.handlers.start import register
from bot.keyboards import bot_commands
from core.config import Settings, configure_logging, get_settings
from server.app import create_app
from server.database import dispose_db, init_db

logger = logging.getLogger("main")


def _install_stop_signal(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, ValueError, RuntimeError):  # Windows: Ctrl+C raises instead
            try:
                signal.signal(sig, lambda *_: stop.set())
            except (ValueError, OSError):  # pragma: no cover - non-main thread
                pass


async def _bootstrap(config: Settings) -> tuple[MaxClient, Dispatcher]:
    await init_db()
    client = MaxClient(config)
    dispatcher = register(Dispatcher(client=client))
    if config.auto_setup:
        try:
            await client.set_commands(bot_commands())
            logger.info("Registered bot commands menu")
        except MaxApiError as error:
            logger.warning("Could not register bot commands: %s", error)
    return client, dispatcher


async def run_polling(config: Settings) -> int:
    """Long polling loop — no public HTTPS endpoint needed."""
    client, dispatcher = await _bootstrap(config)
    stop = asyncio.Event()
    _install_stop_signal(stop)
    marker: int | None = None
    logger.info(
        "Long polling %s (timeout=%ds, limit=%d) — press Ctrl+C to stop",
        config.bot_api_url,
        config.polling_timeout,
        config.polling_limit,
    )
    try:
        while not stop.is_set():
            try:
                page = await client.get_updates(marker)
            except MaxApiError as error:
                logger.error("Polling failed: %s", error)
                await _sleep_or_stop(stop, 3.0)
                continue

            for update in page.updates:
                if await dispatcher.feed(update) == "error":
                    break
            else:
                if page.marker is not None:
                    marker = page.marker
            if page.updates:
                logger.info("Processed %d update(s), marker=%s", len(page.updates), marker)
    finally:
        await client.aclose()
        await dispose_db()
        logger.info("Long polling stopped")
    return 0


async def _sleep_or_stop(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except TimeoutError:
        pass


async def setup_webhook(config: Settings) -> int:
    if not config.webhook_subscribable:
        logger.error("Cannot register the webhook: %s", config.webhook_setup_hint)
        logger.error("Generate a secret with: openssl rand -hex 32")
        return 2
    url = config.resolved_webhook_url
    async with MaxClient(config) as client:
        try:
            removed = await client.ensure_webhook(url)
            await client.set_commands(bot_commands())
            me = await client.get_me()
        except MaxApiError as error:
            logger.error("Webhook setup failed: %s", error)
            return 1
    logger.info(
        "Webhook %s is live (replaced: %s) — bot @%s id %s",
        url,
        ", ".join(removed) or "none",
        me.get("username") or config.bot_username or "?",
        me.get("user_id", config.bot_user_id or "unknown"),
    )
    return 0


def run_webhook(config: Settings, host: str, port: int, reload: bool) -> int:
    import uvicorn

    uvicorn.run(
        "server.app:app" if reload else create_app(config),
        host=host,
        port=port,
        reload=reload,
        log_level=config.log_level.lower(),
        access_log=config.log_level == "DEBUG",
    )
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MAX messenger bot runner")
    parser.add_argument(
        "--mode",
        choices=("webhook", "polling", "setup-webhook"),
        default="webhook",
        help="webhook for the server, polling for local development (default: webhook)",
    )
    parser.add_argument("--host", default=None, help="override SERVER_HOST")
    parser.add_argument("--port", type=int, default=None, help="override SERVER_PORT")
    parser.add_argument("--reload", action="store_true", help="uvicorn autoreload (webhook mode)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = get_settings()
    configure_logging(config.log_level)

    if args.mode == "setup-webhook":
        return asyncio.run(setup_webhook(config))

    if args.mode == "polling":
        try:
            return asyncio.run(run_polling(config))
        except KeyboardInterrupt:
            logger.info("Interrupted")
            return 0

    return run_webhook(config, args.host or config.server_host, args.port or config.server_port, args.reload)


if __name__ == "__main__":
    sys.exit(main())

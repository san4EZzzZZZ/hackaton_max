"""FastAPI application: validates MAX updates, ACKs fast, dispatches in the background."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from bot.client import MaxApiError, MaxClient
from bot.dispatcher import Dispatcher
from bot.handlers.start import register
from bot.keyboards import bot_commands
from bot.models import Update
from core.config import Settings, configure_logging, get_settings
from server.cors import configure_cors
from server.routers import api_router
from server.database import (
    count_users,
    dispose_db,
    init_db,
    ping_db,
    session_scope,
)

logger = logging.getLogger(__name__)

WEBHOOK_SECRET_HEADER = "X-Max-Bot-Api-Secret"


async def _run_setup(client: MaxClient, config: Settings) -> None:
    """Best-effort bot bootstrap: command menu next to the input field and the webhook."""
    try:
        await client.set_commands(bot_commands())
        logger.info("Registered %d bot commands", len(bot_commands()))
    except MaxApiError as error:
        logger.warning("Could not register bot commands: %s", error)

    if not config.webhook_subscribable:
        logger.warning("Webhook registration skipped: %s", config.webhook_setup_hint)
        return
    url = config.resolved_webhook_url
    try:
        removed = await client.ensure_webhook(url)
        if removed:
            logger.info(
                "Replaced %d previous webhook subscription(s): %s", len(removed), ", ".join(removed)
            )
    except MaxApiError as error:
        logger.error("Webhook subscription failed for %s: %s", url, error)


def create_app(config: Settings | None = None) -> FastAPI:
    settings = config or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(settings.log_level)
        await init_db()
        client = MaxClient(settings)
        app.state.settings = settings
        app.state.client = client
        app.state.dispatcher = Dispatcher(client=client)
        register(app.state.dispatcher)
        app.state.setup_task = (
            asyncio.create_task(_run_setup(client, settings)) if settings.auto_setup else None
        )
        logger.info(
            "Webhook server ready (bot=%s api=%s)",
            settings.bot_username or settings.bot_name or "unknown",
            settings.bot_api_url,
        )
        if not settings.webhook_secret:
            logger.warning(
                "WEBHOOK_SECRET is empty: /webhook accepts unauthenticated payloads — set it in .env "
                "before exposing this server to the internet"
            )
        try:
            yield
        finally:
            task = app.state.setup_task
            if task and not task.done():
                task.cancel()
            await client.aclose()
            await dispose_db()

    app = FastAPI(
        title="MAX Messenger Bot — MVP",
        description=(
            "Webhook receiver for the MAX Mini App bot plus the places & routes API "
            "consumed by the Mini App frontend."
        ),
        version="1.0.0",
        lifespan=lifespan,
    )
    configure_cors(app)
    app.include_router(api_router)

    @app.post("/webhook")
    async def webhook(request: Request, update: Update) -> JSONResponse:
        expected = request.app.state.settings.webhook_secret
        presented = request.headers.get(WEBHOOK_SECRET_HEADER)
        if not expected or presented != expected:
            logger.warning("Rejected webhook delivery with bad secret")
            return JSONResponse(status_code=401, content={"status": "unauthorized"})

        dispatcher: Dispatcher = request.app.state.dispatcher
        await dispatcher.feed(update)
        return JSONResponse(content={"status": "accepted"})

    @app.exception_handler(RequestValidationError)
    async def malformed_update(request: Request, exc: RequestValidationError) -> JSONResponse:
        """Ack unreadable payloads: a non-200 makes MAX redeliver the same update forever."""
        if request.url.path != "/webhook":
            # Rejecting unknown request fields only helps if the answer says which one, and the
            # errors array is the shape /openapi.json declares for 422.
            logger.info("Validation failed for %s: %s", request.url.path, str(exc.errors())[:500])
            return JSONResponse(status_code=422, content={"detail": jsonable_encoder(exc.errors())})
        logger.warning("Ignoring malformed webhook payload: %s", str(exc.errors())[:300])
        return JSONResponse(status_code=200, content={"status": "ignored"})

    @app.exception_handler(StarletteHTTPException)
    async def unparsable_delivery(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        """FastAPI raises HTTP 400 when the body is not even decodable JSON, which happens before
        validation — /webhook must still answer 200 or MAX retries that delivery for hours."""
        if request.url.path == "/webhook" and exc.status_code == 400:
            logger.warning("Ignoring webhook body FastAPI could not parse: %s", exc.detail)
            return JSONResponse(status_code=200, content={"status": "ignored"})
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail},
            headers=getattr(exc, "headers", None),
        )

    @app.get("/health")
    async def health(request: Request) -> dict[str, object]:
        database_ok = await ping_db()
        users = 0
        if database_ok:
            async with session_scope() as session:
                users = await count_users(session)
        return {
            "status": "ok" if database_ok else "degraded",
            "database": "up" if database_ok else "down",
            "users": users,
            "bot": request.app.state.settings.bot_username,
            "mode": "webhook",
        }

    @app.get("/")
    async def root() -> dict[str, str]:
        return {
            "service": "MAX Messenger Bot MVP",
            "webhook": "POST /webhook",
            "docs": "/docs",
        }

    return app


app = create_app()

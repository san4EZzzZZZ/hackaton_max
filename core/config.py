"""Application configuration validated from the environment."""

from __future__ import annotations

import logging
import sys
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

# Hosts that only ever appear as placeholders in `.env.example`; a webhook pointing at one of
# them would silently register a dead delivery target with MAX.
PLACEHOLDER_HOSTS = ("your-domain", "yourdomain", "example.com", "example.org", "localhost", "127.0.0.1")

_LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"


class Settings(BaseSettings):
    """Every value is overridable through environment variables or `.env`."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # MAX bot credentials
    bot_token: str = ""
    bot_api_url: str = "https://platform-api2.max.ru"
    bot_user_id: int = 0
    bot_username: str = ""
    bot_name: str = ""
    bot_webhook_url: str = ""
    webhook_secret: str = ""
    mini_app_url: str = ""

    # HTTP server
    server_host: str = "0.0.0.0"
    server_port: int = 8080
    public_base_url: str = ""

    # Storage
    database_url: str = "sqlite+aiosqlite:///data/bot.db"

    # Extra trust anchors loaded on top of the system/certifi roots: MAX serves a chain rooted
    # in the Russian Ministry of Digital Development (НУЦ Госуслуг) CA, which no base image ships.
    ssl_ca_bundle: str = "certs/russian-trusted-root-ca.pem"
    ssl_verify: bool = True

    # Outbound behaviour
    log_level: LogLevel = "INFO"
    http_timeout: float = 30.0
    http_max_retries: int = 4
    rate_limit_per_chat: float = 0.55
    polling_timeout: int = 30
    polling_limit: int = 100
    auto_setup: bool = True

    @field_validator("bot_user_id", mode="before")
    @classmethod
    def _blank_user_id(cls, value: object) -> object:
        """.env.example ships `BOT_USER_ID=` empty on purpose; an empty string is not an integer."""
        return 0 if value is None or (isinstance(value, str) and not value.strip()) else value

    @field_validator("bot_token")
    @classmethod
    def _check_token(cls, value: str) -> str:
        token = value.strip()
        if len(token) < 16:
            raise ValueError("BOT_TOKEN is missing or too short — put the real token into .env")
        return token

    @field_validator("bot_api_url", "bot_webhook_url", "mini_app_url", "public_base_url")
    @classmethod
    def _check_url(cls, value: str) -> str:
        url = value.strip().rstrip("/")
        if url and not url.startswith(("http://", "https://")):
            raise ValueError(f"URL must be absolute: {url!r}")
        return url

    @field_validator("polling_timeout")
    @classmethod
    def _check_polling_timeout(cls, value: int) -> int:
        if not 0 <= value <= 90:
            raise ValueError("POLLING_TIMEOUT must be within 0..90 seconds (MAX API limit)")
        return value

    @field_validator("polling_limit")
    @classmethod
    def _check_polling_limit(cls, value: int) -> int:
        if not 1 <= value <= 1000:
            raise ValueError("POLLING_LIMIT must be within 1..1000 (MAX API limit)")
        return value

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.split("+", 1)[0].split("://", 1)[0] == "sqlite"

    @property
    def sqlalchemy_url(self) -> str:
        """Async SQLAlchemy URL. Switching to PostgreSQL only needs `DATABASE_URL`."""
        scheme, separator, remainder = self.database_url.partition("://")
        if not separator:
            raise ValueError(f"DATABASE_URL is malformed: {self.database_url!r}")
        base = scheme.split("+", 1)[0]
        if base == "sqlite":
            return f"sqlite+aiosqlite://{remainder}"
        if base in {"postgres", "postgresql"}:
            return f"postgresql+asyncpg://{remainder}"
        raise ValueError(f"DATABASE_URL scheme {base!r} has no async driver supported here")

    @property
    def ca_bundle_path(self) -> Path | None:
        if not self.ssl_ca_bundle.strip():
            return None
        path = Path(self.ssl_ca_bundle)
        return path if path.is_file() else None

    @staticmethod
    def _usable_url(value: str) -> str:
        """Template values copied from `.env.example` must never pass for a real endpoint."""
        candidate = value.strip().rstrip("/")
        if not candidate:
            return ""
        if any(marker in candidate.lower() for marker in PLACEHOLDER_HOSTS):
            return ""
        return candidate

    @property
    def resolved_webhook_url(self) -> str:
        if direct := self._usable_url(self.bot_webhook_url):
            return direct
        if base := self._usable_url(self.public_base_url):
            return f"{base}/webhook"
        return ""

    @property
    def webhook_subscribable(self) -> bool:
        """Refuse to register a delivery target we cannot authenticate or that MAX will reject."""
        url = self.resolved_webhook_url
        return bool(url and url.startswith("https://") and self.webhook_secret)

    @property
    def webhook_setup_hint(self) -> str:
        """One-line explanation of why automatic webhook registration is off."""
        url = self.resolved_webhook_url
        if not url:
            return (
                "no public HTTPS endpoint configured — set PUBLIC_BASE_URL (or BOT_WEBHOOK_URL) "
                "and WEBHOOK_SECRET, then run: python main.py --mode=setup-webhook"
            )
        if not url.startswith("https://"):
            return f"{url} is not HTTPS — MAX rejects plain HTTP webhooks"
        return f"BOT_WEBHOOK_URL={url} is set but WEBHOOK_SECRET is empty"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def configure_logging(level: str | None = None) -> None:
    logging.basicConfig(
        level=(level or get_settings().log_level).upper(),
        format=_LOG_FORMAT,
        stream=sys.stdout,
        force=True,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)


settings = get_settings()

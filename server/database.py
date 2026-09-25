"""Async SQLAlchemy layer: engine, sessions, PRAGMAs and persisted models."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import (
    BigInteger,
    DateTime,
    Float,
    Integer,
    MetaData,
    String,
    Text,
    case,
    event,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from core.config import settings

logger = logging.getLogger(__name__)

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class User(Base):
    """One row per MAX account that ever talked to the bot."""

    __tablename__ = "users"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    username: Mapped[str | None] = mapped_column(String(64))
    first_name: Mapped[str] = mapped_column(String(128), default="")
    last_name: Mapped[str | None] = mapped_column(String(128))
    locale: Mapped[str | None] = mapped_column(String(16))
    last_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_active: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"<User {self.user_id} @{self.username or self.first_name}>"


class SavedRoute(Base):
    """A generated route the visitor chose to keep; `payload` is the API answer, stored verbatim."""

    __tablename__ = "saved_routes"

    # SQLite aliases rowid only to a column declared exactly INTEGER, so a plain BigInteger primary key
    # would not autoincrement there; PostgreSQL keeps BIGINT.
    route_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True
    )
    # Client-supplied and unverified — see the docstring of server/routers/saved_routes.py.
    user_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    city: Mapped[str] = mapped_column(String(128))
    title: Mapped[str] = mapped_column(String(256))
    # SQLite has no JSON type, so the route travels as text and is parsed on the way out.
    payload: Mapped[str] = mapped_column(Text)
    total_cost: Mapped[float] = mapped_column(Float)
    stop_count: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"<SavedRoute {self.route_id} {self.city} ({self.stop_count} stops)>"


engine: AsyncEngine = create_async_engine(
    settings.sqlalchemy_url,
    echo=False,
    pool_pre_ping=True,
)

SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

if engine.dialect.name == "sqlite":

    @event.listens_for(engine.sync_engine, "connect")
    def _apply_sqlite_pragmas(dbapi_connection, _connection_record) -> None:
        """WAL lets concurrent webhook workers read and write without blocking each other."""
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=5000")
        finally:
            cursor.close()


def _dialect_insert(model):
    if engine.dialect.name == "sqlite":
        return sqlite_insert(model)
    if engine.dialect.name == "postgresql":
        return postgresql_insert(model)
    raise RuntimeError(f"No upsert statement configured for dialect {engine.dialect.name!r}")


async def init_db() -> None:
    """Create tables on startup; safe to call repeatedly."""
    if settings.is_sqlite:
        database = make_url(settings.sqlalchemy_url).database
        if database and database != ":memory:":
            Path(database).parent.mkdir(parents=True, exist_ok=True)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    logger.info("Database ready: %s", _safe_url())


async def dispose_db() -> None:
    await engine.dispose()


async def ping_db() -> bool:
    try:
        async with engine.connect() as connection:
            await connection.execute(select(1))
        return True
    except Exception:
        logger.warning("Database health check failed", exc_info=True)
        return False


def _safe_url() -> str:
    """Connection string without credentials, for logs."""
    url = settings.sqlalchemy_url
    return url if settings.is_sqlite else url.split("@")[-1]


class session_scope:
    """Async context manager committing on success and rolling back on failure."""

    __slots__ = ("_session",)

    def __init__(self) -> None:
        self._session: AsyncSession | None = None

    async def __aenter__(self) -> AsyncSession:
        self._session = SessionLocal()
        return self._session

    async def __aexit__(self, exc_type, exc, tb) -> None:
        assert self._session is not None
        try:
            if exc_type is None:
                await self._session.commit()
            else:
                await self._session.rollback()
        finally:
            await self._session.close()


async def upsert_user(
    session: AsyncSession,
    *,
    user_id: int,
    username: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
    locale: str | None = None,
    chat_id: int | None = None,
) -> User:
    """Insert on first sighting, then refresh profile and activity on every event."""
    now = _utcnow()
    values = {
        "user_id": user_id,
        "username": username,
        "first_name": first_name or "",
        "last_name": last_name,
        "locale": locale,
        "last_chat_id": chat_id,
        "created_at": now,
        "last_active": now,
    }
    insert_statement = _dialect_insert(User).values(**values)
    excluded = insert_statement.excluded
    statement = insert_statement.on_conflict_do_update(
        index_elements=[User.user_id],
        set_={
            "last_active": now,
            "username": func.coalesce(excluded.username, User.username),
            "first_name": case(
                (excluded.first_name == "", User.first_name), else_=excluded.first_name
            ),
            "last_name": func.coalesce(excluded.last_name, User.last_name),
            "locale": func.coalesce(excluded.locale, User.locale),
            "last_chat_id": func.coalesce(excluded.last_chat_id, User.last_chat_id),
        },
    )
    await session.execute(statement)
    await session.flush()
    stored = await session.get(User, user_id)
    if stored is None:  # pragma: no cover - only reachable on a racing delete
        raise RuntimeError(f"User {user_id} disappeared right after upsert")
    return stored


async def count_users(session: AsyncSession) -> int:
    result = await session.execute(select(func.count()).select_from(User))
    return int(result.scalar_one())


async def save_route(
    session: AsyncSession,
    *,
    city: str,
    title: str,
    payload: str,
    total_cost: float,
    stop_count: int,
    user_id: int | None = None,
) -> SavedRoute:
    """Append one route; the id and the timestamp come back from the database."""
    row = SavedRoute(
        city=city,
        title=title,
        payload=payload,
        total_cost=total_cost,
        stop_count=stop_count,
        user_id=user_id,
    )
    session.add(row)
    await session.flush()
    return row


async def get_saved_route(session: AsyncSession, route_id: int) -> SavedRoute | None:
    return await session.get(SavedRoute, route_id)


async def list_saved_routes(
    session: AsyncSession,
    *,
    user_id: int | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> tuple[list[SavedRoute], int]:
    """Newest first, and the count is of the whole selection — before `limit` cuts it."""
    conditions = [] if user_id is None else [SavedRoute.user_id == user_id]
    total = (
        await session.execute(select(func.count()).select_from(SavedRoute).where(*conditions))
    ).scalar_one()
    statement = select(SavedRoute).where(*conditions).order_by(SavedRoute.route_id.desc())
    statement = statement.offset(offset)
    if limit is not None:
        statement = statement.limit(limit)
    rows = (await session.execute(statement)).scalars().all()
    return list(rows), int(total)

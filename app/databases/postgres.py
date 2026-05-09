"""PostgreSQL database provider."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote_plus

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from .base import DatabaseProvider


class PostgresDatabaseProvider(DatabaseProvider):
    """PostgreSQL provider built on asyncpg (async) and psycopg v3 (sync).

    Connection params come from :mod:`app.config.bootstrap` — they live in
    ``bootstrap.toml`` (or the matching ``OPENPA_POSTGRES_*`` env vars), not
    in the regular Dynaconf settings, because we have to know them before any
    DB connection can succeed.
    """

    name = "postgres"

    def __init__(
        self,
        host: str,
        port: int,
        database: str,
        user: str,
        password: str,
        sslmode: str = "prefer",
    ):
        self.host = host
        self.port = int(port)
        self.database = database
        self.user = user
        self.password = password
        self.sslmode = (sslmode or "prefer").lower()
        self._async_engine: AsyncEngine | None = None
        self._sync_engine: Engine | None = None

    # ── URL builders ──────────────────────────────────────────────────────
    # Password is URL-encoded so unusual characters (``@``, ``/``, ``:``)
    # don't break the DSN. asyncpg uses the ``ssl`` query param while
    # psycopg uses ``sslmode``; map ours into the right name per driver.

    def _async_url(self) -> str:
        ssl = self._asyncpg_ssl_param()
        suffix = f"?ssl={ssl}" if ssl else ""
        return (
            f"postgresql+asyncpg://{quote_plus(self.user)}:{quote_plus(self.password)}"
            f"@{self.host}:{self.port}/{self.database}{suffix}"
        )

    def _sync_url(self) -> str:
        return (
            f"postgresql+psycopg://{quote_plus(self.user)}:{quote_plus(self.password)}"
            f"@{self.host}:{self.port}/{self.database}?sslmode={self.sslmode}"
        )

    def _asyncpg_ssl_param(self) -> str:
        # asyncpg accepts: disable, allow, prefer, require, verify-ca, verify-full.
        # Pass the user's choice through verbatim — it speaks the same vocabulary.
        return self.sslmode if self.sslmode and self.sslmode != "disable" else ""

    # ── Engines ───────────────────────────────────────────────────────────

    def async_engine(self) -> AsyncEngine:
        if self._async_engine is None:
            # NullPool: open/close per session inside the calling task. The
            # default pooled engine produces noisy CancelledError tracebacks
            # on client disconnect — SQLAlchemy's pool tries to terminate a
            # pooled connection under uvicorn's request cancel scope, and
            # asyncpg's graceful-close coroutine is awaited via the greenlet
            # bridge which propagates the cancellation. The teardown still
            # succeeds (asyncpg.terminate is forceful) but the traceback is
            # logged as a warning every time a streaming client closes its
            # tab. Per-session connections sidestep the race entirely. The
            # SQLite provider uses the same NullPool for identical reasons.
            self._async_engine = create_async_engine(
                self._async_url(),
                echo=False,
                poolclass=NullPool,
            )
        return self._async_engine

    def sync_engine(self) -> Engine:
        if self._sync_engine is None:
            self._sync_engine = create_engine(
                self._sync_url(),
                echo=False,
                pool_pre_ping=True,
            )
        return self._sync_engine

    async def apply_pragmas(self, conn: AsyncConnection) -> None:
        # Postgres doesn't need WAL or FK pragmas — both are first-class
        # behavior of the engine itself.
        return None

    def add_column_if_not_exists(self, table: str, column_def: str) -> str:
        return f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column_def}"

    def supports_add_column_if_not_exists(self) -> bool:
        return True

    def alter_column_type(self, table: str, column: str, new_type: str) -> str | None:
        return f"ALTER TABLE {table} ALTER COLUMN {column} TYPE {new_type}"

    def upsert(self, table_name: str, values: dict[str, Any], conflict_keys: list[str], update_fields: list[str]) -> Any:
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        from sqlalchemy import Table, MetaData
        table = Table(table_name, MetaData(), autoload_with=self.sync_engine())
        stmt = pg_insert(table).values(**values)
        update_dict = {field: getattr(stmt.excluded, field) for field in update_fields}
        return stmt.on_conflict_do_update(index_elements=conflict_keys, set_=update_dict)

    async def health_check(self) -> None:
        async with self.async_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))

    async def dispose(self) -> None:
        if self._async_engine is not None:
            await self._async_engine.dispose()
            self._async_engine = None
        if self._sync_engine is not None:
            self._sync_engine.dispose()
            self._sync_engine = None

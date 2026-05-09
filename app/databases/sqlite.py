"""SQLite database provider."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from .base import DatabaseProvider


class SqliteDatabaseProvider(DatabaseProvider):
    """SQLite + aiosqlite provider.

    NullPool on the async engine: open/close per session inside the calling
    task. Avoids the aiosqlite teardown race where the pool terminates a
    pooled connection under a foreign cancel scope (uvicorn request scope),
    which produced noisy CancelledError tracebacks on client disconnect.
    """

    name = "sqlite"

    def __init__(self, db_path: str):
        self.db_path = db_path
        # Deliberately do NOT mkdir here. The directory is only created when
        # an engine is actually built — this keeps the disk untouched while
        # the boot sequence is still constructing storage objects, in case a
        # later hot-swap discards this provider before any DB I/O happened.
        self._async_engine: AsyncEngine | None = None
        self._sync_engine: Engine | None = None

    def _ensure_parent_dir(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

    def async_engine(self) -> AsyncEngine:
        if self._async_engine is None:
            self._ensure_parent_dir()
            self._async_engine = create_async_engine(
                f"sqlite+aiosqlite:///{self.db_path}",
                echo=False,
                poolclass=NullPool,
            )
        return self._async_engine

    def sync_engine(self) -> Engine:
        if self._sync_engine is None:
            self._ensure_parent_dir()
            self._sync_engine = create_engine(
                f"sqlite:///{self.db_path}",
                echo=False,
                connect_args={"check_same_thread": False},
            )

            @event.listens_for(self._sync_engine, "connect")
            def _set_sqlite_pragma(dbapi_conn, _record):
                cursor = dbapi_conn.cursor()
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.close()

        return self._sync_engine

    async def apply_pragmas(self, conn: AsyncConnection) -> None:
        await conn.execute(text("PRAGMA journal_mode=WAL"))
        await conn.execute(text("PRAGMA foreign_keys=ON"))

        # Enforce foreign keys on every new connection to the underlying engine.
        sync_engine = conn.sync_connection.engine if conn.sync_connection else None
        if sync_engine is not None:
            @event.listens_for(sync_engine, "connect")
            def _enforce_fks(dbapi_conn, _record):
                cursor = dbapi_conn.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.close()

    def add_column_if_not_exists(self, table: str, column_def: str) -> str:
        # SQLite doesn't support IF NOT EXISTS on ADD COLUMN. Caller wraps
        # this in try/except (see supports_add_column_if_not_exists).
        return f"ALTER TABLE {table} ADD COLUMN {column_def}"

    def supports_add_column_if_not_exists(self) -> bool:
        return False

    def alter_column_type(self, table: str, column: str, new_type: str) -> str | None:
        # SQLite treats VARCHAR length as advisory and has no in-place
        # ALTER COLUMN TYPE. Returning None tells the caller to skip.
        return None

    def upsert(self, table_name: str, values: dict[str, Any], conflict_keys: list[str], update_fields: list[str]) -> Any:
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert
        from sqlalchemy import Table, MetaData
        # Use a lightweight Table reference — caller has already ensured the
        # table exists via the schema migration in ConversationStorage.
        table = Table(table_name, MetaData(), autoload_with=self.sync_engine())
        stmt = sqlite_insert(table).values(**values)
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

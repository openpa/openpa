"""Synchronous SQLite storage for global server config and per-profile LLM config.

After the tool-management refactor this module no longer owns the
``tool_configs`` table. Tool-related data (registry rows, profile↔tool join,
scoped per-tool configs) lives in :mod:`app.storage.tool_storage`.

All schema creation is owned by
:class:`app.storage.conversation_storage.ConversationStorage`. This class
never issues ``CREATE TABLE`` statements -- it only reads/writes existing
tables.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from app.utils.logger import logger


class DynamicConfigStorage:
    """Synchronous SQLite-backed dynamic config storage.

    Operates on two tables: ``server_config`` (global) and ``llm_config``
    (profile-scoped).
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = sqlite3.Row
        return conn

    # ── Generic key-value operations for server_config and llm_config ──

    def get(self, table: str, key: str, profile: str = "admin") -> str | None:
        if table not in ("server_config", "llm_config"):
            return None
        conn = self._get_conn()
        try:
            if table == "server_config":
                row = conn.execute(
                    "SELECT value FROM server_config WHERE key = ?", (key,)
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT value FROM llm_config WHERE profile = ? AND key = ?",
                    (profile, key),
                ).fetchone()
            return row["value"] if row else None
        finally:
            conn.close()

    def set(self, table: str, key: str, value: str, is_secret: bool = False, profile: str = "admin"):
        if table not in ("server_config", "llm_config"):
            raise ValueError(f"Invalid table: {table}")
        now = time.time() * 1000
        conn = self._get_conn()
        try:
            if table == "server_config":
                conn.execute(
                    """INSERT INTO server_config (key, value, is_secret, updated_at)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(key) DO UPDATE SET
                            value = excluded.value,
                            is_secret = excluded.is_secret,
                            updated_at = excluded.updated_at""",
                    (key, value, int(is_secret), now),
                )
            else:
                conn.execute(
                    """INSERT INTO llm_config (profile, key, value, is_secret, updated_at)
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(profile, key) DO UPDATE SET
                            value = excluded.value,
                            is_secret = excluded.is_secret,
                            updated_at = excluded.updated_at""",
                    (profile, key, value, int(is_secret), now),
                )
            conn.commit()
        finally:
            conn.close()

    def delete(self, table: str, key: str, profile: str = "admin") -> bool:
        if table not in ("server_config", "llm_config"):
            return False
        conn = self._get_conn()
        try:
            if table == "server_config":
                result = conn.execute("DELETE FROM server_config WHERE key = ?", (key,))
            else:
                result = conn.execute(
                    "DELETE FROM llm_config WHERE profile = ? AND key = ?",
                    (profile, key),
                )
            conn.commit()
            return result.rowcount > 0
        finally:
            conn.close()

    def get_all(self, table: str, include_secrets: bool = False, profile: str = "admin") -> dict[str, str]:
        if table not in ("server_config", "llm_config"):
            return {}
        conn = self._get_conn()
        try:
            if table == "server_config":
                rows = conn.execute("SELECT key, value, is_secret FROM server_config").fetchall()
            else:
                rows = conn.execute(
                    "SELECT key, value, is_secret FROM llm_config WHERE profile = ?",
                    (profile,),
                ).fetchall()
            result = {}
            for row in rows:
                if row["is_secret"] and not include_secrets:
                    result[row["key"]] = "***"
                else:
                    result[row["key"]] = row["value"]
            return result
        finally:
            conn.close()

    # ── Setup status ──

    def is_setup_complete(self) -> bool:
        return self.get("server_config", "setup_complete") == "true"

    def mark_setup_complete(self):
        self.set("server_config", "setup_complete", "true")

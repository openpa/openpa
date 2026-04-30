"""Synchronous SQLite storage for global server config and per-profile config.

Manages three tables:

- ``server_config``  Global server settings (key/value, optional secret flag).
- ``llm_config``     Per-profile LLM credentials and model assignments
                     (profile/key/value, optional secret flag).
- ``user_config``    Per-profile general application configuration
                     (profile/key/value, no secret flag) — drives the
                     Settings → Config UI.

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


_PROFILE_SCOPED = ("llm_config", "user_config")
_SECRET_AWARE = ("server_config", "llm_config")
_VALID_TABLES = ("server_config", "llm_config", "user_config")


class DynamicConfigStorage:
    """Synchronous SQLite-backed dynamic config storage.

    Operates on three tables: ``server_config`` (global, secret-aware),
    ``llm_config`` (profile-scoped, secret-aware), and ``user_config``
    (profile-scoped, no secret flag).
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

    # ── Generic key-value operations ──

    def get(self, table: str, key: str, profile: str = "admin") -> str | None:
        if table not in _VALID_TABLES:
            return None
        conn = self._get_conn()
        try:
            if table == "server_config":
                row = conn.execute(
                    "SELECT value FROM server_config WHERE key = ?", (key,)
                ).fetchone()
            else:
                row = conn.execute(
                    f"SELECT value FROM {table} WHERE profile = ? AND key = ?",
                    (profile, key),
                ).fetchone()
            return row["value"] if row else None
        finally:
            conn.close()

    def set(self, table: str, key: str, value: str, is_secret: bool = False, profile: str = "admin"):
        if table not in _VALID_TABLES:
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
            elif table == "llm_config":
                conn.execute(
                    """INSERT INTO llm_config (profile, key, value, is_secret, updated_at)
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(profile, key) DO UPDATE SET
                            value = excluded.value,
                            is_secret = excluded.is_secret,
                            updated_at = excluded.updated_at""",
                    (profile, key, value, int(is_secret), now),
                )
            else:  # user_config — no is_secret column
                conn.execute(
                    """INSERT INTO user_config (profile, key, value, updated_at)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(profile, key) DO UPDATE SET
                            value = excluded.value,
                            updated_at = excluded.updated_at""",
                    (profile, key, value, now),
                )
            conn.commit()
        finally:
            conn.close()

    def delete(self, table: str, key: str, profile: str = "admin") -> bool:
        if table not in _VALID_TABLES:
            return False
        conn = self._get_conn()
        try:
            if table == "server_config":
                result = conn.execute("DELETE FROM server_config WHERE key = ?", (key,))
            else:
                result = conn.execute(
                    f"DELETE FROM {table} WHERE profile = ? AND key = ?",
                    (profile, key),
                )
            conn.commit()
            return result.rowcount > 0
        finally:
            conn.close()

    def delete_by_prefix(self, table: str, prefix: str, profile: str = "admin") -> int:
        """Delete all keys matching a prefix. Returns the number of rows deleted."""
        if table not in _VALID_TABLES:
            return 0
        conn = self._get_conn()
        try:
            if table == "server_config":
                result = conn.execute(
                    "DELETE FROM server_config WHERE key LIKE ?",
                    (prefix + "%",),
                )
            else:
                result = conn.execute(
                    f"DELETE FROM {table} WHERE profile = ? AND key LIKE ?",
                    (profile, prefix + "%"),
                )
            conn.commit()
            return result.rowcount
        finally:
            conn.close()

    def get_all(self, table: str, include_secrets: bool = False, profile: str = "admin") -> dict[str, str]:
        if table not in _VALID_TABLES:
            return {}
        conn = self._get_conn()
        try:
            if table == "server_config":
                rows = conn.execute("SELECT key, value, is_secret FROM server_config").fetchall()
                secret_aware = True
            elif table == "llm_config":
                rows = conn.execute(
                    "SELECT key, value, is_secret FROM llm_config WHERE profile = ?",
                    (profile,),
                ).fetchall()
                secret_aware = True
            else:  # user_config
                rows = conn.execute(
                    "SELECT key, value FROM user_config WHERE profile = ?",
                    (profile,),
                ).fetchall()
                secret_aware = False
            result = {}
            for row in rows:
                if secret_aware and row["is_secret"] and not include_secrets:
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

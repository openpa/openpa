"""Synchronous SQLite storage for dynamic configuration.

Provides get/set/delete operations for server_config, llm_config, and tool_configs tables.
Used by BaseConfig and ToolConfigManager to resolve the SQLite layer of the priority chain.

llm_config and tool_configs are scoped per profile (FK to profiles.name with CASCADE delete).
server_config remains global (no profile column).
"""

import sqlite3
import time
import uuid
from pathlib import Path

from app.utils.logger import logger


class DynamicConfigStorage:
    """Synchronous SQLite-backed dynamic config storage.

    Operates on three tables: server_config, llm_config, tool_configs.
    llm_config and tool_configs are profile-scoped; server_config is global.
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._ensure_tables()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_tables(self):
        """Create config tables if they don't exist."""
        conn = self._get_conn()
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS server_config (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    is_secret INTEGER DEFAULT 0,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS llm_config (
                    profile TEXT NOT NULL,
                    key TEXT NOT NULL,
                    value TEXT NOT NULL,
                    is_secret INTEGER DEFAULT 0,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (profile, key),
                    FOREIGN KEY (profile) REFERENCES profiles(name) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS tool_configs (
                    id TEXT PRIMARY KEY,
                    profile TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    config_key TEXT NOT NULL,
                    config_value TEXT NOT NULL,
                    is_secret INTEGER DEFAULT 0,
                    updated_at REAL NOT NULL,
                    FOREIGN KEY (profile) REFERENCES profiles(name) ON DELETE CASCADE
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_tool_configs_profile_name_key
                    ON tool_configs(profile, tool_name, config_key);
            """)
            conn.commit()
        finally:
            conn.close()

    # ── Generic key-value operations for server_config and llm_config ──

    def get(self, table: str, key: str, profile: str = "admin") -> str | None:
        """Get a value from server_config or llm_config by key.

        For llm_config, ``profile`` scopes the lookup. Ignored for server_config.
        """
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
        """Set a value in server_config or llm_config (upsert).

        For llm_config, ``profile`` scopes the row. Ignored for server_config.
        """
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
        """Delete a key from server_config or llm_config.

        For llm_config, ``profile`` scopes the delete. Ignored for server_config.
        """
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
        """Get all key-value pairs from server_config or llm_config.

        For llm_config, ``profile`` scopes the query. Ignored for server_config.
        If include_secrets is False, secret values are masked as '***'.
        """
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

    # ── Tool config operations ──

    def get_tool_config(self, tool_name: str, config_key: str, profile: str = "admin") -> str | None:
        """Get a specific config value for a tool, scoped by profile."""
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT config_value FROM tool_configs WHERE profile = ? AND tool_name = ? AND config_key = ?",
                (profile, tool_name, config_key),
            ).fetchone()
            return row["config_value"] if row else None
        finally:
            conn.close()

    def set_tool_config(self, tool_name: str, config_key: str, config_value: str,
                        is_secret: bool = False, profile: str = "admin"):
        """Set a tool config value (upsert), scoped by profile."""
        now = time.time() * 1000
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT id FROM tool_configs WHERE profile = ? AND tool_name = ? AND config_key = ?",
                (profile, tool_name, config_key),
            ).fetchone()
            if row:
                conn.execute(
                    """UPDATE tool_configs SET config_value = ?, is_secret = ?, updated_at = ?
                       WHERE profile = ? AND tool_name = ? AND config_key = ?""",
                    (config_value, int(is_secret), now, profile, tool_name, config_key),
                )
            else:
                conn.execute(
                    """INSERT INTO tool_configs (id, profile, tool_name, config_key, config_value, is_secret, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (str(uuid.uuid4()), profile, tool_name, config_key, config_value, int(is_secret), now),
                )
            conn.commit()
        finally:
            conn.close()

    def delete_tool_config(self, tool_name: str, config_key: str, profile: str = "admin") -> bool:
        """Delete a specific tool config entry, scoped by profile."""
        conn = self._get_conn()
        try:
            result = conn.execute(
                "DELETE FROM tool_configs WHERE profile = ? AND tool_name = ? AND config_key = ?",
                (profile, tool_name, config_key),
            )
            conn.commit()
            return result.rowcount > 0
        finally:
            conn.close()

    def get_all_tool_configs(self, tool_name: str, include_secrets: bool = False,
                             profile: str = "admin") -> dict[str, str]:
        """Get all config values for a tool, scoped by profile.

        If include_secrets is False, secret values are masked as '***'.
        """
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT config_key, config_value, is_secret FROM tool_configs WHERE profile = ? AND tool_name = ?",
                (profile, tool_name),
            ).fetchall()
            result = {}
            for row in rows:
                if row["is_secret"] and not include_secrets:
                    result[row["config_key"]] = "***"
                else:
                    result[row["config_key"]] = row["config_value"]
            return result
        finally:
            conn.close()

    def get_all_tools_configs(self, include_secrets: bool = False,
                              profile: str = "admin") -> dict[str, dict[str, str]]:
        """Get all config values for all tools, grouped by tool name, scoped by profile."""
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT tool_name, config_key, config_value, is_secret FROM tool_configs WHERE profile = ?",
                (profile,),
            ).fetchall()
            result: dict[str, dict[str, str]] = {}
            for row in rows:
                tool = row["tool_name"]
                if tool not in result:
                    result[tool] = {}
                if row["is_secret"] and not include_secrets:
                    result[tool][row["config_key"]] = "***"
                else:
                    result[tool][row["config_key"]] = row["config_value"]
            return result
        finally:
            conn.close()

    # ── Setup status ──

    def is_setup_complete(self) -> bool:
        """Check if initial setup has been completed."""
        return self.get("server_config", "setup_complete") == "true"

    def mark_setup_complete(self):
        """Mark initial setup as complete."""
        self.set("server_config", "setup_complete", "true")

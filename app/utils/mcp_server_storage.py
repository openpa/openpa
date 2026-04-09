"""MCP server storage backend for managing MCP server configurations per profile.

Provides SQLite-backed storage for MCP server configs. Stores full config dicts
(url, llm_provider, llm_model, system_prompt, description) as JSON.
"""
import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional

from app.config.settings import BaseConfig
from app.utils.logger import logger


class SQLiteMCPServerStorage:
    """SQLite-backed MCP server storage, scoped by profile."""

    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or BaseConfig.SQLITE_DB_PATH
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._ensure_table()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _ensure_table(self):
        with self._get_conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS mcp_servers (
                    id TEXT PRIMARY KEY,
                    url TEXT NOT NULL,
                    profile TEXT NOT NULL REFERENCES profiles(name) ON DELETE CASCADE,
                    config_json TEXT,
                    created_at REAL NOT NULL
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_mcp_servers_profile
                ON mcp_servers (profile)
            """)

    def add_agent(self, url: str, config: Optional[dict] = None, *, profile: str) -> None:
        if self.exists(url, profile):
            return
        config_data = config if config else {"url": url}
        now = time.time() * 1000
        with self._get_conn() as conn:
            conn.execute(
                "INSERT INTO mcp_servers (id, url, profile, config_json, created_at) VALUES (?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), url, profile, json.dumps(config_data), now)
            )
        logger.info(f"MCP server config added (profile={profile}): {url}")

    def remove_agent(self, url: str, profile: str) -> bool:
        with self._get_conn() as conn:
            cursor = conn.execute(
                "DELETE FROM mcp_servers WHERE url = ? AND profile = ?",
                (url, profile)
            )
            if cursor.rowcount > 0:
                logger.info(f"MCP server config removed (profile={profile}): {url}")
                return True
            return False

    def get_all_agents(self, profile: str) -> list[dict]:
        """Return all MCP server configs as a list of dicts for a profile."""
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT url, config_json FROM mcp_servers WHERE profile = ?",
                (profile,)
            ).fetchall()
            configs = []
            for url, config_json in rows:
                if config_json:
                    try:
                        config = json.loads(config_json)
                        if isinstance(config, dict):
                            config.setdefault("url", url)
                            configs.append(config)
                        else:
                            configs.append({"url": url})
                    except json.JSONDecodeError:
                        configs.append({"url": url})
                else:
                    configs.append({"url": url})
            return configs

    def get_agent_config(self, url: str, profile: str) -> Optional[dict]:
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT config_json FROM mcp_servers WHERE url = ? AND profile = ?",
                (url, profile)
            ).fetchone()
            if row and row[0]:
                try:
                    config = json.loads(row[0])
                    if isinstance(config, dict):
                        config.setdefault("url", url)
                        return config
                except json.JSONDecodeError:
                    pass
            return None

    def update_agent_config(self, url: str, config: dict, profile: str) -> bool:
        config["url"] = url
        with self._get_conn() as conn:
            cursor = conn.execute(
                "UPDATE mcp_servers SET config_json = ? WHERE url = ? AND profile = ?",
                (json.dumps(config), url, profile)
            )
            if cursor.rowcount > 0:
                logger.info(f"MCP server config updated (profile={profile}): {url}")
                return True
            return False

    def exists(self, url: str, profile: str) -> bool:
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM mcp_servers WHERE url = ? AND profile = ?",
                (url, profile)
            ).fetchone()
            return row is not None

    def get_all_profiles_agents(self) -> Dict[str, List[dict]]:
        """Retrieve all MCP server configs grouped by profile."""
        result: Dict[str, List[dict]] = {}
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT profile, url, config_json FROM mcp_servers ORDER BY profile"
            ).fetchall()
            for profile, url, config_json in rows:
                if config_json:
                    try:
                        config = json.loads(config_json)
                        if isinstance(config, dict):
                            config.setdefault("url", url)
                        else:
                            config = {"url": url}
                    except json.JSONDecodeError:
                        config = {"url": url}
                else:
                    config = {"url": url}
                result.setdefault(profile, []).append(config)
        return result

    def remove_all_for_profile(self, profile: str) -> None:
        with self._get_conn() as conn:
            conn.execute(
                "DELETE FROM mcp_servers WHERE profile = ?",
                (profile,)
            )
        logger.info(f"Removed all MCP server data for profile '{profile}'")


_storage_instance = None


def get_mcp_server_storage():
    """Get the MCP server storage backend instance (singleton)."""
    global _storage_instance

    if _storage_instance is not None:
        return _storage_instance

    _storage_instance = SQLiteMCPServerStorage()
    logger.info("Using SQLite MCP server storage")
    return _storage_instance

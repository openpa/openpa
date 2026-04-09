"""Remote agent storage backend for managing agent URLs per profile.

Provides SQLite-backed storage for remote agent addresses with support
for add/remove/list operations, scoped by profile.
"""
import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Protocol, Optional, Dict, List

from app.config.settings import BaseConfig
from app.utils.logger import logger


class RemoteAgentStorageBackend(Protocol):
    """Protocol for remote agent storage backends."""

    def add_agent(self, url: str, profile: str) -> None:
        """Add a remote agent URL to storage for a specific profile."""
        ...

    def remove_agent(self, url: str, profile: str) -> bool:
        """Remove a remote agent URL from storage for a specific profile."""
        ...

    def get_all_agents(self, profile: str) -> list[str]:
        """Retrieve all remote agent URLs for a specific profile."""
        ...

    def exists(self, url: str, profile: str) -> bool:
        """Check if an agent URL exists in storage for a specific profile."""
        ...

    def get_all_profiles_agents(self) -> Dict[str, List[str]]:
        """Retrieve all agent URLs grouped by profile (for startup)."""
        ...

    def remove_all_for_profile(self, profile: str) -> None:
        """Remove all agent URLs for a specific profile."""
        ...


class SQLiteRemoteAgentStorage:
    """SQLite-backed remote agent storage, scoped by profile."""

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
                CREATE TABLE IF NOT EXISTS remote_agents (
                    id TEXT PRIMARY KEY,
                    url TEXT NOT NULL,
                    profile TEXT NOT NULL REFERENCES profiles(name) ON DELETE CASCADE,
                    created_at REAL NOT NULL
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_remote_agents_profile
                ON remote_agents (profile)
            """)

    def add_agent(self, url: str, profile: str) -> None:
        if self.exists(url, profile):
            logger.info(f"Agent URL already exists (profile={profile}): {url}")
            return
        now = time.time() * 1000
        with self._get_conn() as conn:
            conn.execute(
                "INSERT INTO remote_agents (id, url, profile, created_at) VALUES (?, ?, ?, ?)",
                (str(uuid.uuid4()), url, profile, now)
            )
        logger.info(f"Agent URL added (profile={profile}): {url}")

    def remove_agent(self, url: str, profile: str) -> bool:
        with self._get_conn() as conn:
            cursor = conn.execute(
                "DELETE FROM remote_agents WHERE url = ? AND profile = ?",
                (url, profile)
            )
            if cursor.rowcount > 0:
                logger.info(f"Agent URL removed (profile={profile}): {url}")
                return True
            return False

    def get_all_agents(self, profile: str) -> list[str]:
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT url FROM remote_agents WHERE profile = ?",
                (profile,)
            ).fetchall()
            return [row[0] for row in rows]

    def exists(self, url: str, profile: str) -> bool:
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM remote_agents WHERE url = ? AND profile = ?",
                (url, profile)
            ).fetchone()
            return row is not None

    def get_all_profiles_agents(self) -> Dict[str, List[str]]:
        result: Dict[str, List[str]] = {}
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT profile, url FROM remote_agents ORDER BY profile"
            ).fetchall()
            for profile, url in rows:
                result.setdefault(profile, []).append(url)
        return result

    def remove_all_for_profile(self, profile: str) -> None:
        with self._get_conn() as conn:
            conn.execute(
                "DELETE FROM remote_agents WHERE profile = ?",
                (profile,)
            )
        logger.info(f"Removed all remote agent data for profile '{profile}'")


# Singleton storage instance
_storage_instance: Optional[RemoteAgentStorageBackend] = None


def get_remote_agent_storage() -> RemoteAgentStorageBackend:
    """Get the remote agent storage backend instance (singleton)."""
    global _storage_instance

    if _storage_instance is not None:
        return _storage_instance

    _storage_instance = SQLiteRemoteAgentStorage()
    logger.info("Using SQLite remote agent storage")
    return _storage_instance

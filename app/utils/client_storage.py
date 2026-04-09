"""Auth client storage backend for session tokens.

Provides SQLite-backed storage for OAuth tokens with automatic table creation.

Storage table: auth_tokens
Columns: id, agent_name, profile, agent_type, token_kind, token, created_at
"""
import base64
import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Protocol, Optional

from app.config.settings import BaseConfig
from app.utils.logger import logger

# Default token kind
ACCESS_TOKEN = "access_token"
REFRESH_TOKEN = "refresh_token"
DCR_CLIENT_ID = "dcr_client_id"
DCR_CLIENT_SECRET = "dcr_client_secret"

# Special profile for server-level (profile-independent) data like DCR credentials
SERVER_PROFILE = "__server__"


def _sanitize_name(name: str) -> str:
    """Sanitize a name for use in storage keys."""
    return "".join([c for c in name if c.isalnum() or c in (' ', '-', '_')]).strip()


def _decode_jwt_exp(token: str) -> Optional[int]:
    """Decode JWT token and extract the 'exp' claim.

    Returns:
        The exp timestamp (seconds since epoch) or None if unable to decode
    """
    try:
        parts = token.split('.')
        if len(parts) != 3:
            return None

        payload = parts[1]
        padding = 4 - len(payload) % 4
        if padding != 4:
            payload += '=' * padding

        decoded_bytes = base64.urlsafe_b64decode(payload)
        payload_dict = json.loads(decoded_bytes)

        return payload_dict.get('exp')
    except Exception as e:
        logger.warning(f"Failed to decode JWT exp claim: {e}")
        return None


class ClientStorageBackend(Protocol):
    """Protocol for client storage backends."""

    def save_token(self, agent_name: str, profile: str, token: str,
                   agent_type: str = "a2a", token_kind: str = ACCESS_TOKEN) -> None:
        """Save a token for the given agent, profile, and kind."""
        ...

    def get_token(self, agent_name: str, profile: str,
                  agent_type: str = "a2a", token_kind: str = ACCESS_TOKEN) -> str:
        """Retrieve a token for the given agent, profile, and kind."""
        ...

    def delete_token(self, agent_name: str, profile: str,
                     agent_type: str = "a2a", token_kind: str = ACCESS_TOKEN) -> bool:
        """Delete a token for the given agent, profile, and kind."""
        ...


class SQLiteClientStorage:
    """SQLite-backed client storage for auth tokens."""

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
                CREATE TABLE IF NOT EXISTS auth_tokens (
                    id TEXT PRIMARY KEY,
                    agent_name TEXT NOT NULL,
                    profile TEXT NOT NULL REFERENCES profiles(name) ON DELETE CASCADE,
                    agent_type TEXT NOT NULL DEFAULT 'a2a',
                    token_kind TEXT NOT NULL DEFAULT 'access_token',
                    token TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_auth_tokens_lookup
                ON auth_tokens (agent_name, profile, agent_type, token_kind)
            """)
            # Ensure the __server__ pseudo-profile exists for server-level
            # data (e.g., DCR credentials) that isn't tied to a user profile.
            import time
            now = time.time() * 1000
            conn.execute(
                "INSERT OR IGNORE INTO profiles (id, name, created_at, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (SERVER_PROFILE, SERVER_PROFILE, now, now)
            )

    def save_token(self, agent_name: str, profile: str, token: str,
                   agent_type: str = "a2a", token_kind: str = ACCESS_TOKEN) -> None:
        safe_agent = _sanitize_name(agent_name)
        now = time.time() * 1000
        with self._get_conn() as conn:
            # Delete existing token for this combination, then insert
            conn.execute(
                "DELETE FROM auth_tokens WHERE agent_name = ? AND profile = ? AND agent_type = ? AND token_kind = ?",
                (safe_agent, profile, agent_type, token_kind)
            )
            conn.execute(
                "INSERT INTO auth_tokens (id, agent_name, profile, agent_type, token_kind, token, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), safe_agent, profile, agent_type, token_kind, token, now)
            )
        logger.info(f"Token saved ({token_kind}): agent={safe_agent}, profile={profile}")

    def get_token(self, agent_name: str, profile: str,
                  agent_type: str = "a2a", token_kind: str = ACCESS_TOKEN) -> str:
        safe_agent = _sanitize_name(agent_name)
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT token FROM auth_tokens WHERE agent_name = ? AND profile = ? AND agent_type = ? AND token_kind = ?",
                (safe_agent, profile, agent_type, token_kind)
            ).fetchone()
            return row[0] if row else ""

    def delete_token(self, agent_name: str, profile: str,
                     agent_type: str = "a2a", token_kind: str = ACCESS_TOKEN) -> bool:
        safe_agent = _sanitize_name(agent_name)
        with self._get_conn() as conn:
            cursor = conn.execute(
                "DELETE FROM auth_tokens WHERE agent_name = ? AND profile = ? AND agent_type = ? AND token_kind = ?",
                (safe_agent, profile, agent_type, token_kind)
            )
            if cursor.rowcount > 0:
                logger.info(f"Token deleted ({token_kind}): agent={safe_agent}, profile={profile}")
                return True
            return False


# Singleton storage instance
_storage_instance: Optional[ClientStorageBackend] = None


def get_auth_client_storage() -> ClientStorageBackend:
    """Get the auth client storage backend instance (singleton)."""
    global _storage_instance

    if _storage_instance is not None:
        return _storage_instance

    _storage_instance = SQLiteClientStorage()
    logger.info("Using SQLite client storage")
    return _storage_instance


# Backward-compatible alias
get_client_storage = get_auth_client_storage

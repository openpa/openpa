"""Application settings with priority chain: SQLite > TOML (Dynaconf) > .env fallback.

Server-level vars (HOST, PORT, APP_URL, ENV, DEBUG, LOG_LEVEL) remain in .env.
All other configuration reads from Dynaconf TOML defaults, overridable via SQLite dynamic config.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

from app.config import settings as dynaconf_settings

dotenv_path = os.path.join("app/../.env")
load_dotenv(dotenv_path)


def _bool(val) -> bool:
    if val is None:
        return False
    if isinstance(val, bool):
        return val
    return str(val).lower() == "true"


def _csv_list(val) -> list[str]:
    """Parse a comma-separated env var into a list of trimmed, non-empty strings."""
    if not val:
        return []
    return [item.strip() for item in val.split(",") if item.strip()]


def _dynaconf_get(key: str, default=None):
    """Get a value from Dynaconf settings, with a fallback default."""
    try:
        return dynaconf_settings.get(key, default)
    except Exception:
        return default


# Lazy reference to dynamic config storage — set at runtime after DB init.
_dynamic_config_storage = None


def set_dynamic_config_storage(storage):
    """Set the dynamic config storage (called once after DB initialization)."""
    global _dynamic_config_storage
    _dynamic_config_storage = storage


def get_dynamic(table: str, key: str, default=None, profile: str | None = None):
    """Get a value from SQLite dynamic config, falling back to Dynaconf, then to default.

    Args:
        table: One of 'server_config', 'llm_config', 'tool_configs'
        key: The config key to look up
        default: Fallback value if not found anywhere
        profile: Profile name (used for llm_config; ignored for server_config)
    """
    if _dynamic_config_storage is not None:
        kwargs = {}
        if profile is not None:
            kwargs["profile"] = profile
        val = _dynamic_config_storage.get(table, key, **kwargs)
        if val is not None:
            return val
    return default


DEFAULT_USER_WORKING_DIR = os.path.join(os.path.expanduser("~"), "Documents")


def get_user_working_directory() -> str:
    """Resolve the User Working Directory — the default active path injected
    into built-in tools as ``_working_directory`` and shown to the LLM as the
    ``Current User Working Directory``.

    Distinct from ``BaseConfig.OPENPA_WORKING_DIR``, which is reserved for
    OpenPA-internal paths (skills, PERSONA.md, exec_shell stdout).

    Resolution order:
      1. ``server_config.user_working_dir`` (set via setup wizard)
      2. ``~/Documents`` fallback

    The returned directory is created if it does not exist.
    """
    raw = get_dynamic("server_config", "user_working_dir", default=None)
    path = raw if raw else DEFAULT_USER_WORKING_DIR
    if path.startswith("~"):
        path = os.path.expanduser(path)
    path = os.path.normpath(path)
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        # Falls through; tools that actually need the dir will surface the error.
        pass
    return path


class BaseConfig:
    """Application configuration.

    Server-level settings come from .env.
    Application settings use the priority chain: SQLite > TOML > env fallback.
    """

    # ── Server-level (from .env only) ──
    HOST = os.environ.get("HOST", "0.0.0.0")
    PORT = int(os.environ.get("PORT", 10000))
    APP_URL = os.environ.get("APP_URL", f"http://{HOST}:{PORT}")
    ENV = os.environ.get("ENV", "production")
    DEBUG = _bool(os.environ.get("DEBUG", "false"))
    LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
    DISABLE_LOG = _bool(os.environ.get("DISABLE_LOG", "false"))
    CORS_ALLOWED_ORIGINS = _csv_list(os.environ.get("CORS_ALLOWED_ORIGINS", "")) or ["*"]

    # ── Application-level (TOML defaults, overridable via SQLite) ──
    SERVICE_NAME = _dynaconf_get("general.service_name", "openpa-agent")
    AGENT_ID = _dynaconf_get("general.agent_id", "openpa_agent")
    AGENT_NAME = _dynaconf_get("general.agent_name", "OPENPA Agent")
    JWT_EXPIRATION_HOURS = int(_dynaconf_get("general.jwt_expiration_hours", 720))
    SESSION_TOKEN_DEFAULT_TTL = int(_dynaconf_get("general.session_token_default_ttl", 3600))
    MCP_TOOL_CALL_TIMEOUT = int(_dynaconf_get("general.mcp_tool_call_timeout", 300))
    OPENPA_WORKING_DIR = _dynaconf_get("general.working_dir", os.path.join(os.path.expanduser("~"), ".openpa"))
    # Expand ~ and normalize path separators (avoids mixed \ and / on Windows)
    if OPENPA_WORKING_DIR.startswith("~"):
        OPENPA_WORKING_DIR = os.path.expanduser(OPENPA_WORKING_DIR)
    OPENPA_WORKING_DIR = os.path.normpath(OPENPA_WORKING_DIR)

    SQLITE_DB_PATH = os.path.join(
        OPENPA_WORKING_DIR, "storage",
        _dynaconf_get("general.sqlite_db_path", "openpa.db"),
    )

    # ── Embedding ──
    EMBEDDING_GRPC_HOST = _dynaconf_get("embedding.grpc_host", "localhost")
    EMBEDDING_GRPC_PORT = int(_dynaconf_get("embedding.grpc_port", 50051))

    # ── Qdrant ──
    QDRANT_HOST =  _dynaconf_get("qdrant.host", "localhost")
    QDRANT_PORT = int(_dynaconf_get("qdrant.port", 6333))
    QDRANT_API_KEY = _dynaconf_get("qdrant.api_key", "")
    QDRANT_HTTPS = _bool(_dynaconf_get("qdrant.https", "false"))

    # ── LLM (configured via setup wizard, stored in SQLite) ──
    DEFAULT_MODEL_NAME = _dynaconf_get("llm.model_groups.high", "")
    REASONING_MODEL_NAME = _dynaconf_get("llm.model_groups.low", "")

    # ── Profile ──
    PROFILE = _dynaconf_get("general.profile", "")
    AUTO_AUTH_MODE = _bool(os.environ.get("AUTO_AUTH_MODE", "true"))

    @classmethod
    def get_provider_api_key(cls, provider_name: str, profile: str | None = None) -> str:
        """Get API key for a provider from SQLite dynamic config."""
        # Try SQLite first
        dynamic_val = get_dynamic("llm_config", f"{provider_name}.api_key", profile=profile)
        if dynamic_val:
            return dynamic_val

        return ""

    @classmethod
    def get_model_group(cls, group: str, profile: str | None = None) -> str:
        """Get the model identifier for a model group ('high' or 'low').

        Priority: SQLite > TOML > class default.
        """
        dynamic_val = get_dynamic("llm_config", f"model_group.{group}", profile=profile)
        if dynamic_val:
            return dynamic_val
        return _dynaconf_get(f"llm.model_groups.{group}", "")

    @classmethod
    def get_default_provider(cls, profile: str | None = None) -> str:
        """Get the default LLM provider name."""
        dynamic_val = get_dynamic("llm_config", "default_provider", profile=profile)
        if dynamic_val:
            return dynamic_val
        return _dynaconf_get("llm.default_provider", "")

    @classmethod
    def get_jwt_secret(cls) -> str:
        """Get JWT secret from SQLite dynamic config, falling back to TOML."""
        dynamic_val = get_dynamic("server_config", "jwt_secret")
        if dynamic_val:
            return dynamic_val
        return _dynaconf_get("general.jwt_secret", "")

    @classmethod
    def get_server_config(cls, key: str, default=None):
        """Get a server config value from SQLite, falling back to class attribute or default."""
        dynamic_val = get_dynamic("server_config", key)
        if dynamic_val is not None:
            return dynamic_val
        attr = key.upper()
        if hasattr(cls, attr):
            return getattr(cls, attr)
        return default

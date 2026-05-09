"""Bootstrap config: the database provider choice and its connection details.

This file is the only durable home for the database-provider selection. It is
read **before** any database connection because we have to know which engine
to build before we can read anything from a DB.

Layout (TOML):

    db_provider = "sqlite"          # or "postgres"

    [postgres]
    host     = "localhost"
    port     = 5432
    database = "openpa"
    user     = "openpa"
    password = "..."
    sslmode  = "prefer"             # disable | allow | prefer | require | verify-ca | verify-full

The file lives at ``<OPENPA_WORKING_DIR>/bootstrap.toml`` (default
``~/.openpa/bootstrap.toml``) and is created by the setup wizard. If the file
is missing, the system defaults to SQLite, which is also what a fresh
installation gets.

Environment variables (``OPENPA_DB_PROVIDER``, ``OPENPA_POSTGRES_HOST`` etc.)
override anything written to the file — useful for container deployments and
test harnesses that want to point the same image at different databases
without mutating the working dir.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

import toml


_DEFAULT_PROVIDER = "sqlite"
_DEFAULT_POSTGRES_PORT = 5432
_DEFAULT_POSTGRES_SSLMODE = "prefer"


def _bootstrap_path() -> Path:
    """Resolve the bootstrap file path from the OpenPA working dir."""
    # Imported lazily so this module stays import-safe even if settings.py
    # itself wants to read the bootstrap during its own import.
    from app.config.settings import BaseConfig
    return Path(BaseConfig.OPENPA_WORKING_DIR) / "bootstrap.toml"


def read_bootstrap() -> dict[str, Any]:
    """Return the parsed bootstrap config, or sane defaults if the file is missing.

    Always returns a dict shaped like::

        {
            "db_provider": "sqlite" | "postgres",
            "postgres": {host, port, database, user, password, sslmode},
        }

    Missing keys are filled with defaults so callers can index without
    branching on presence.
    """
    path = _bootstrap_path()
    data: dict[str, Any] = {}
    if path.is_file():
        try:
            data = toml.loads(path.read_text(encoding="utf-8"))
        except Exception:
            # A malformed file is treated as missing — the wizard can rewrite
            # it. We deliberately avoid raising here so a corrupt bootstrap
            # never bricks the server's ability to come up on SQLite.
            data = {}

    provider = (data.get("db_provider") or _DEFAULT_PROVIDER).strip().lower()
    pg = data.get("postgres") or {}
    return {
        "db_provider": provider,
        "postgres": {
            "host": pg.get("host", "localhost"),
            "port": int(pg.get("port", _DEFAULT_POSTGRES_PORT)),
            "database": pg.get("database", "openpa"),
            "user": pg.get("user", "openpa"),
            "password": pg.get("password", ""),
            "sslmode": pg.get("sslmode", _DEFAULT_POSTGRES_SSLMODE),
        },
    }


def write_bootstrap(data: dict[str, Any]) -> None:
    """Atomically persist a new bootstrap config.

    Accepts the same shape as :func:`read_bootstrap`. Writes via a temp file +
    rename so a crashed write never leaves a half-formed bootstrap behind.
    """
    path = _bootstrap_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    payload: dict[str, Any] = {
        "db_provider": (data.get("db_provider") or _DEFAULT_PROVIDER).strip().lower(),
    }
    pg = data.get("postgres") or {}
    if payload["db_provider"] == "postgres" or pg:
        payload["postgres"] = {
            "host": pg.get("host", "localhost"),
            "port": int(pg.get("port", _DEFAULT_POSTGRES_PORT)),
            "database": pg.get("database", "openpa"),
            "user": pg.get("user", "openpa"),
            "password": pg.get("password", ""),
            "sslmode": pg.get("sslmode", _DEFAULT_POSTGRES_SSLMODE),
        }

    fd, tmp_path = tempfile.mkstemp(prefix=".bootstrap.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            toml.dump(payload, f)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _env_override(key: str, current: Any) -> Any:
    val = os.environ.get(key)
    if val is None or val == "":
        return current
    return val


def resolve_bootstrap() -> dict[str, Any]:
    """Read bootstrap.toml and overlay any ``OPENPA_*`` env vars on top.

    Env vars win so containerized/CI deploys can reuse a single image without
    touching the working dir. Recognized vars:

      OPENPA_DB_PROVIDER, OPENPA_POSTGRES_HOST, OPENPA_POSTGRES_PORT,
      OPENPA_POSTGRES_DATABASE, OPENPA_POSTGRES_USER,
      OPENPA_POSTGRES_PASSWORD, OPENPA_POSTGRES_SSLMODE
    """
    cfg = read_bootstrap()
    cfg["db_provider"] = (_env_override("OPENPA_DB_PROVIDER", cfg["db_provider"]) or _DEFAULT_PROVIDER).strip().lower()
    pg = cfg["postgres"]
    pg["host"] = _env_override("OPENPA_POSTGRES_HOST", pg["host"])
    port_raw = _env_override("OPENPA_POSTGRES_PORT", pg["port"])
    try:
        pg["port"] = int(port_raw)
    except (TypeError, ValueError):
        pg["port"] = _DEFAULT_POSTGRES_PORT
    pg["database"] = _env_override("OPENPA_POSTGRES_DATABASE", pg["database"])
    pg["user"] = _env_override("OPENPA_POSTGRES_USER", pg["user"])
    pg["password"] = _env_override("OPENPA_POSTGRES_PASSWORD", pg["password"])
    pg["sslmode"] = _env_override("OPENPA_POSTGRES_SSLMODE", pg["sslmode"])
    return cfg

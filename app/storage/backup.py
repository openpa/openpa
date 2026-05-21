"""Database backup and restore — backend-aware, runs synchronously.

The upgrader calls :func:`backup` before running migrations and keeps the
returned path so a post-upgrade health-check failure can trigger
:func:`restore`. Both ``opa db backup`` and ``opa db restore`` are thin
wrappers around these.

SQLite snapshots use :func:`shutil.copy2`; the sync engine's WAL is
already flushed by the time we open the file (the application stops
writing during upgrade, and we open via SQLite's backup API so the file
is consistent regardless). Postgres snapshots shell out to ``pg_dump``
and ``psql``; the binaries must be on PATH.
"""

from __future__ import annotations

import gzip
import shutil
import subprocess
import time
from contextlib import closing
from pathlib import Path

from app.databases import get_database_provider


def _default_backup_root() -> Path:
    """Resolve the default backup directory.

    Imported lazily so this module stays importable in environments where
    the settings stack hasn't been initialized yet (e.g., bare ``opa db``
    invocations against a non-existent install).
    """
    from app.config.settings import BaseConfig
    return Path(BaseConfig.OPENPA_SYSTEM_DIR) / "backups"


def _timestamp() -> str:
    return time.strftime("%Y%m%d_%H%M%S", time.localtime())


def backup(to: Path | None = None) -> Path:
    """Take a snapshot. Returns the path the snapshot was written to.

    For SQLite, the snapshot is a gzipped copy of the database file. For
    Postgres, it's the output of ``pg_dump --format=custom`` piped to
    gzip — that's the format ``pg_restore`` consumes on the way back.
    """
    provider = get_database_provider()
    name = provider.name

    if to is None:
        root = _default_backup_root()
        root.mkdir(parents=True, exist_ok=True)
        ext = "sqlite.gz" if name == "sqlite" else "pgdump.gz"
        to = root / f"{_timestamp()}.{ext}"
    else:
        to = Path(to)
        to.parent.mkdir(parents=True, exist_ok=True)

    if name == "sqlite":
        return _backup_sqlite(to)
    if name == "postgres":
        return _backup_postgres(to)
    raise ValueError(f"Backup not supported for provider {name!r}")


def restore(src: Path) -> None:
    """Replace the live database with the contents of ``src``.

    Destructive — caller is responsible for ensuring no writers are
    connected. The CLI prompts for confirmation; the upgrader takes the
    backup path it captured at upgrade-start time.
    """
    src = Path(src)
    if not src.is_file():
        raise FileNotFoundError(src)

    provider = get_database_provider()
    name = provider.name

    if name == "sqlite":
        _restore_sqlite(src)
        return
    if name == "postgres":
        _restore_postgres(src)
        return
    raise ValueError(f"Restore not supported for provider {name!r}")


# ── SQLite ─────────────────────────────────────────────────────────────────


def _sqlite_db_path() -> Path:
    """Resolve the live SQLite database file path."""
    from app.config.settings import BaseConfig
    return Path(BaseConfig.SQLITE_DB_PATH)


def _backup_sqlite(to: Path) -> Path:
    src = _sqlite_db_path()
    if not src.is_file():
        raise FileNotFoundError(f"SQLite database not found at {src}")
    # Use SQLite's online backup API via the sync engine's raw connection.
    # This produces a consistent snapshot without requiring writers to stop.
    import sqlite3

    tmp_dest = to.with_suffix(to.suffix + ".tmp")
    try:
        # ``with sqlite3.connect(...)`` only manages the transaction — it does
        # not close the connection, so the OS file handle on ``tmp_dest`` would
        # linger until GC and block ``unlink()`` on Windows (WinError 32).
        # ``closing()`` forces ``.close()`` on scope exit.
        with (
            closing(sqlite3.connect(str(src))) as src_conn,
            closing(sqlite3.connect(str(tmp_dest))) as dst_conn,
        ):
            src_conn.backup(dst_conn)
        with open(tmp_dest, "rb") as raw, gzip.open(to, "wb") as gz:
            shutil.copyfileobj(raw, gz)
    finally:
        if tmp_dest.exists():
            tmp_dest.unlink()
    return to


def _restore_sqlite(src: Path) -> None:
    dst = _sqlite_db_path()
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.suffixes[-1:] == [".gz"]:
        with gzip.open(src, "rb") as gz, open(dst, "wb") as raw:
            shutil.copyfileobj(gz, raw)
    else:
        shutil.copy2(src, dst)


# ── Postgres ───────────────────────────────────────────────────────────────


def _postgres_env_and_args() -> tuple[dict[str, str], list[str]]:
    """Build the env + connection-flag pieces both pg_dump and psql need."""
    import os

    from app.config.bootstrap import resolve_bootstrap

    cfg = resolve_bootstrap()["postgres"]
    env = os.environ.copy()
    env["PGPASSWORD"] = cfg["password"]
    args = [
        "--host", cfg["host"],
        "--port", str(cfg["port"]),
        "--username", cfg["user"],
        "--dbname", cfg["database"],
    ]
    return env, args


def _backup_postgres(to: Path) -> Path:
    env, args = _postgres_env_and_args()
    cmd = ["pg_dump", "--format=custom", *args]
    with gzip.open(to, "wb") as gz:
        proc = subprocess.run(cmd, env=env, stdout=subprocess.PIPE, check=True)
        gz.write(proc.stdout)
    return to


def _restore_postgres(src: Path) -> None:
    env, args = _postgres_env_and_args()
    cmd = ["pg_restore", "--clean", "--if-exists", *args]
    if src.suffixes[-1:] == [".gz"]:
        with gzip.open(src, "rb") as gz:
            subprocess.run(cmd, env=env, input=gz.read(), check=True)
    else:
        with open(src, "rb") as f:
            subprocess.run(cmd, env=env, input=f.read(), check=True)


__all__ = ["backup", "restore"]

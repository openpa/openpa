"""Autostart runner for long-running processes.

Spawns commands registered in ``autostart_processes`` with a small retry
loop.  Each successful spawn registers a :class:`ProcessInfo` in
``_process_registry`` so the Process Manager API/WebSocket sees it exactly
like an agent-spawned long-running process.  If retries are exhausted, the
registration row's ``last_error`` is populated and the row is surfaced as
a ``failed_to_autostart`` synthetic entry by :func:`list_processes`.
"""

from __future__ import annotations

import asyncio
import os
import platform
import time
import uuid as _uuid
from typing import Any, Dict, Optional, Tuple

from app.config.settings import BaseConfig
from app.storage.autostart_storage import AutostartStorage
from app.tools.builtin.exec_shell import (
    LogWriterState,
    ProcessInfo,
    _DEFAULT_CLEANUP_TTL_HOURS,
    _log_writer_loop,
    _process_registry,
    _shell_for,
    _spawn_command,
    _write_state,
    Var,
)
from app.tools.builtin.exec_shell_input_mode import TerminalState
from app.tools.builtin.exec_shell_pty import _spawn_command_pty
from app.utils.logger import logger


async def _sanity_check(proc, delay: float = 2.0) -> Optional[int]:
    """Wait briefly; return the process's exit code if it died early, else None."""
    try:
        await asyncio.wait_for(proc.wait(), timeout=delay)
    except asyncio.TimeoutError:
        return None
    return proc.returncode


async def spawn_from_autostart(
    row: Dict[str, Any],
    *,
    max_attempts: int = 3,
    base_delay: float = 1.0,
    sanity_delay: float = 2.0,
) -> Tuple[Optional[str], Optional[str]]:
    """Spawn a command from an autostart row with retry/backoff.

    Returns ``(process_id, error)``.  On success ``process_id`` is the new
    registry id and ``error`` is ``None``; on terminal failure both are set
    (``process_id=None``, ``error`` populated with the last-attempt reason).
    """
    command = row.get("command", "").strip()
    if not command:
        return None, "Empty command"

    working_dir = row.get("working_dir") or BaseConfig.OPENPA_WORKING_DIR
    is_pty = bool(row.get("is_pty"))
    system = platform.system()
    shell, shell_flag = _shell_for(system)

    if working_dir:
        try:
            os.makedirs(working_dir, exist_ok=True)
        except Exception as exc:  # noqa: BLE001
            return None, f"Invalid working directory {working_dir!r}: {exc}"

    last_error: Optional[str] = None
    for attempt in range(max_attempts):
        if attempt > 0:
            delay = base_delay * (2 ** (attempt - 1))
            logger.info(
                f"autostart[{row.get('id')}]: waiting {delay:.1f}s before retry "
                f"{attempt + 1}/{max_attempts}",
            )
            await asyncio.sleep(delay)

        try:
            if is_pty:
                proc = await _spawn_command_pty(command, working_dir, 100, 50, system)
            else:
                proc = await _spawn_command(
                    command, working_dir, system, shell, shell_flag,
                )
        except Exception as exc:  # noqa: BLE001
            last_error = f"spawn failed: {exc}"
            logger.warning(f"autostart[{row.get('id')}]: {last_error}")
            continue

        early_rc = await _sanity_check(proc, delay=sanity_delay)
        if early_rc is not None:
            last_error = f"process exited immediately with code {early_rc}"
            logger.warning(f"autostart[{row.get('id')}]: {last_error}")
            if is_pty:
                try:
                    proc.close_master()  # type: ignore[union-attr]
                except Exception:
                    pass
            continue

        # Success — register in the live registry so the Process Manager
        # picks it up.
        process_id = _uuid.uuid4().hex[:8]
        log_dir = os.path.join(
            working_dir, "tools", "builtin", "exec_shell", "stdout", process_id,
        )
        os.makedirs(log_dir, exist_ok=True)
        _write_state(log_dir, {
            "process_id": process_id,
            "command": command,
            "status": "running",
            "return_code": None,
            "created_at": time.time(),
            "exited_at": None,
        })

        writer_state = LogWriterState(
            process_id=process_id,
            log_dir=log_dir,
            current_file_number=1,
            silence_threshold=3.0,
            terminal_state=TerminalState(),
        )
        writer_state.task = asyncio.create_task(_log_writer_loop(proc, writer_state))

        expire_time = time.monotonic() + (_DEFAULT_CLEANUP_TTL_HOURS * 3600)
        _process_registry[process_id] = ProcessInfo(
            process=proc,
            created_at=time.time(),
            working_dir=working_dir,
            command=command,
            log_dir=log_dir,
            log_writer_state=writer_state,
            expire_time=expire_time,
            is_long_running=True,
            is_pty=is_pty,
            task_id=None,
            profile=row.get("profile") or None,
            autostart_id=row.get("id"),
        )

        logger.info(
            f"autostart[{row.get('id')}]: process {process_id} started "
            f"(command={command!r}, attempt={attempt + 1})",
        )
        return process_id, None

    return None, last_error or "Unknown autostart failure"


async def _spawn_one(storage: AutostartStorage, row: Dict[str, Any]) -> None:
    process_id, error = await spawn_from_autostart(row)
    try:
        if error:
            storage.set_error(row["id"], error)
        else:
            storage.clear_error(row["id"])
    except Exception as exc:  # noqa: BLE001
        logger.error(f"autostart[{row.get('id')}]: failed to update status row: {exc}")


async def run_autostart_on_boot(storage: AutostartStorage) -> None:
    """Launch every autostart registration as a fire-and-forget task.

    Intentionally does not await the per-row tasks: a slow or failing spawn
    must not block server startup.  Each task updates its own row with
    success/error state on completion.
    """
    try:
        rows = storage.list_all()
    except Exception as exc:  # noqa: BLE001
        logger.error(f"run_autostart_on_boot: could not list registrations: {exc}")
        return
    if not rows:
        logger.info("run_autostart_on_boot: no autostart registrations")
        return
    logger.info(f"run_autostart_on_boot: launching {len(rows)} registration(s)")
    for row in rows:
        asyncio.create_task(_spawn_one(storage, row))

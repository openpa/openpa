"""Exec Shell built-in tool.

Executes shell commands on the terminal.  Each invocation spawns its own
subprocess (wrapped in the platform shell) and classifies the command by
observing its runtime behaviour:

- Process exits on its own        -> fire-and-forget  (returns result directly)
- TUI escape sequences in output  -> tui_fullscreen   (unsupported)
- Silence / stdin-blocked / long  -> long_running     (returns process_id)

Long-running processes are tracked in ``_process_registry``; their post-
classification stdout/stderr is streamed to incrementally numbered ``.log``
files under ``<working_dir>/tools/builtin/exec_shell/stdout/<process_id>/``.
"""

import asyncio
import json
import os
import platform
import shutil
import time
import uuid as _uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union

from app.config.settings import BaseConfig
from app.tools.builtin.base import BuiltInTool, BuiltInToolResult
from app.types import ToolConfig
from app.tools.builtin.exec_shell_classifier import detect_tui_sequences
from app.tools.builtin.exec_shell_process_inspect import is_child_blocked_on_read
from app.tools.builtin.exec_shell_pty import (
    PtyProcess,
    _looks_like_tty_error,
    _spawn_command_pty,
)
from app.utils.logger import logger

def _resolve_os(selected: Optional[str]) -> str:
    """Return concrete OS; resolve 'Auto-Detect'/None via platform.system()."""
    if not selected or selected == "Auto-Detect":
        return platform.system()
    return selected


def _shell_for(system: str) -> tuple[str, str]:
    """Return (shell, flag) for the given OS."""
    if system == "Windows":
        return "powershell.exe", "-Command"
    return "/bin/bash", "-c"


# ---------------------------------------------------------------------------
# Process registry & log writer state
# ---------------------------------------------------------------------------

@dataclass
class LogWriterState:
    """State for the background stdout log-writer task."""
    process_id: str
    log_dir: str
    current_file_number: int = 1
    silence_threshold: float = 3.0
    task: Optional[asyncio.Task] = None
    stopped: bool = False
    # rotate_lock serialises "writer is appending a chunk" with
    # "reader is flushing & rotating".  Prevents the reader from listing/
    # reading a file that the writer is mid-write on.
    rotate_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Current file handle (open only while the writer holds rotate_lock
    # during a write).  Exposed on the state so flush_and_rotate can close
    # it from outside the writer task.
    current_file: Optional[Any] = None


@dataclass
class ProcessInfo:
    """Tracks a managed long-running process."""
    process: Union[asyncio.subprocess.Process, PtyProcess]
    created_at: float
    working_dir: str
    command: str
    log_dir: str = ""
    log_writer_state: Optional[LogWriterState] = None
    expire_time: float = 0.0
    is_long_running: bool = False
    is_pty: bool = False


_process_registry: Dict[str, ProcessInfo] = {}
_DEFAULT_SILENCE_TIMEOUT = 3.0
_DEFAULT_LONG_RUNNING_TIMEOUT = 10.0
_MAX_CLASSIFICATION_TIME = 30.0
_DEFAULT_CLEANUP_TTL_HOURS = 24
_STATE_FILENAME = "state.json"


def _state_path(log_dir: str) -> str:
    return os.path.join(log_dir, _STATE_FILENAME)


def _write_state(log_dir: str, data: Dict[str, Any]) -> None:
    """Atomically write ``state.json`` in *log_dir*.

    Uses write-to-temp + os.replace so the reader never observes a partial
    file.  Failures are logged but not raised — state.json is advisory and
    callers fall back to ``process.returncode`` when it's missing.
    """
    path = _state_path(log_dir)
    tmp_path = path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp_path, path)
    except Exception as exc:
        logger.error(f"_write_state({log_dir}): {exc}")


def _read_state(log_dir: str) -> Optional[Dict[str, Any]]:
    """Read ``state.json`` from *log_dir*; return None if missing/invalid."""
    path = _state_path(log_dir)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return None
    except Exception as exc:
        logger.error(f"_read_state({log_dir}): {exc}")
        return None

# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

SERVER_NAME = "Exec Shell"


class Var:
    """Variable keys for the Shell Executor tool."""
    LARGE_OUTPUT_MODE = "LARGE_OUTPUT_MODE"
    LARGE_OUTPUT_TOKEN_THRESHOLD = "LARGE_OUTPUT_TOKEN_THRESHOLD"
    LOG_SILENCE_THRESHOLD = "LOG_SILENCE_THRESHOLD"
    CLEANUP_TTL_HOURS = "CLEANUP_TTL_HOURS"


TOOL_CONFIG: ToolConfig = {
    "name": "exec_shell",
    "display_name": "Shell Executor",
    "default_model_group": "low",
    "llm_parameters": {
        "tool_instructions": (
            f"Execute command-line instructions on the terminal. Supports Linux, Windows, and macOS."
        ),
        "system_prompt": (
            "You are a helpful assistant that can execute shell commands and provide their output.\n"
            "Don't answer any questions or provide explanations. Only execute the command and return the output."
        )
    },
    "required_config": {
        Var.LARGE_OUTPUT_MODE: {
            "description": (
                "How to handle large command output. "
                "'manual': ask user before returning output exceeding the token threshold. "
                "'automatic': always return full output regardless of size."
            ),
            "type": "string",
            "enum": ["manual", "automatic"],
            "default": "automatic",
        },
        Var.LARGE_OUTPUT_TOKEN_THRESHOLD: {
            "description": (
                "Token count threshold for large output handling. "
                "Only applies when LARGE_OUTPUT_MODE is 'manual'. "
                "Default: 10000."
            ),
            "type": "number",
            "default": 10000,
        },
        Var.LOG_SILENCE_THRESHOLD: {
            "description": (
                "Seconds of silence before closing the current log file "
                "and starting a new one for long-running processes. "
                "Default: 3."
            ),
            "type": "number",
            "default": 3,
        },
        Var.CLEANUP_TTL_HOURS: {
            "description": (
                "Hours before expired process data (logs, registry entries) "
                "is automatically cleaned up. Default: 24."
            ),
            "type": "number",
            "default": 24,
        },
    },
    "arguments": {
        "type": "object",
        "properties": {
            "os": {
                "type": "string",
                "enum": ["Windows", "Linux", "Darwin", "Auto-Detect"],
                "default": "Auto-Detect",
                "description": "Operating system to target for shell selection.",
            },
        },
    },
}


async def _spawn_command(
    command: str, working_dir: str, system: str, shell: str, shell_flag: str,
) -> asyncio.subprocess.Process:
    """Spawn a command as a standalone subprocess with piped stdin/stdout/stderr."""
    if system == "Windows":
        proc = await asyncio.create_subprocess_exec(
            shell, "-NoLogo", "-NoProfile", shell_flag, command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=working_dir,
        )
    else:
        proc = await asyncio.create_subprocess_exec(
            shell, shell_flag, command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=working_dir,
        )
    return proc


async def _check_stdin_blocked(process: asyncio.subprocess.Process) -> bool | None:
    """True if the child process is blocked on stdin; None if inconclusive.

    Uses the host OS (the machine OpenPA runs on), not the caller's target OS:
    stdin-block detection inspects the live process table via /proc (Linux) or
    Windows APIs, which only the host provides.
    """
    return await is_child_blocked_on_read(process.pid, platform.system())


# ---------------------------------------------------------------------------
# Classification (pre-long-running): read initial output and decide category
# ---------------------------------------------------------------------------

async def _classify_stream(
    process: Union[asyncio.subprocess.Process, PtyProcess],
    overall_timeout: float = 120.0,
    silence_timeout: float = _DEFAULT_SILENCE_TIMEOUT,
    long_running_timeout: float = _DEFAULT_LONG_RUNNING_TIMEOUT,
    skip_tui: bool = False,
) -> dict:
    """Read stdout/stderr from *process* until classification is decided.

    Returns a dict with: stdout, stderr, return_code, completed, category,
    waiting_for_input, and optionally timed_out / tui_detected.
    """
    stdout_buf = ""
    stderr_buf = ""
    stdout_done = False
    stderr_done = False
    start = time.monotonic()
    last_data = start

    while not (stdout_done and stderr_done):
        elapsed = time.monotonic() - start
        if elapsed >= overall_timeout:
            return {
                "stdout": stdout_buf, "stderr": stderr_buf,
                "return_code": None, "completed": False,
                "category": "long_running",
                "waiting_for_input": False, "timed_out": True,
            }

        task_map: Dict[asyncio.Task, str] = {}
        if not stdout_done and process.stdout:
            task_map[asyncio.ensure_future(process.stdout.read(4096))] = "stdout"
        if not stderr_done and process.stderr:
            task_map[asyncio.ensure_future(process.stderr.read(4096))] = "stderr"
        if not task_map:
            break

        remaining = overall_timeout - elapsed
        done, pending = await asyncio.wait(
            task_map.keys(),
            timeout=min(silence_timeout, remaining),
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

        if not done:
            # Silence window elapsed without new data.
            if process.returncode is not None:
                return {
                    "stdout": stdout_buf, "stderr": stderr_buf,
                    "return_code": process.returncode, "completed": True,
                    "category": "fire_and_forget",
                }

            silence_duration = time.monotonic() - last_data
            if silence_duration < silence_timeout:
                continue

            blocked = await _check_stdin_blocked(process)
            if blocked is True:
                return {
                    "stdout": stdout_buf, "stderr": stderr_buf,
                    "return_code": None, "completed": False,
                    "category": "long_running",
                    "waiting_for_input": True,
                }
            if silence_duration >= long_running_timeout:
                return {
                    "stdout": stdout_buf, "stderr": stderr_buf,
                    "return_code": None, "completed": False,
                    "category": "long_running",
                    "waiting_for_input": False,
                }
            continue

        for t in done:
            data = t.result()
            stream_name = task_map[t]
            if data == b"":
                if stream_name == "stdout":
                    stdout_done = True
                else:
                    stderr_done = True
                continue
            chunk = data.decode("utf-8", errors="replace")
            last_data = time.monotonic()

            if stream_name == "stdout":
                if not skip_tui and detect_tui_sequences(chunk):
                    return {
                        "stdout": stdout_buf + chunk,
                        "stderr": stderr_buf,
                        "return_code": None, "completed": False,
                        "category": "tui_fullscreen",
                        "tui_detected": True,
                    }
                stdout_buf += chunk
            else:
                stderr_buf += chunk

        if elapsed >= _MAX_CLASSIFICATION_TIME:
            return {
                "stdout": stdout_buf, "stderr": stderr_buf,
                "return_code": None, "completed": False,
                "category": "long_running",
                "waiting_for_input": False,
            }

    # Both streams closed -> process finished.
    try:
        await asyncio.wait_for(process.wait(), timeout=5.0)
    except (asyncio.TimeoutError, Exception):
        pass
    return {
        "stdout": stdout_buf, "stderr": stderr_buf,
        "return_code": process.returncode if process.returncode is not None else 0,
        "completed": True,
        "category": "fire_and_forget",
    }


# ---------------------------------------------------------------------------
# Background log writer for long-running processes
# ---------------------------------------------------------------------------

async def _log_writer_loop(
    process: asyncio.subprocess.Process,
    state: LogWriterState,
) -> None:
    """Stream process stdout/stderr into numbered ``.log`` files.

    Rotation rules:
    - New data opens ``<current_file_number>.log`` (if not already open) and
      appends.
    - ``silence_threshold`` seconds without new data closes the current file
      and increments ``current_file_number`` — the next output starts a new
      file.
    - On process exit, whatever file is open is closed, then a **fresh**
      ``<current_file_number>.log`` is created containing only
      ``__OPA_EXIT__:<rc>\\n``.  This keeps the sentinel in its own file.
    - External callers (``exec_shell_output``) can invoke
      ``flush_and_rotate(state)`` to force close the current file and
      increment the counter before listing files on disk.
    """
    os.makedirs(state.log_dir, exist_ok=True)

    # Persistent read tasks — never cancelled mid-stream.  Cancelling a
    # StreamReader.read() mid-flight on Windows ProactorEventLoop can drop
    # data that had already been pulled into the transport buffer; by
    # replacing cancelled tasks every silence cycle the original writer
    # loop lost chunks that arrived right as cancellation fired.  Keeping
    # one task per stream alive for the stream's entire lifetime, and only
    # replacing it when it completes with data (or EOF), avoids the race.
    read_tasks: Dict[str, asyncio.Task] = {}
    if process.stdout:
        read_tasks["stdout"] = asyncio.ensure_future(process.stdout.read(4096))
    if process.stderr:
        read_tasks["stderr"] = asyncio.ensure_future(process.stderr.read(4096))

    try:
        while not state.stopped and read_tasks:
            done, _ = await asyncio.wait(
                read_tasks.values(),
                timeout=state.silence_threshold,
                return_when=asyncio.FIRST_COMPLETED,
            )

            if not done:
                # Silence — rotate the current file (if any) and keep the
                # read tasks alive for the next cycle.
                async with state.rotate_lock:
                    if state.current_file is not None:
                        state.current_file.close()
                        state.current_file = None
                        state.current_file_number += 1
                if process.returncode is not None and not read_tasks:
                    break
                continue

            chunks: List[tuple[str, str]] = []
            finished_streams: List[str] = []
            for stream_name, task in list(read_tasks.items()):
                if task not in done:
                    continue
                data = task.result()
                if data == b"":
                    finished_streams.append(stream_name)
                    continue
                chunks.append((stream_name, data.decode("utf-8", errors="replace")))
                # Replace with a fresh read on the same stream.
                stream_obj = process.stdout if stream_name == "stdout" else process.stderr
                if stream_obj is not None:
                    read_tasks[stream_name] = asyncio.ensure_future(stream_obj.read(4096))

            for stream_name in finished_streams:
                read_tasks.pop(stream_name, None)

            if chunks:
                async with state.rotate_lock:
                    if state.current_file is None:
                        log_path = os.path.join(
                            state.log_dir, f"{state.current_file_number}.log",
                        )
                        state.current_file = open(log_path, "a", encoding="utf-8")
                    for stream_name, chunk in chunks:
                        if stream_name == "stderr":
                            state.current_file.write(f"[STDERR] {chunk}")
                        else:
                            state.current_file.write(chunk)
                    state.current_file.flush()
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        logger.error(f"log_writer_loop({state.process_id}): {exc}")
    finally:
        # Cancel any reads still outstanding (streams not EOF'd) before
        # closing files.  At shutdown the data would be lost anyway.
        for task in list(read_tasks.values()):
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        try:
            await asyncio.wait_for(process.wait(), timeout=5.0)
        except (asyncio.TimeoutError, Exception):
            pass

        # Exit status goes into state.json alongside the .log files.  The
        # .log files now contain only program output — no sentinel.
        try:
            async with state.rotate_lock:
                if state.current_file is not None:
                    state.current_file.close()
                    state.current_file = None
                    state.current_file_number += 1
                rc = process.returncode if process.returncode is not None else -1
                existing = _read_state(state.log_dir) or {}
                existing.update({
                    "status": "completed",
                    "return_code": rc,
                    "exited_at": time.time(),
                })
                _write_state(state.log_dir, existing)
        except Exception as exc:
            logger.error(f"log_writer_loop({state.process_id}): state.json update failed: {exc}")

        state.stopped = True
        logger.debug(
            f"log_writer_loop({state.process_id}): finished, "
            f"exit_code={process.returncode}"
        )


async def flush_and_rotate(state: LogWriterState) -> None:
    """Close the currently-open log file and advance the counter.

    Invoked by ``exec_shell_output`` before it lists files on disk so that
    every file it sees is fully written and closed.
    """
    async with state.rotate_lock:
        if state.current_file is not None:
            try:
                state.current_file.close()
            except Exception:
                pass
            state.current_file = None
            state.current_file_number += 1


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

async def _cleanup_stale_processes() -> int:
    """Kill and remove processes whose ``expire_time`` has passed."""
    now = time.monotonic()
    stale_ids = [
        pid for pid, info in _process_registry.items()
        if now >= info.expire_time
    ]
    for pid in stale_ids:
        info = _process_registry.pop(pid, None)
        if not info:
            continue
        if info.log_writer_state and info.log_writer_state.task:
            info.log_writer_state.stopped = True
            info.log_writer_state.task.cancel()
            try:
                await info.log_writer_state.task
            except (asyncio.CancelledError, Exception):
                pass
        if info.process.returncode is None:
            try:
                info.process.kill()
                await asyncio.wait_for(info.process.wait(), timeout=5.0)
            except (asyncio.TimeoutError, Exception):
                pass
        if info.is_pty:
            try:
                info.process.close_master()  # type: ignore[union-attr]
            except Exception:
                pass
        if info.log_dir and os.path.isdir(info.log_dir):
            shutil.rmtree(info.log_dir, ignore_errors=True)
        logger.info(f"Cleaned up expired process {pid} ({info.command!r})")
    return len(stale_ids)


class ExecShellTool(BuiltInTool):
    name: str = "exec_shell"
    description: str = (
        "Executes a shell command on the terminal and returns its output. "
        "Automatically detects the operating system and uses the appropriate shell.\n"
        "Supports interactive commands that require a TTY (ssh, docker exec -it, "
        "kubectl exec -it, REPLs like python -i / mysql / psql). If a command "
        "fails with a TTY-required error (e.g. 'the input device is not a TTY'), "
        "it is automatically respawned under a pseudo-terminal. For commands "
        "that hang without a TTY instead of erroring (notably ssh), pass "
        "`pty: true` explicitly. In PTY mode stdout and stderr are merged into "
        "`stdout`.\n"
        "E.g, 'check disk usage', 'ping google.com', or 'ssh user@host' with pty=true."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The shell command to execute.",
            },
            "working_directory": {
                "type": "string",
                "description": "The working directory to run the command in. Defaults to OPENPA_WORKING_DIR.",
            },
            "timeout": {
                "type": "integer",
                "description": "Maximum time in seconds to wait for fire-and-forget commands. Defaults to 120.",
            },
            "silence_timeout": {
                "type": "number",
                "description": (
                    "Seconds of output silence before checking whether the "
                    "process is waiting for input. Default: 3."
                ),
            },
            "pty": {
                "type": "boolean",
                "description": (
                    "Force PTY (pseudo-terminal) allocation on/off. When true, "
                    "the command runs under a real TTY — required for ssh, "
                    "docker exec -it, kubectl exec -it, and interactive REPLs. "
                    "In PTY mode, stderr is merged into stdout. Default: "
                    "auto-detect based on the command."
                ),
            },
            "cols": {
                "type": "integer",
                "description": "Terminal width (columns) for PTY mode. Default 80.",
            },
            "rows": {
                "type": "integer",
                "description": "Terminal height (rows) for PTY mode. Default 24.",
            },
            "os": {
                "type": "string",
                "enum": ["Windows", "Linux", "Darwin"],
                "default": "Linux",
                "description": (
                    "Target operating system for shell selection. Controls "
                    "which shell binary is used to run the command "
                    "(powershell.exe on Windows, /bin/bash on Linux/Darwin)."
                ),
            },
        },
        "required": ["command"],
    }

    async def run(self, arguments: Dict[str, Any]) -> BuiltInToolResult:
        command = arguments.get("command", "").strip()
        working_directory = (
            arguments.get("working_directory", None)
            or arguments.get("_working_directory", None)
            or BaseConfig.OPENPA_WORKING_DIR
        )
        timeout = arguments.get("timeout", 120)
        silence_timeout = arguments.get("silence_timeout", _DEFAULT_SILENCE_TIMEOUT)
        pty_arg = arguments.get("pty")
        cols = int(arguments.get("cols") or 80)
        rows = int(arguments.get("rows") or 24)
        system = _resolve_os(arguments.get("os"))
        shell, shell_flag = _shell_for(system)
        variables = arguments.get("_variables") or {}
        log_silence_threshold = float(variables.get(Var.LOG_SILENCE_THRESHOLD) or 3.0)
        cleanup_ttl_hours = float(variables.get(Var.CLEANUP_TTL_HOURS) or _DEFAULT_CLEANUP_TTL_HOURS)

        if not command:
            return BuiltInToolResult(
                structured_content={
                    "error": "Missing parameter",
                    "message": "The 'command' parameter is required.",
                }
            )

        if working_directory:
            os.makedirs(working_directory, exist_ok=True)
            if not os.path.isdir(working_directory):
                return BuiltInToolResult(
                    structured_content={
                        "error": "Invalid working directory",
                        "message": f"Directory does not exist: {working_directory}",
                    }
                )

        await _cleanup_stale_processes()

        use_pty = bool(pty_arg) if pty_arg is not None else False
        auto_retry_allowed = pty_arg is None

        async def _spawn(pty_mode: bool):
            if pty_mode:
                return await _spawn_command_pty(command, working_directory, cols, rows, system)
            return await _spawn_command(command, working_directory, system, shell, shell_flag)

        logger.debug(
            f"exec_shell: running '{command}' on {system} with shell {shell} "
            f"(pty={use_pty})"
        )

        try:
            proc = await _spawn(use_pty)
        except Exception as e:
            return BuiltInToolResult(
                structured_content={
                    "error": "Spawn error",
                    "message": f"Failed to spawn command: {str(e)}",
                    "command": command,
                    "os": system,
                    "shell": shell,
                    "pty": use_pty,
                }
            )

        try:
            classify_timeout = min(timeout, 5.0) if use_pty else timeout
            result = await _classify_stream(
                proc,
                overall_timeout=classify_timeout,
                silence_timeout=silence_timeout,
                long_running_timeout=_DEFAULT_LONG_RUNNING_TIMEOUT,
                skip_tui=use_pty,
            )

            # Runtime PTY detection: if the command exited with an error whose
            # output looks like a "TTY required" failure, respawn under PTY
            # and re-classify. Skipped if the caller pinned `pty` explicitly.
            if (
                auto_retry_allowed
                and not use_pty
                and result.get("completed")
                and (result.get("return_code") or 0) != 0
                and _looks_like_tty_error(result.get("stdout", ""), result.get("stderr", ""))
            ):
                logger.info(
                    f"exec_shell: detected TTY-required error for '{command}', "
                    "respawning under PTY"
                )
                use_pty = True
                try:
                    proc = await _spawn(True)
                except Exception as e:
                    return BuiltInToolResult(
                        structured_content={
                            "error": "Spawn error",
                            "message": f"Failed to respawn command under PTY: {str(e)}",
                            "command": command,
                            "os": system,
                            "shell": shell,
                            "pty": True,
                        }
                    )
                classify_timeout = min(timeout, 5.0)
                result = await _classify_stream(
                    proc,
                    overall_timeout=classify_timeout,
                    silence_timeout=silence_timeout,
                    long_running_timeout=_DEFAULT_LONG_RUNNING_TIMEOUT,
                    skip_tui=True,
                )

            # In PTY mode, user opted into a terminal session — don't reject
            # on TUI sequences; treat as long_running instead.
            if use_pty and result.get("category") == "tui_fullscreen":
                result["category"] = "long_running"
                result["completed"] = False
                result.pop("tui_detected", None)

            category = result.get("category", "fire_and_forget")
            logger.debug(
                f"exec_shell: classification result for '{command}': "
                f"category={category}, completed={result.get('completed')}"
            )

            if result["completed"]:
                # Drain any remaining output the classifier might have missed
                # (rare, but happens if EOF landed in the last read).
                if use_pty:
                    try:
                        proc.close_master()  # type: ignore[union-attr]
                    except Exception:
                        pass
                return BuiltInToolResult(
                    structured_content={
                        "stdout": result["stdout"],
                        "stderr": result["stderr"],
                        "return_code": result["return_code"],
                        "category": "fire_and_forget",
                        "command": command,
                        "os": system,
                        "shell": shell,
                        **({"pty": True, "stderr_merged_into_stdout": True} if use_pty else {}),
                        **({"timed_out": True} if result.get("timed_out") else {}),
                    }
                )

            if category == "tui_fullscreen":
                try:
                    proc.kill()
                    await asyncio.wait_for(proc.wait(), timeout=5.0)
                except (asyncio.TimeoutError, Exception):
                    pass
                if use_pty:
                    try:
                        proc.close_master()  # type: ignore[union-attr]
                    except Exception:
                        pass
                return BuiltInToolResult(
                    structured_content={
                        "error": "TUI application detected",
                        "unsupported": True,
                        "category": "tui_fullscreen",
                        "stdout": result["stdout"],
                        "stderr": result["stderr"],
                        "interrupted": True,
                        "message": (
                            f"The command '{command}' appears to be a TUI / full-screen "
                            "application (detected terminal control escape sequences). "
                            "This type of command is not supported."
                        ),
                        "command": command,
                        "os": system,
                        "shell": shell,
                    }
                )

            # --- LONG_RUNNING: keep subprocess alive, start log writer ---
            process_id = _uuid.uuid4().hex[:8]
            log_dir = os.path.join(
                working_directory, "tools", "builtin", "exec_shell",
                "stdout", process_id,
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
                silence_threshold=log_silence_threshold,
            )
            writer_state.task = asyncio.create_task(
                _log_writer_loop(proc, writer_state)
            )

            expire_time = time.monotonic() + (cleanup_ttl_hours * 3600)
            _process_registry[process_id] = ProcessInfo(
                process=proc,
                created_at=time.monotonic(),
                working_dir=working_directory,
                command=command,
                log_dir=log_dir,
                log_writer_state=writer_state,
                expire_time=expire_time,
                is_long_running=True,
                is_pty=use_pty,
            )

            logger.info(
                f"exec_shell: long-running process {process_id} started "
                f"(command={command!r}, pty={use_pty})"
            )

            return BuiltInToolResult(
                structured_content={
                    "process_id": process_id,
                    "stdout": result.get("stdout", ""),
                    "stderr": result.get("stderr", ""),
                    "status": "running",
                    "category": "long_running",
                    "waiting_for_input": result.get("waiting_for_input", False),
                    "command": command,
                    "os": system,
                    "shell": shell,
                    **({"pty": True, "stderr_merged_into_stdout": True} if use_pty else {}),
                    "message": (
                        f"Long-running process started with id '{process_id}'. "
                        "Use exec shell stdout to read output, exec shell input "
                        "to send input, or exec_shell_stop to terminate."
                    ),
                }
            )

        except Exception as e:
            try:
                proc.kill()
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except (asyncio.TimeoutError, Exception):
                pass
            if use_pty:
                try:
                    proc.close_master()  # type: ignore[union-attr]
                except Exception:
                    pass
            return BuiltInToolResult(
                structured_content={
                    "error": "Execution error",
                    "message": f"Failed to execute command: {str(e)}",
                    "command": command,
                    "os": system,
                    "shell": shell,
                    "pty": use_pty,
                }
            )


class ExecShellInputTool(BuiltInTool):
    name: str = "exec_shell_input"
    description: str = (
        "Send input to a running process. Use this after exec_shell returns a "
        "process_id. Writes the input text to the process's stdin. "
        "Use exec shell output to read the response.\n"
        'E.g., Send input to process "708e9873": {"process_id":"708e9873","input_text":"my input"}\n'
        "For PTY processes, use line_ending=\"none\" to send raw control "
        "characters (e.g. input_text=\"\\x03\" for Ctrl+C, \"\\x04\" for Ctrl+D).\n"
        "Required: process_id, input_text"
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "process_id": {
                "type": "string",
                "description": "The process_id returned by exec_shell.",
            },
            "input_text": {
                "type": "string",
                "description": (
                    "The text to send to the process's stdin."
                ),
            },
            "line_ending": {
                "type": "string",
                "enum": ["\n", "\r", "\r\n", "none"],
                "description": (
                    "Line terminator appended to input_text. 'none' sends the "
                    "raw bytes (useful for control characters). Default: '\\n'."
                ),
            },
        },
        "required": ["process_id", "input_text"],
    }

    async def run(self, arguments: Dict[str, Any]) -> BuiltInToolResult:
        process_id = arguments.get("process_id", "").strip()
        input_text = arguments.get("input_text", "")
        line_ending = arguments.get("line_ending", "\n")

        if not process_id:
            return BuiltInToolResult(
                structured_content={
                    "error": "Missing parameter",
                    "message": "The 'process_id' parameter is required.",
                }
            )

        info = _process_registry.get(process_id)
        if not info:
            return BuiltInToolResult(
                structured_content={
                    "error": "Process not found",
                    "message": (
                        f"No running process with id '{process_id}'. "
                        "It may have exited or been cleaned up."
                    ),
                }
            )

        process = info.process

        if process.returncode is not None:
            return BuiltInToolResult(
                structured_content={
                    "error": "Process already exited",
                    "message": (
                        f"Process '{process_id}' has already exited with "
                        f"code {process.returncode}. Use exec shell stdout "
                        "to read remaining output."
                    ),
                    "return_code": process.returncode,
                    "process_id": process_id,
                }
            )

        try:
            if line_ending == "none":
                payload = input_text
            else:
                payload = input_text + line_ending
            process.stdin.write(payload.encode("utf-8"))
            await process.stdin.drain()
        except Exception as e:
            return BuiltInToolResult(
                structured_content={
                    "error": "Write error",
                    "message": f"Failed to write to process stdin: {str(e)}",
                    "process_id": process_id,
                }
            )

        return BuiltInToolResult(
            structured_content={
                "process_id": process_id,
                "input_sent": True,
                "command": info.command,
                "message": (
                    "Input sent. Use exec shell stdout to read the response."
                ),
            }
        )


class ExecShellOutputTool(BuiltInTool):
    name: str = "exec_shell_output"
    description: str = (
        "Read stdout from a long-running process.\n"
        "Note that some applications do not produce stdout immediately, "
        "so you should combine it with a **Sleep Tool** to wait for stdout.\n"
        "E.g., read stdout for process '708e9873'\n"
        "Required: process_id"
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "process_id": {
                "type": "string",
                "description": "The process_id returned by exec_shell.",
            },
        },
        "required": ["process_id"],
    }

    async def run(self, arguments: Dict[str, Any]) -> BuiltInToolResult:
        process_id = arguments.get("process_id", "").strip()
        variables = arguments.get("_variables") or {}
        large_output_mode = variables.get(Var.LARGE_OUTPUT_MODE) or "automatic"
        token_threshold = int(variables.get(Var.LARGE_OUTPUT_TOKEN_THRESHOLD) or 10000)

        if not process_id:
            return BuiltInToolResult(
                structured_content={
                    "error": "Missing parameter",
                    "message": "The 'process_id' parameter is required.",
                }
            )

        info = _process_registry.get(process_id)
        if not info:
            return BuiltInToolResult(
                structured_content={
                    "error": "Process not found",
                    "message": (
                        f"No process with id '{process_id}'. "
                        "It may have been cleaned up."
                    ),
                }
            )

        if info.log_writer_state:
            await flush_and_rotate(info.log_writer_state)

        if not info.log_dir or not os.path.isdir(info.log_dir):
            return BuiltInToolResult(
                structured_content={
                    "process_id": process_id,
                    "stdout": "",
                    "completed": info.process.returncode is not None,
                    "return_code": info.process.returncode,
                    "command": info.command,
                    "message": "No log output available yet.",
                }
            )

        log_files: List[str] = [
            f for f in os.listdir(info.log_dir) if f.endswith(".log")
        ]
        log_files.sort(key=lambda f: int(f.split(".")[0]))

        merged_output = ""
        read_paths: List[str] = []

        for fname in log_files:
            fpath = os.path.join(info.log_dir, fname)
            try:
                with open(fpath, "r", encoding="utf-8") as fh:
                    content = fh.read()
            except Exception:
                continue
            read_paths.append(fpath)
            merged_output += content

        for fpath in read_paths:
            try:
                os.remove(fpath)
            except Exception:
                pass

        cleanup_ttl_hours = float(
            variables.get(Var.CLEANUP_TTL_HOURS) or _DEFAULT_CLEANUP_TTL_HOURS
        )
        info.expire_time = time.monotonic() + (cleanup_ttl_hours * 3600)

        state_data = _read_state(info.log_dir) or {}
        completed = state_data.get("status") == "completed"
        exit_code: Optional[int] = state_data.get("return_code")
        if not completed and info.process.returncode is not None:
            completed = True
            exit_code = info.process.returncode

        total_tokens = 0
        truncated = False
        if merged_output:
            try:
                from tiktoken import encoding_for_model
                encoder = encoding_for_model("gpt-4o")
                total_tokens = len(encoder.encode(merged_output))
            except Exception:
                total_tokens = len(merged_output) // 4

            if (
                large_output_mode == "manual"
                and total_tokens > token_threshold
            ):
                truncated = True
                merged_output = (
                    f"[Output too large: {total_tokens} tokens "
                    f"(threshold: {token_threshold}). "
                    "Please confirm with the user before retrieving the full output.]"
                )

        return BuiltInToolResult(
            structured_content={
                "process_id": process_id,
                "stdout": merged_output,
                "completed": completed,
                "return_code": exit_code,
                "truncated": truncated,
                "total_tokens": total_tokens,
                "command": info.command,
            }
        )


class ExecShellStopTool(BuiltInTool):
    name: str = "exec_shell_stop"
    description: str = (
        "Stop a running process by process_id. "
        "Sends SIGTERM, waits briefly, then SIGKILL if needed.\n"
        "E.g., stop process '708e9873'\n"
        "Required: process_id"
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "process_id": {
                "type": "string",
                "description": "The process_id of the process to stop.",
            },
        },
        "required": ["process_id"],
    }

    async def run(self, arguments: Dict[str, Any]) -> BuiltInToolResult:
        process_id = arguments.get("process_id", "").strip()

        if not process_id:
            return BuiltInToolResult(
                structured_content={
                    "error": "Missing parameter",
                    "message": "The 'process_id' parameter is required.",
                }
            )

        info = _process_registry.pop(process_id, None)
        if not info:
            return BuiltInToolResult(
                structured_content={
                    "error": "Process not found",
                    "message": (
                        f"No running process with id '{process_id}'. "
                        "It may have already exited or been cleaned up."
                    ),
                }
            )

        process = info.process

        if info.log_writer_state and info.log_writer_state.task:
            info.log_writer_state.stopped = True
            info.log_writer_state.task.cancel()
            try:
                await info.log_writer_state.task
            except (asyncio.CancelledError, Exception):
                pass

        # PTY processes need EOF (^D) before terminate so interactive shells
        # (ssh, bash -i) exit cleanly instead of being killed mid-session.
        if info.is_pty and process.returncode is None:
            try:
                process.send_eof()  # type: ignore[union-attr]
            except Exception:
                pass
            try:
                await asyncio.wait_for(process.wait(), timeout=2.0)
            except (asyncio.TimeoutError, Exception):
                pass

        if process.stdin:
            try:
                process.stdin.close()
            except Exception:
                pass

        if process.returncode is None:
            try:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    process.kill()
                    try:
                        await asyncio.wait_for(process.wait(), timeout=5.0)
                    except asyncio.TimeoutError:
                        pass
            except Exception:
                pass

        if info.is_pty:
            try:
                process.close_master()  # type: ignore[union-attr]
            except Exception:
                pass

        final_output = ""
        if info.log_dir and os.path.isdir(info.log_dir):
            log_files = sorted(
                [f for f in os.listdir(info.log_dir) if f.endswith(".log")],
                key=lambda f: int(f.split(".")[0]),
            )
            for fname in log_files:
                try:
                    with open(os.path.join(info.log_dir, fname), "r",
                              encoding="utf-8") as fh:
                        final_output += fh.read()
                except Exception:
                    pass
            shutil.rmtree(info.log_dir, ignore_errors=True)

        return BuiltInToolResult(
            structured_content={
                "stdout": final_output,
                "return_code": process.returncode,
                "stopped": True,
                "process_id": process_id,
                "command": info.command,
            }
        )


def get_tools(config: dict) -> list[BuiltInTool]:
    """Return tool instances for this server."""
    return [
        ExecShellTool(),
        ExecShellInputTool(),
        ExecShellOutputTool(),
        ExecShellStopTool(),
    ]

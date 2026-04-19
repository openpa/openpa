"""Exec Shell built-in tool.

Executes shell commands on the terminal. Detects the operating system and
uses the appropriate shell (PowerShell on Windows, /bin/bash on Linux/macOS).

Classification is **behaviour-based** — the tool observes what the process
actually does at runtime rather than guessing from the command name:

- Process exits on its own       → fire-and-forget  (returns result directly)
- TUI escape sequences in output → tui_fullscreen   (unsupported)
- Everything else                → long-running      (returns process_id)
"""

import asyncio
import os
import platform
import shutil
import time
import uuid as _uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from app.config.settings import BaseConfig
from app.tools.builtin.base import BuiltInTool, BuiltInToolResult
from app.types import ToolConfig
from app.tools.builtin.exec_shell_classifier import detect_tui_sequences
from app.tools.builtin.exec_shell_process_inspect import is_child_blocked_on_read
from app.utils.logger import logger

# Detect OS and shell once at startup
_SYSTEM = platform.system()  # "Windows", "Linux", "Darwin"

if _SYSTEM == "Windows":
    _SHELL = "powershell.exe"
    _SHELL_FLAG = "-Command"
else:
    # Linux and macOS (Darwin)
    _SHELL = "/bin/bash"
    _SHELL_FLAG = "-c"


# ---------------------------------------------------------------------------
# Process registry & log writer state
# ---------------------------------------------------------------------------

@dataclass
class LogWriterState:
    """State for a background stdout log-writer task."""
    process_id: str
    log_dir: str
    current_file_number: int = 1
    silence_threshold: float = 3.0
    task: Optional[asyncio.Task] = None
    stopped: bool = False


@dataclass
class ProcessInfo:
    """Tracks a managed process (long-running or interactive)."""
    process: asyncio.subprocess.Process
    created_at: float
    working_dir: str
    command: str
    log_dir: str = ""
    log_writer_state: Optional[LogWriterState] = None
    expire_time: float = 0.0          # monotonic time for auto-cleanup
    is_long_running: bool = False


_process_registry: Dict[str, ProcessInfo] = {}
_DEFAULT_SILENCE_TIMEOUT = 3.0  # seconds of silence → trigger introspection check
_DEFAULT_LONG_RUNNING_TIMEOUT = 10.0  # seconds of continuous silence → long-running
_MAX_CLASSIFICATION_TIME = 30.0  # seconds — even with continuous output, classify as long_running
_DEFAULT_CLEANUP_TTL_HOURS = 24  # default expiration for process data
_EXIT_SENTINEL = "__OPA_EXIT__"  # written to log when process exits

# Per-profile persistent shell sessions
_shell_sessions: Dict[str, asyncio.subprocess.Process] = {}


async def _spawn_long_running(command: str, working_dir: str) -> asyncio.subprocess.Process:
    """Spawn a standalone subprocess for a long-running command.

    Unlike the persistent shell session, this process IS the command — no
    marker wrapping, no shared stdin.  stdout/stderr are piped so the
    background log writer can consume them.
    """
    if _SYSTEM == "Windows":
        proc = await asyncio.create_subprocess_exec(
            _SHELL, "-NoLogo", "-NoProfile", _SHELL_FLAG, command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=working_dir,
        )
    else:
        proc = await asyncio.create_subprocess_exec(
            _SHELL, _SHELL_FLAG, command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=working_dir,
        )
    return proc


async def _log_writer_loop(
    process: asyncio.subprocess.Process,
    state: LogWriterState,
) -> None:
    """Background task: read process stdout/stderr and write to numbered log files.

    - Writes to ``<log_dir>/<N>.log`` where N starts at ``state.current_file_number``.
    - If no output arrives for ``state.silence_threshold`` seconds the current
      file is closed; subsequent output goes to a new file with N+1.
    - stderr chunks are prefixed with ``[STDERR] `` for disambiguation.
    - On process exit a ``__OPA_EXIT__:<code>`` sentinel is appended.
    """
    os.makedirs(state.log_dir, exist_ok=True)
    current_file = None

    try:
        while not state.stopped:
            task_map: Dict[asyncio.Task, str] = {}
            if process.stdout:
                task_map[asyncio.ensure_future(process.stdout.read(4096))] = "stdout"
            if process.stderr:
                task_map[asyncio.ensure_future(process.stderr.read(4096))] = "stderr"
            if not task_map:
                break

            done, pending = await asyncio.wait(
                task_map.keys(),
                timeout=state.silence_threshold,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass

            if not done:
                # Silence — close the current log file so it's considered complete.
                if current_file is not None:
                    current_file.close()
                    current_file = None
                    state.current_file_number += 1
                if process.returncode is not None:
                    break
                continue

            eof_seen = False
            for t in done:
                data = t.result()
                if data == b"":
                    eof_seen = True
                    continue
                chunk = data.decode("utf-8", errors="replace")
                stream_name = task_map[t]

                if current_file is None:
                    log_path = os.path.join(
                        state.log_dir,
                        f"{state.current_file_number}.log",
                    )
                    current_file = open(log_path, "a", encoding="utf-8")

                if stream_name == "stderr":
                    current_file.write(f"[STDERR] {chunk}")
                else:
                    current_file.write(chunk)
                current_file.flush()

            if eof_seen:
                break
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        logger.error(f"log_writer_loop({state.process_id}): {exc}")
    finally:
        # Wait for the process to fully exit so we can capture the exit code.
        try:
            await asyncio.wait_for(process.wait(), timeout=5.0)
        except (asyncio.TimeoutError, Exception):
            pass

        if current_file is None:
            log_path = os.path.join(
                state.log_dir, f"{state.current_file_number}.log",
            )
            current_file = open(log_path, "a", encoding="utf-8")

        rc = process.returncode if process.returncode is not None else -1
        current_file.write(f"\n{_EXIT_SENTINEL}:{rc}\n")
        current_file.close()
        state.stopped = True
        logger.debug(f"log_writer_loop({state.process_id}): finished, exit_code={rc}")


async def _get_shell_session(profile: str, working_dir: str) -> asyncio.subprocess.Process:
    """Return a persistent shell process for *profile*, creating one if needed."""
    proc = _shell_sessions.get(profile)
    if proc is not None and proc.returncode is None:
        return proc

    if _SYSTEM == "Windows":
        # Run a read-eval loop via -Command so PowerShell never echoes
        # commands or shows prompts.  Lines are buffered until the
        # sentinel ``__OPA_EXEC__`` triggers Invoke-Expression.
        loop_script = (
            '$OutputEncoding = [Console]::OutputEncoding = '
            '[System.Text.Encoding]::UTF8; '
            '$__buf = ""; '
            '$__opa_ok = $true; '
            'while ($true) { '
            '  $__line = [Console]::In.ReadLine(); '
            '  if ($__line -eq $null) { break }; '
            '  if ($__line -eq "__OPA_EXEC__") { '
            '    if ($__buf) { '
            '      try { Invoke-Expression $__buf; $__opa_ok = $? } '
            '      catch { [Console]::Error.WriteLine($_.Exception.Message); $__opa_ok = $false } '
            '    }; '
            '    $__buf = "" '
            '  } else { '
            '    if ($__buf) { $__buf += "`n" + $__line } '
            '    else { $__buf = $__line } '
            '  } '
            '}'
        )
        proc = await asyncio.create_subprocess_exec(
            _SHELL, "-NoLogo", "-NoProfile", "-Command", loop_script,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=working_dir,
        )
    else:
        proc = await asyncio.create_subprocess_exec(
            _SHELL,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=working_dir,
        )

    _shell_sessions[profile] = proc
    return proc


async def _check_stdin_blocked(process: asyncio.subprocess.Process) -> bool | None:
    """Check if the child process is blocked on stdin.

    Returns ``True`` (blocked), ``False`` (not blocked), or ``None``
    (inconclusive — e.g. on Windows).
    """
    return await is_child_blocked_on_read(process.pid, _SYSTEM)


async def _read_until_markers(
    process: asyncio.subprocess.Process,
    marker: str,
    overall_timeout: float = 120.0,
    silence_timeout: float = 3.0,
    long_running_timeout: float = 10.0,
) -> dict:
    """Read stdout/stderr until end-markers are found.

    Looks for ``{marker}:EXIT:{code}`` on stdout and ``{marker}:ERR_END``
    on stderr.

    **Three-category classification:**

    - **Markers found / process exited** → ``fire_and_forget``
    - **TUI escape sequences in output** → ``tui_fullscreen``
    - **Silence (stdin-blocked OR long silence)** → ``long_running``
    - **No markers after _MAX_CLASSIFICATION_TIME** → ``long_running``
      (catches commands that produce continuous output without completing)
    """
    stdout_buf = ""
    stderr_buf = ""
    exit_code: Optional[int] = None
    stdout_done = False
    stderr_done = False
    stderr_marker = f"{marker}:ERR_END"
    start = time.monotonic()
    last_data = start

    while not (stdout_done and stderr_done):
        elapsed = time.monotonic() - start
        if elapsed >= overall_timeout:
            # Markers were never found — the command did not complete.
            # Classify as long_running so the caller can spawn a standalone
            # process instead of leaving the persistent shell in a broken
            # state.
            logger.debug(
                f"_read_until_markers: overall_timeout ({overall_timeout}s) "
                f"reached without markers"
            )
            return {"stdout": stdout_buf, "stderr": stderr_buf,
                    "return_code": None, "completed": False,
                    "category": "long_running",
                    "waiting_for_input": False, "timed_out": True}

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
            # ---- No data received within this check interval ----
            if process.returncode is not None:
                return {"stdout": stdout_buf, "stderr": stderr_buf,
                        "return_code": process.returncode, "completed": True,
                        "category": "fire_and_forget"}

            silence_duration = time.monotonic() - last_data
            if silence_duration < silence_timeout:
                continue

            # --- Behaviour-based classification ---
            blocked = await _check_stdin_blocked(process)

            if blocked is True:
                # Process is waiting for user input → long_running.
                return {
                    "stdout": stdout_buf, "stderr": stderr_buf,
                    "return_code": None, "completed": False,
                    "category": "long_running",
                    "waiting_for_input": True,
                }

            if silence_duration >= long_running_timeout:
                # Daemon/server or inconclusive — all map to long_running.
                return {
                    "stdout": stdout_buf, "stderr": stderr_buf,
                    "return_code": None, "completed": False,
                    "category": "long_running",
                    "waiting_for_input": False,
                }

            # Not yet at long_running_timeout — keep waiting.
            continue

        # ---- Data received ----
        for t in done:
            data = t.result()
            if data == b"":
                if task_map[t] == "stdout":
                    stdout_done = True
                else:
                    stderr_done = True
                continue
            chunk = data.decode("utf-8", errors="replace")
            last_data = time.monotonic()

            if task_map[t] == "stdout":
                # Check for TUI escape sequences (behaviour-based).
                if detect_tui_sequences(chunk):
                    return {
                        "stdout": stdout_buf + chunk,
                        "stderr": stderr_buf,
                        "return_code": None, "completed": False,
                        "category": "tui_fullscreen",
                        "tui_detected": True,
                    }
                stdout_buf += chunk
                tag = f"{marker}:EXIT:"
                idx = stdout_buf.find(tag)
                if idx != -1:
                    after = stdout_buf[idx + len(tag):]
                    try:
                        exit_code = int(after.split("\n", 1)[0].strip())
                    except ValueError:
                        exit_code = 0
                    line_start = stdout_buf.rfind("\n", 0, idx)
                    stdout_buf = stdout_buf[:line_start + 1] if line_start != -1 else ""
                    stdout_done = True
            else:
                stderr_buf += chunk
                idx = stderr_buf.find(stderr_marker)
                if idx != -1:
                    line_start = stderr_buf.rfind("\n", 0, idx)
                    stderr_buf = stderr_buf[:line_start + 1] if line_start != -1 else ""
                    stderr_done = True

        # ---- Early long_running detection ----
        # If we've been reading output for longer than _MAX_CLASSIFICATION_TIME
        # without finding markers, the command is producing continuous output
        # and is clearly not a quick fire-and-forget.  This catches commands
        # like `uv run app.py` where the setup phase produces intermittent
        # output (preventing silence detection) for a long time.
        if elapsed >= _MAX_CLASSIFICATION_TIME and exit_code is None:
            logger.debug(
                f"_read_until_markers: max classification time "
                f"({_MAX_CLASSIFICATION_TIME}s) reached — classifying as "
                f"long_running"
            )
            return {
                "stdout": stdout_buf, "stderr": stderr_buf,
                "return_code": None, "completed": False,
                "category": "long_running",
                "waiting_for_input": False,
            }

    return {"stdout": stdout_buf, "stderr": stderr_buf,
            "return_code": exit_code if exit_code is not None else 0,
            "completed": True, "category": "fire_and_forget"}


SERVER_NAME = "Exec Shell"
SERVER_INSTRUCTIONS = (
    f"Execute command-line instructions on the terminal. Supports Linux, Windows, and macOS. "
    f"Current OS: {_SYSTEM}. Current shell: {_SHELL}."
)

class Var:
    """Variable keys for the Shell Executor tool (used in TOOL_CONFIG and runtime reads)."""
    LARGE_OUTPUT_MODE = "LARGE_OUTPUT_MODE"
    LARGE_OUTPUT_TOKEN_THRESHOLD = "LARGE_OUTPUT_TOKEN_THRESHOLD"
    LOG_SILENCE_THRESHOLD = "LOG_SILENCE_THRESHOLD"
    CLEANUP_TTL_HOURS = "CLEANUP_TTL_HOURS"


TOOL_CONFIG: ToolConfig = {
    "name": "exec_shell",
    "display_name": "Shell Executor",
    "default_model_group": "low",
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
}


class ExecShellTool(BuiltInTool):
    name: str = "exec_shell"
    description: str = (
        "Executes a shell command on the terminal and returns its output. "
        "Automatically detects the operating system and uses the appropriate shell "
        "(PowerShell on Windows, /bin/bash on Linux/macOS). "
        "For commands that complete quickly (fire-and-forget), returns the output directly. "
        "For long-running or interactive commands, returns a process_id for follow-up "
        "via exec_shell_output, exec_shell_input, or exec_shell_stop. "
        "TUI/full-screen applications are not supported."
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
        },
        "required": ["command"],
    }

    async def run(self, arguments: Dict[str, Any]) -> BuiltInToolResult:
        profile = (arguments.get("_profile") or "admin").strip() or "admin"
        command = arguments.get("command", "").strip()
        working_directory = (
            arguments.get("working_directory", None)
            or arguments.get("_working_directory", None)
            or BaseConfig.OPENPA_WORKING_DIR
        )
        timeout = arguments.get("timeout", 120)
        silence_timeout = arguments.get("silence_timeout", _DEFAULT_SILENCE_TIMEOUT)
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

        # Ensure the working directory exists
        if working_directory:
            os.makedirs(working_directory, exist_ok=True)
            if not os.path.isdir(working_directory):
                return BuiltInToolResult(
                    structured_content={
                        "error": "Invalid working directory",
                        "message": f"Directory does not exist: {working_directory}",
                    }
                )

        # Cleanup expired processes before starting a new one
        await _cleanup_stale_processes()

        logger.debug(f"exec_shell: running '{command}' on {_SYSTEM} with shell {_SHELL}")

        try:
            proc = await _get_shell_session(profile, working_directory)

            # Wrap the command with markers to delimit output
            marker = f"__opa_{_uuid.uuid4().hex[:12]}"
            if _SYSTEM == "Windows":
                # All lines are accumulated in $__buf by the read-eval loop
                # BEFORE execution.  Using a single __OPA_EXEC__ group ensures
                # the marker commands are never left in the stdin pipe where a
                # child process (e.g. Python's input()) could read them.
                wrapped = (
                    f"$__opa_err = $false; "
                    f"try {{ {command} }} catch {{ "
                    f"[Console]::Error.WriteLine($_.Exception.Message); "
                    f"$__opa_err = $true }}; "
                    f"$__opa_ok_inner = (-not $__opa_err) -and $?; "
                    f"$__opa_ec = $LASTEXITCODE; "
                    f"if ($null -eq $__opa_ec) {{ $__opa_ec = if ($__opa_ok_inner) {{ 0 }} else {{ 1 }} }}\n"
                    f"Write-Host ('{marker}:EXIT:' + $__opa_ec)\n"
                    f"[Console]::Error.WriteLine('{marker}:ERR_END')\n"
                    f"__OPA_EXEC__\n"
                )
            else:
                # Keep marker commands on the same line so bash reads the
                # entire sequence in one readline — prevents child processes
                # from consuming marker text via stdin.
                wrapped = (
                    f"{command}; __opa_ec=$?; "
                    f"echo '{marker}:EXIT:'$__opa_ec; "
                    f"echo '{marker}:ERR_END' >&2\n"
                )

            proc.stdin.write(wrapped.encode("utf-8"))
            await proc.stdin.drain()

            result = await _read_until_markers(
                proc, marker,
                overall_timeout=timeout,
                silence_timeout=silence_timeout,
                long_running_timeout=_DEFAULT_LONG_RUNNING_TIMEOUT,
            )

            category = result.get("category", "fire_and_forget")
            logger.debug(
                f"exec_shell: classification result for '{command}': "
                f"category={category}, completed={result.get('completed')}"
            )

            # --- FIRE_AND_FORGET: command completed normally ---
            if result["completed"]:
                return BuiltInToolResult(
                    structured_content={
                        "stdout": result["stdout"],
                        "stderr": result["stderr"],
                        "return_code": result["return_code"],
                        "category": "fire_and_forget",
                        "command": command,
                        "os": _SYSTEM,
                        "shell": _SHELL,
                        **({"timed_out": True} if result.get("timed_out") else {}),
                    }
                )

            # --- TUI_FULLSCREEN: not supported ---
            if category == "tui_fullscreen":
                await _interrupt_running_command(proc, marker, profile)
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
                        "os": _SYSTEM,
                        "shell": _SHELL,
                    }
                )

            # --- LONG_RUNNING: spawn standalone process with log writer ---
            # 1. Interrupt the persistent shell so it's usable for future commands
            logger.debug(f"exec_shell: interrupting persistent shell for '{command}'")
            await _interrupt_running_command(proc, marker, profile)
            logger.debug(f"exec_shell: persistent shell interrupted, spawning standalone process")

            # 2. Generate process_id and log directory
            process_id = _uuid.uuid4().hex[:8]
            log_dir = os.path.join(
                working_directory, "tools", "builtin", "exec_shell",
                "stdout", process_id,
            )
            os.makedirs(log_dir, exist_ok=True)

            # Classification-phase output is NOT written to log files because
            # the standalone process re-runs the command and reproduces its
            # own output.  Writing both would create duplicate log files.
            initial_stdout = result.get("stdout", "")
            initial_stderr = result.get("stderr", "")
            initial_file_num = 1

            # 4. Spawn standalone subprocess
            standalone_proc = await _spawn_long_running(command, working_directory)

            # 5. Start background log writer task
            writer_state = LogWriterState(
                process_id=process_id,
                log_dir=log_dir,
                current_file_number=initial_file_num,
                silence_threshold=log_silence_threshold,
            )
            writer_state.task = asyncio.create_task(
                _log_writer_loop(standalone_proc, writer_state)
            )

            # 6. Register in process registry
            expire_time = time.monotonic() + (cleanup_ttl_hours * 3600)
            _process_registry[process_id] = ProcessInfo(
                process=standalone_proc,
                created_at=time.monotonic(),
                working_dir=working_directory,
                command=command,
                log_dir=log_dir,
                log_writer_state=writer_state,
                expire_time=expire_time,
                is_long_running=True,
            )

            logger.info(
                f"exec_shell: long-running process {process_id} started "
                f"(command={command!r})"
            )
            return BuiltInToolResult(
                structured_content={
                    "process_id": process_id,
                    "stdout": initial_stdout,
                    "stderr": initial_stderr,
                    "status": "running",
                    "category": "long_running",
                    "waiting_for_input": result.get("waiting_for_input", False),
                    "command": command,
                    "os": _SYSTEM,
                    "shell": _SHELL,
                    "message": (
                        f"Long-running process started with id '{process_id}'. "
                        "Use exec_shell_output to read output, exec_shell_input "
                        "to send input, or exec_shell_stop to terminate."
                    ),
                }
            )

        except Exception as e:
            # If the session broke, discard it so the next call gets a fresh one
            _shell_sessions.pop(profile, None)
            return BuiltInToolResult(
                structured_content={
                    "error": "Execution error",
                    "message": f"Failed to execute command: {str(e)}",
                    "command": command,
                    "os": _SYSTEM,
                    "shell": _SHELL,
                }
            )


# ---------------------------------------------------------------------------
# Helpers for interactive process management
# ---------------------------------------------------------------------------

async def _read_until_blocked(
    process: asyncio.subprocess.Process,
    silence_timeout: float = _DEFAULT_SILENCE_TIMEOUT,
    overall_timeout: float = 120.0,
) -> dict:
    """Read stdout/stderr until the process exits or blocks for *silence_timeout* seconds.

    Returns a dict with keys: stdout, stderr, completed, return_code,
    waiting_for_input, and optionally timed_out.
    """
    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    start_time = time.monotonic()

    while True:
        elapsed = time.monotonic() - start_time
        if elapsed >= overall_timeout:
            process.kill()
            try:
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except (asyncio.TimeoutError, Exception):
                pass
            return {
                "stdout": "".join(stdout_parts),
                "stderr": "".join(stderr_parts),
                "completed": True,
                "return_code": process.returncode,
                "waiting_for_input": False,
                "timed_out": True,
            }

        # Build read tasks for live streams
        task_map: Dict[asyncio.Task, str] = {}
        if process.stdout:
            t = asyncio.ensure_future(process.stdout.read(4096))
            task_map[t] = "stdout"
        if process.stderr:
            t = asyncio.ensure_future(process.stderr.read(4096))
            task_map[t] = "stderr"

        if not task_map:
            # Both streams closed
            try:
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except (asyncio.TimeoutError, Exception):
                pass
            return {
                "stdout": "".join(stdout_parts),
                "stderr": "".join(stderr_parts),
                "completed": True,
                "return_code": process.returncode,
                "waiting_for_input": False,
            }

        done, pending = await asyncio.wait(
            task_map.keys(),
            timeout=silence_timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )

        # Cancel pending read tasks (safe — unread data stays in StreamReader buffer)
        for t in pending:
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

        if not done:
            # No output for silence_timeout seconds
            if process.returncode is not None:
                return {
                    "stdout": "".join(stdout_parts),
                    "stderr": "".join(stderr_parts),
                    "completed": True,
                    "return_code": process.returncode,
                    "waiting_for_input": False,
                }
            return {
                "stdout": "".join(stdout_parts),
                "stderr": "".join(stderr_parts),
                "completed": False,
                "return_code": None,
                "waiting_for_input": True,
            }

        process_exited = False
        for t in done:
            stream_name = task_map[t]
            data = t.result()
            if data == b"":
                process_exited = True
            else:
                decoded = data.decode("utf-8", errors="replace")
                if stream_name == "stdout":
                    stdout_parts.append(decoded)
                else:
                    stderr_parts.append(decoded)

        if process_exited:
            # Drain any remaining data from both streams
            if process.stdout:
                try:
                    remaining = await asyncio.wait_for(process.stdout.read(), timeout=2.0)
                    if remaining:
                        stdout_parts.append(remaining.decode("utf-8", errors="replace"))
                except (asyncio.TimeoutError, Exception):
                    pass
            if process.stderr:
                try:
                    remaining = await asyncio.wait_for(process.stderr.read(), timeout=2.0)
                    if remaining:
                        stderr_parts.append(remaining.decode("utf-8", errors="replace"))
                except (asyncio.TimeoutError, Exception):
                    pass
            try:
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except (asyncio.TimeoutError, Exception):
                pass
            return {
                "stdout": "".join(stdout_parts),
                "stderr": "".join(stderr_parts),
                "completed": True,
                "return_code": process.returncode,
                "waiting_for_input": False,
            }


async def _cleanup_stale_processes() -> int:
    """Kill and remove processes whose ``expire_time`` has passed.

    Also cleans up the log writer task and log directory for each expired
    process.  Returns the number of entries removed.
    """
    now = time.monotonic()
    stale_ids = [
        pid for pid, info in _process_registry.items()
        if now >= info.expire_time
    ]
    for pid in stale_ids:
        info = _process_registry.pop(pid, None)
        if not info:
            continue
        # Cancel log writer
        if info.log_writer_state and info.log_writer_state.task:
            info.log_writer_state.stopped = True
            info.log_writer_state.task.cancel()
            try:
                await info.log_writer_state.task
            except (asyncio.CancelledError, Exception):
                pass
        # Kill process
        if info.process.returncode is None:
            try:
                info.process.kill()
                await asyncio.wait_for(info.process.wait(), timeout=5.0)
            except (asyncio.TimeoutError, Exception):
                pass
        # Delete log directory
        if info.log_dir and os.path.isdir(info.log_dir):
            shutil.rmtree(info.log_dir, ignore_errors=True)
        logger.info(f"Cleaned up expired process {pid} ({info.command!r})")
    return len(stale_ids)


async def _interrupt_running_command(
    proc: asyncio.subprocess.Process,
    marker: str,
    profile: str,
) -> bool:
    """Send Ctrl+C to interrupt a long-running command in the persistent shell.

    Returns ``True`` if the shell session is still usable, ``False`` if it was
    discarded and will be recreated on the next call.
    """
    if _SYSTEM == "Windows":
        # Ctrl+C via stdin is unreliable with PowerShell's Invoke-Expression.
        # Discard the session; a fresh one will be created on the next call.
        _shell_sessions.pop(profile, None)
        try:
            proc.kill()
            # On Windows, proc.wait() can hang after kill() due to the
            # ProactorEventLoop not always delivering the exit notification.
            # Use a timeout to avoid blocking forever.
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except (asyncio.TimeoutError, Exception):
            pass
        return False

    # Unix (Linux / macOS): send SIGINT via the Ctrl+C character.
    for attempt in range(2):
        try:
            proc.stdin.write(b"\x03\n")
            await proc.stdin.drain()
        except Exception:
            _shell_sessions.pop(profile, None)
            return False

        # Wait briefly for the end marker to appear (shell recovered).
        try:
            buf = b""
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                if proc.stdout:
                    try:
                        chunk = await asyncio.wait_for(
                            proc.stdout.read(4096), timeout=remaining,
                        )
                        if chunk:
                            buf += chunk
                            if marker.encode() in buf:
                                logger.debug(
                                    f"exec_shell: shell recovered after Ctrl+C (attempt {attempt + 1})"
                                )
                                return True
                    except asyncio.TimeoutError:
                        break
                else:
                    break
        except Exception:
            break

    # Shell did not recover — discard the session.
    logger.warning("exec_shell: shell session did not recover after Ctrl+C; discarding")
    _shell_sessions.pop(profile, None)
    try:
        proc.kill()
        await asyncio.wait_for(proc.wait(), timeout=5.0)
    except (asyncio.TimeoutError, Exception):
        pass
    return False


# ---------------------------------------------------------------------------
# Interactive follow-up tools
# ---------------------------------------------------------------------------

class ExecShellInputTool(BuiltInTool):
    name: str = "exec_shell_input"
    description: str = (
        "Send input to a running process. Use this after exec_shell returns a "
        "process_id. Writes the input text to the process's stdin. "
        "For long-running processes the output is captured by the log writer — "
        "use exec_shell_output to read it. "
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
                    "The text to send to the process's stdin. "
                    "A newline is appended automatically."
                ),
            },
        },
        "required": ["process_id", "input_text"],
    }

    async def run(self, arguments: Dict[str, Any]) -> BuiltInToolResult:
        process_id = arguments.get("process_id", "").strip()
        input_text = arguments.get("input_text", "")

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

        # Check if the process already exited
        if process.returncode is not None:
            return BuiltInToolResult(
                structured_content={
                    "error": "Process already exited",
                    "message": (
                        f"Process '{process_id}' has already exited with "
                        f"code {process.returncode}. Use exec_shell_output "
                        "to read remaining output."
                    ),
                    "return_code": process.returncode,
                    "process_id": process_id,
                }
            )

        try:
            process.stdin.write((input_text + "\n").encode("utf-8"))
            await process.stdin.drain()
        except Exception as e:
            return BuiltInToolResult(
                structured_content={
                    "error": "Write error",
                    "message": f"Failed to write to process stdin: {str(e)}",
                    "process_id": process_id,
                }
            )

        # For long-running processes the log writer is the sole stdout reader.
        # Return immediately — the caller should use exec_shell_output to read.
        if info.is_long_running:
            return BuiltInToolResult(
                structured_content={
                    "process_id": process_id,
                    "input_sent": True,
                    "command": info.command,
                    "message": (
                        "Input sent. Use exec_shell_output to read the response."
                    ),
                }
            )

        # Non-long-running (should not happen in the new model, but kept for
        # safety): read directly until the process blocks again.
        try:
            result = await _read_until_blocked(
                process,
                silence_timeout=_DEFAULT_SILENCE_TIMEOUT,
                overall_timeout=120.0,
            )
        except Exception as e:
            return BuiltInToolResult(
                structured_content={
                    "error": "Read error",
                    "message": f"Failed to read process output: {str(e)}",
                    "process_id": process_id,
                }
            )

        if result["completed"]:
            _process_registry.pop(process_id, None)
            return BuiltInToolResult(
                structured_content={
                    "stdout": result["stdout"],
                    "stderr": result["stderr"],
                    "return_code": result["return_code"],
                    "completed": True,
                    "process_id": process_id,
                    "command": info.command,
                }
            )

        return BuiltInToolResult(
            structured_content={
                "process_id": process_id,
                "stdout": result["stdout"],
                "stderr": result["stderr"],
                "waiting_for_input": True,
                "completed": False,
                "command": info.command,
            }
        )


class ExecShellOutputTool(BuiltInTool):
    name: str = "exec_shell_output"
    description: str = (
        "Read stdout from a long-running process. Returns all new output since "
        "the last read and deletes consumed log files so subsequent calls only "
        "return fresh output. If the process has exited the response includes "
        "the exit code. "
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

        if not info.log_dir or not os.path.isdir(info.log_dir):
            return BuiltInToolResult(
                structured_content={
                    "process_id": process_id,
                    "output": "",
                    "completed": info.process.returncode is not None,
                    "return_code": info.process.returncode,
                    "command": info.command,
                    "message": "No log output available yet.",
                }
            )

        # 1. Collect and sort log files numerically
        log_files: List[str] = []
        for fname in os.listdir(info.log_dir):
            if fname.endswith(".log"):
                log_files.append(fname)
        log_files.sort(key=lambda f: int(f.split(".")[0]))

        # 2. Read and merge all log files
        merged_output = ""
        exit_code: Optional[int] = None
        completed = False
        read_paths: List[str] = []

        for fname in log_files:
            fpath = os.path.join(info.log_dir, fname)
            try:
                with open(fpath, "r", encoding="utf-8") as fh:
                    content = fh.read()
            except Exception:
                continue
            read_paths.append(fpath)

            # Check for exit sentinel
            sentinel_idx = content.find(_EXIT_SENTINEL + ":")
            if sentinel_idx != -1:
                completed = True
                after = content[sentinel_idx + len(_EXIT_SENTINEL) + 1:]
                try:
                    exit_code = int(after.split("\n", 1)[0].strip())
                except ValueError:
                    exit_code = -1
                # Strip the sentinel line from output
                content = content[:sentinel_idx].rstrip("\n")

            merged_output += content

        # 3. Delete all read log files
        for fpath in read_paths:
            try:
                os.remove(fpath)
            except Exception:
                pass

        # 4. Rename any new files created during reading to start from 1
        remaining_files: List[str] = []
        if os.path.isdir(info.log_dir):
            for fname in os.listdir(info.log_dir):
                if fname.endswith(".log"):
                    remaining_files.append(fname)
        remaining_files.sort(key=lambda f: int(f.split(".")[0]))

        for new_num, fname in enumerate(remaining_files, start=1):
            expected = f"{new_num}.log"
            if fname != expected:
                try:
                    os.rename(
                        os.path.join(info.log_dir, fname),
                        os.path.join(info.log_dir, expected),
                    )
                except Exception:
                    pass

        # Update the log writer's file counter to follow the renaming
        if info.log_writer_state and remaining_files:
            info.log_writer_state.current_file_number = len(remaining_files) + 1
        elif info.log_writer_state:
            info.log_writer_state.current_file_number = 1

        # 5. Refresh expire_time
        cleanup_ttl_hours = float(
            variables.get(Var.CLEANUP_TTL_HOURS) or _DEFAULT_CLEANUP_TTL_HOURS
        )
        info.expire_time = time.monotonic() + (cleanup_ttl_hours * 3600)

        # 6. If process exited but we didn't see sentinel, check returncode
        if not completed and info.process.returncode is not None:
            completed = True
            exit_code = info.process.returncode

        # 7. Token counting for large output handling
        total_tokens = 0
        truncated = False
        if merged_output:
            try:
                from tiktoken import encoding_for_model
                encoder = encoding_for_model("gpt-4o")
                total_tokens = len(encoder.encode(merged_output))
            except Exception:
                # Rough fallback: ~4 chars per token
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
                "output": merged_output,
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
        "Sends SIGTERM, waits briefly, then SIGKILL if needed. "
        "Returns any final output from log files and the return code. "
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

        # Cancel log writer first so it stops reading from the process
        if info.log_writer_state and info.log_writer_state.task:
            info.log_writer_state.stopped = True
            info.log_writer_state.task.cancel()
            try:
                await info.log_writer_state.task
            except (asyncio.CancelledError, Exception):
                pass

        # Close stdin to unblock any pending reads on the child side
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

        # Read remaining output from log files
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
                        content = fh.read()
                    # Strip exit sentinel from display
                    sentinel_idx = content.find(_EXIT_SENTINEL + ":")
                    if sentinel_idx != -1:
                        content = content[:sentinel_idx].rstrip("\n")
                    final_output += content
                except Exception:
                    pass
            # Clean up log directory
            shutil.rmtree(info.log_dir, ignore_errors=True)

        return BuiltInToolResult(
            structured_content={
                "output": final_output,
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
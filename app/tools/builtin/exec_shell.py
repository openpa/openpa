"""Exec Shell built-in tool.

Executes shell commands on the terminal. Detects the operating system and
uses the appropriate shell (PowerShell on Windows, /bin/bash on Linux/macOS).

Classification is **behaviour-based** — the tool observes what the process
actually does at runtime rather than guessing from the command name:

- Process exits on its own                  → fire-and-forget  (Cat 1)
- Silence + blocked on stdin                → waiting for input (Cat 2 / 5)
- Silence + NOT blocked on stdin for >10 s  → long-running      (Cat 3)
- TUI escape sequences in output            → full-screen app   (Cat 4)

Categories 1, 2, 5, and 6 are supported.  Categories 3 and 4 are detected
at runtime, interrupted, and reported as unsupported.
"""

import asyncio
import os
import platform
import time
import uuid as _uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from app.config.settings import BaseConfig
from app.tools.builtin.base import BuiltInTool, BuiltInToolResult
from app.types import ToolConfig
from app.tools.builtin.exec_shell_classifier import (
    CommandCategory,
    UNSUPPORTED_CATEGORIES,
    detect_tui_sequences,
)
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
# Interactive process registry
# ---------------------------------------------------------------------------

@dataclass
class ProcessInfo:
    """Tracks a running interactive process."""
    process: asyncio.subprocess.Process
    created_at: float
    working_dir: str
    command: str


_process_registry: Dict[str, ProcessInfo] = {}
_PROCESS_TTL = 600  # 10 min TTL for orphaned processes
_DEFAULT_SILENCE_TIMEOUT = 3.0  # seconds of silence → trigger introspection check
_DEFAULT_LONG_RUNNING_TIMEOUT = 10.0  # seconds of continuous silence (not stdin-blocked) → long-running

# Per-profile persistent shell sessions
_shell_sessions: Dict[str, asyncio.subprocess.Process] = {}


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
            'while ($true) { '
            '  $__line = [Console]::In.ReadLine(); '
            '  if ($__line -eq $null) { break }; '
            '  if ($__line -eq "__OPA_EXEC__") { '
            '    if ($__buf) { '
            '      try { Invoke-Expression $__buf } '
            '      catch { [Console]::Error.WriteLine($_.Exception.Message) } '
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

    **Behaviour-based classification** — instead of relying on command
    names, the function observes the process's actual behaviour:

    - **Silence + stdin-blocked** → ``waiting_for_input`` (returned
      immediately on the first silence check).
    - **Silence + NOT stdin-blocked, accumulated silence < long_running_timeout**
      → keep waiting (the process may resume output).
    - **Silence + NOT stdin-blocked, accumulated silence ≥ long_running_timeout**
      → ``long_running`` (the process is likely a daemon / server).
    - **TUI escape sequences in output** → ``tui_detected``.
    - **Markers found** → ``completed``.
    - **overall_timeout exceeded** → ``timed_out``.
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
            return {"stdout": stdout_buf, "stderr": stderr_buf,
                    "return_code": exit_code, "completed": True,
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
                        "waiting_for_input": False}

            silence_duration = time.monotonic() - last_data
            if silence_duration < silence_timeout:
                # Haven't been silent long enough for a check yet.
                continue

            # --- Behaviour-based classification ---
            blocked = await _check_stdin_blocked(process)

            if blocked is True:
                # Definitively waiting for user input.
                return {
                    "stdout": stdout_buf, "stderr": stderr_buf,
                    "return_code": None, "completed": False,
                    "waiting_for_input": True,
                    "detected_category": "waiting_for_input",
                }

            if silence_duration >= long_running_timeout:
                if blocked is False:
                    # Confirmed not waiting on stdin → daemon / server.
                    return {
                        "stdout": stdout_buf, "stderr": stderr_buf,
                        "return_code": None, "completed": False,
                        "waiting_for_input": False,
                        "long_running": True,
                        "detected_category": "long_running",
                    }
                # blocked is None (inconclusive, e.g. Windows).
                # After long_running_timeout of silence we still can't
                # tell — default to waiting_for_input for safety.
                return {
                    "stdout": stdout_buf, "stderr": stderr_buf,
                    "return_code": None, "completed": False,
                    "waiting_for_input": True,
                    "detected_category": "waiting_for_input",
                    "classification_uncertain": True,
                }

            # Not yet at long_running_timeout — keep waiting.
            # The process may resume output (e.g. a build step that
            # pauses briefly between phases).
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
                        "waiting_for_input": False,
                        "tui_detected": True,
                        "detected_category": "tui_fullscreen",
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

    return {"stdout": stdout_buf, "stderr": stderr_buf,
            "return_code": exit_code if exit_code is not None else 0,
            "completed": True, "waiting_for_input": False}


SERVER_NAME = "Exec Shell"
SERVER_INSTRUCTIONS = (
    f"Execute command-line instructions on the terminal. Supports Linux, Windows, and macOS. "
    f"Current OS: {_SYSTEM}. Current shell: {_SHELL}."
)

TOOL_CONFIG: ToolConfig = {
    "name": "exec_shell",
    "display_name": "Shell Executor",
    "default_model_group": "low",
}


class ExecShellTool(BuiltInTool):
    name: str = "exec_shell"
    description: str = (
        "Executes a shell command on the terminal and returns its output. "
        "Automatically detects the operating system and uses the appropriate shell "
        "(PowerShell on Windows, /bin/bash on Linux/macOS). "
        "E.g. 'please generate public SSH keys'"
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
                "description": "Maximum time in seconds to wait for the command to complete. Defaults to 120.",
            },
            "silence_timeout": {
                "type": "number",
                "description": (
                    "Seconds of output silence before checking whether the "
                    "process is waiting for input. Default: 3."
                ),
            },
            "long_running_timeout": {
                "type": "number",
                "description": (
                    "Seconds of continuous silence (while the process is NOT "
                    "blocked on stdin) before classifying as a long-running "
                    "process and interrupting. Default: 10."
                ),
            },
            "category": {
                "type": "string",
                "enum": [c.value for c in CommandCategory if c != CommandCategory.UNKNOWN],
                "description": (
                    "Optional hint — explicitly specify the expected command "
                    "category. If set to tui_fullscreen or long_running the "
                    "command will be blocked before execution."
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
        long_running_timeout = arguments.get("long_running_timeout", _DEFAULT_LONG_RUNNING_TIMEOUT)
        caller_category = arguments.get("category")

        if not command:
            return BuiltInToolResult(
                structured_content={
                    "error": "Missing parameter",
                    "message": "The 'command' parameter is required.",
                }
            )

        # Ensure the profile directory exists, then validate
        if working_directory:
            os.makedirs(working_directory, exist_ok=True)
            if not os.path.isdir(working_directory):
                return BuiltInToolResult(
                    structured_content={
                        "error": "Invalid working directory",
                        "message": f"Directory does not exist: {working_directory}",
                    }
                )

        # -----------------------------------------------------------------
        # Optional caller-provided category hint
        # -----------------------------------------------------------------
        if caller_category:
            try:
                cat = CommandCategory(caller_category)
            except ValueError:
                cat = CommandCategory.UNKNOWN
            if cat in UNSUPPORTED_CATEGORIES:
                return BuiltInToolResult(
                    structured_content={
                        "error": "Unsupported command type",
                        "unsupported": True,
                        "category": cat.value,
                        "message": (
                            f"The caller marked this command as '{cat.value}' which is "
                            "not supported by exec_shell."
                        ),
                        "command": command,
                        "os": _SYSTEM,
                        "shell": _SHELL,
                    }
                )

        # Cleanup stale processes before starting a new one
        await _cleanup_stale_processes()

        logger.debug(f"exec_shell: running '{command}' on {_SYSTEM} with shell {_SHELL}")

        try:
            proc = await _get_shell_session(profile, working_directory)

            # Wrap the command with markers to delimit output
            marker = f"__opa_{_uuid.uuid4().hex[:12]}"
            if _SYSTEM == "Windows":
                wrapped = (
                    f"{command}\n"
                    f"$__opa_ec = $LASTEXITCODE; "
                    f"if ($null -eq $__opa_ec) {{ $__opa_ec = if ($?) {{ 0 }} else {{ 1 }} }}\n"
                    f"Write-Host ('{marker}:EXIT:' + $__opa_ec)\n"
                    f"[Console]::Error.WriteLine('{marker}:ERR_END')\n"
                    f"__OPA_EXEC__\n"
                )
            else:
                wrapped = (
                    f"{command}\n"
                    f"__opa_ec=$?\n"
                    f"echo '{marker}:EXIT:'$__opa_ec\n"
                    f"echo '{marker}:ERR_END' >&2\n"
                )

            proc.stdin.write(wrapped.encode("utf-8"))
            await proc.stdin.drain()

            result = await _read_until_markers(
                proc, marker,
                overall_timeout=timeout,
                silence_timeout=silence_timeout,
                long_running_timeout=long_running_timeout,
            )

            detected_cat = result.get("detected_category", "fire_and_forget")

            # --- Command completed normally (fire-and-forget) ---
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

            # --- TUI detected at runtime via escape sequences ---
            if result.get("tui_detected"):
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
                            "It has been interrupted."
                        ),
                        "command": command,
                        "os": _SYSTEM,
                        "shell": _SHELL,
                    }
                )

            # --- Long-running process detected at runtime ---
            if result.get("long_running"):
                await _interrupt_running_command(proc, marker, profile)
                return BuiltInToolResult(
                    structured_content={
                        "error": "Long-running command detected",
                        "unsupported": True,
                        "category": "long_running",
                        "stdout": result["stdout"],
                        "stderr": result["stderr"],
                        "interrupted": True,
                        "message": (
                            f"The command '{command}' has been silent for "
                            f"{long_running_timeout}+ seconds and is not waiting "
                            "for input — it appears to be a long-running process "
                            "(daemon/server). It has been interrupted."
                        ),
                        "suggested_alternative": (
                            "Run in a dedicated terminal, or use "
                            "'nohup <command> &' to background it."
                        ),
                        "command": command,
                        "os": _SYSTEM,
                        "shell": _SHELL,
                    }
                )

            # --- Process is waiting for input ---
            process_id = _uuid.uuid4().hex[:8]
            _process_registry[process_id] = ProcessInfo(
                process=proc,
                created_at=time.monotonic(),
                working_dir=working_directory,
                command=command,
            )
            logger.info(
                f"exec_shell: process {process_id} waiting for input "
                f"(command={command!r})"
            )
            return BuiltInToolResult(
                structured_content={
                    "process_id": process_id,
                    "stdout": result["stdout"],
                    "stderr": result["stderr"],
                    "waiting_for_input": True,
                    "completed": False,
                    "category": detected_cat,
                    "command": command,
                    "os": _SYSTEM,
                    "shell": _SHELL,
                    **({"classification_uncertain": True} if result.get("classification_uncertain") else {}),
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
            await process.wait()
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
            await process.wait()
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
            await process.wait()
            return {
                "stdout": "".join(stdout_parts),
                "stderr": "".join(stderr_parts),
                "completed": True,
                "return_code": process.returncode,
                "waiting_for_input": False,
            }


async def _cleanup_stale_processes() -> int:
    """Kill and remove processes older than *_PROCESS_TTL*. Returns count removed."""
    now = time.monotonic()
    stale_ids = [
        pid for pid, info in _process_registry.items()
        if now - info.created_at > _PROCESS_TTL
    ]
    for pid in stale_ids:
        info = _process_registry.pop(pid, None)
        if info and info.process.returncode is None:
            try:
                info.process.kill()
                await info.process.wait()
            except Exception:
                pass
            logger.info(f"Cleaned up stale interactive process {pid} ({info.command!r})")
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
            await proc.wait()
        except Exception:
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
        await proc.wait()
    except Exception:
        pass
    return False


# ---------------------------------------------------------------------------
# Interactive follow-up tools
# ---------------------------------------------------------------------------

class ExecShellInputTool(BuiltInTool):
    name: str = "exec_shell_input"
    description: str = (
        "Send input to a running interactive process. Use this after exec_shell "
        "returns waiting_for_input=true and a process_id. "
        "Writes the input text to the process's stdin, then reads output until "
        "the process blocks again or exits."
        "Returns the new output and whether the process is still waiting for input. "
        "E.g. input: <text_input>\nprocess_id: <abc123>"
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
            "silence_timeout": {
                "type": "number",
                "description": (
                    "Seconds of silence before assuming the process is waiting "
                    "for more input. Default: 3."
                ),
            },
        },
        "required": ["process_id", "input_text"],
    }

    async def run(self, arguments: Dict[str, Any]) -> BuiltInToolResult:
        process_id = arguments.get("process_id", "").strip()
        input_text = arguments.get("input_text", "")
        silence_timeout = arguments.get("silence_timeout", _DEFAULT_SILENCE_TIMEOUT)

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
            _process_registry.pop(process_id, None)
            return BuiltInToolResult(
                structured_content={
                    "error": "Process already exited",
                    "message": f"Process '{process_id}' has already exited with code {process.returncode}.",
                    "return_code": process.returncode,
                }
            )

        try:
            # Write input to stdin
            process.stdin.write((input_text + "\n").encode("utf-8"))
            await process.stdin.drain()
        except Exception as e:
            _process_registry.pop(process_id, None)
            return BuiltInToolResult(
                structured_content={
                    "error": "Write error",
                    "message": f"Failed to write to process stdin: {str(e)}",
                    "process_id": process_id,
                }
            )

        try:
            result = await _read_until_blocked(
                process,
                silence_timeout=silence_timeout,
                overall_timeout=120.0,
            )
        except Exception as e:
            _process_registry.pop(process_id, None)
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


class ExecShellStopTool(BuiltInTool):
    name: str = "exec_shell_stop"
    description: str = (
        "Stop a running interactive process by process_id. "
        "Sends SIGTERM, waits briefly, then SIGKILL if needed."
        "Returns any final output and the return code if available."
        "E.g. 'stop process_id=<abc123>'. "
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
        stdout_final = ""
        stderr_final = ""

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
                    await process.wait()
            except Exception:
                pass

        # Drain remaining output
        if process.stdout:
            try:
                remaining = await asyncio.wait_for(process.stdout.read(), timeout=2.0)
                if remaining:
                    stdout_final = remaining.decode("utf-8", errors="replace")
            except (asyncio.TimeoutError, Exception):
                pass
        if process.stderr:
            try:
                remaining = await asyncio.wait_for(process.stderr.read(), timeout=2.0)
                if remaining:
                    stderr_final = remaining.decode("utf-8", errors="replace")
            except (asyncio.TimeoutError, Exception):
                pass

        return BuiltInToolResult(
            structured_content={
                "stdout": stdout_final,
                "stderr": stderr_final,
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
        ExecShellStopTool(),
    ]
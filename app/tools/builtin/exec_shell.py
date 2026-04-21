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
import os
import platform
import shutil
import time
import uuid as _uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from app.config.settings import BaseConfig
from app.tools.builtin.base import BuiltInTool, BuiltInToolResult
from app.types import ToolConfig
from app.tools.builtin.exec_shell_classifier import detect_tui_sequences
from app.tools.builtin.exec_shell_process_inspect import is_child_blocked_on_read
from app.utils.logger import logger

_SYSTEM = platform.system()  # "Windows", "Linux", "Darwin"

if _SYSTEM == "Windows":
    _SHELL = "powershell.exe"
    _SHELL_FLAG = "-Command"
else:
    _SHELL = "/bin/bash"
    _SHELL_FLAG = "-c"


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
    process: asyncio.subprocess.Process
    created_at: float
    working_dir: str
    command: str
    log_dir: str = ""
    log_writer_state: Optional[LogWriterState] = None
    expire_time: float = 0.0
    is_long_running: bool = False


_process_registry: Dict[str, ProcessInfo] = {}
_DEFAULT_SILENCE_TIMEOUT = 3.0
_DEFAULT_LONG_RUNNING_TIMEOUT = 10.0
_MAX_CLASSIFICATION_TIME = 30.0
_DEFAULT_CLEANUP_TTL_HOURS = 24
_EXIT_SENTINEL = "__OPA_EXIT__"

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
            f"Execute command-line instructions on the terminal. Supports Linux, Windows, and macOS. "
            f"Current OS: {_SYSTEM}. Current shell: {_SHELL}."
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
}


async def _spawn_command(command: str, working_dir: str) -> asyncio.subprocess.Process:
    """Spawn a command as a standalone subprocess with piped stdin/stdout/stderr."""
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


async def _check_stdin_blocked(process: asyncio.subprocess.Process) -> bool | None:
    """True if the child process is blocked on stdin; None if inconclusive."""
    return await is_child_blocked_on_read(process.pid, _SYSTEM)


# ---------------------------------------------------------------------------
# Classification (pre-long-running): read initial output and decide category
# ---------------------------------------------------------------------------

async def _classify_stream(
    process: asyncio.subprocess.Process,
    overall_timeout: float = 120.0,
    silence_timeout: float = _DEFAULT_SILENCE_TIMEOUT,
    long_running_timeout: float = _DEFAULT_LONG_RUNNING_TIMEOUT,
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
                if detect_tui_sequences(chunk):
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

        # Sentinel rule: sentinel always lives in its own file.  Close the
        # current file (if any), then open the NEXT number and write the
        # sentinel into it alone.
        try:
            async with state.rotate_lock:
                if state.current_file is not None:
                    state.current_file.close()
                    state.current_file = None
                    state.current_file_number += 1
                rc = process.returncode if process.returncode is not None else -1
                sentinel_path = os.path.join(
                    state.log_dir, f"{state.current_file_number}.log",
                )
                with open(sentinel_path, "a", encoding="utf-8") as fh:
                    fh.write(f"{_EXIT_SENTINEL}:{rc}\n")
                state.current_file_number += 1
        except Exception as exc:
            logger.error(f"log_writer_loop({state.process_id}): sentinel write failed: {exc}")

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
        if info.log_dir and os.path.isdir(info.log_dir):
            shutil.rmtree(info.log_dir, ignore_errors=True)
        logger.info(f"Cleaned up expired process {pid} ({info.command!r})")
    return len(stale_ids)


class ExecShellTool(BuiltInTool):
    name: str = "exec_shell"
    description: str = (
        "Executes a shell command on the terminal and returns its output. "
        "Automatically detects the operating system and uses the appropriate shell\n"
        "E.g, 'check disk usage' or 'ping google.com'"
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

        logger.debug(f"exec_shell: running '{command}' on {_SYSTEM} with shell {_SHELL}")

        try:
            proc = await _spawn_command(command, working_directory)
        except Exception as e:
            return BuiltInToolResult(
                structured_content={
                    "error": "Spawn error",
                    "message": f"Failed to spawn command: {str(e)}",
                    "command": command,
                    "os": _SYSTEM,
                    "shell": _SHELL,
                }
            )

        try:
            result = await _classify_stream(
                proc,
                overall_timeout=timeout,
                silence_timeout=silence_timeout,
                long_running_timeout=_DEFAULT_LONG_RUNNING_TIMEOUT,
            )

            category = result.get("category", "fire_and_forget")
            logger.debug(
                f"exec_shell: classification result for '{command}': "
                f"category={category}, completed={result.get('completed')}"
            )

            if result["completed"]:
                # Drain any remaining output the classifier might have missed
                # (rare, but happens if EOF landed in the last read).
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

            if category == "tui_fullscreen":
                try:
                    proc.kill()
                    await asyncio.wait_for(proc.wait(), timeout=5.0)
                except (asyncio.TimeoutError, Exception):
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
                        "os": _SYSTEM,
                        "shell": _SHELL,
                    }
                )

            # --- LONG_RUNNING: keep subprocess alive, start log writer ---
            process_id = _uuid.uuid4().hex[:8]
            log_dir = os.path.join(
                working_directory, "tools", "builtin", "exec_shell",
                "stdout", process_id,
            )
            os.makedirs(log_dir, exist_ok=True)

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
            )

            logger.info(
                f"exec_shell: long-running process {process_id} started "
                f"(command={command!r})"
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
                    "os": _SYSTEM,
                    "shell": _SHELL,
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
            return BuiltInToolResult(
                structured_content={
                    "error": "Execution error",
                    "message": f"Failed to execute command: {str(e)}",
                    "command": command,
                    "os": _SYSTEM,
                    "shell": _SHELL,
                }
            )


class ExecShellInputTool(BuiltInTool):
    name: str = "exec_shell_input"
    description: str = (
        "Send input to a running process. Use this after exec_shell returns a "
        "process_id. Writes the input text to the process's stdin. "
        "Use exec shell output to read the response.\n"
        'E.g., Send input to process "708e9873": {"process_id":"708e9873","input_text":"my input"}\n'
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

            sentinel_idx = content.find(_EXIT_SENTINEL + ":")
            if sentinel_idx != -1:
                completed = True
                after = content[sentinel_idx + len(_EXIT_SENTINEL) + 1:]
                try:
                    exit_code = int(after.split("\n", 1)[0].strip())
                except ValueError:
                    exit_code = -1
                content = content[:sentinel_idx].rstrip("\n")

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
                    sentinel_idx = content.find(_EXIT_SENTINEL + ":")
                    if sentinel_idx != -1:
                        content = content[:sentinel_idx].rstrip("\n")
                    final_output += content
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

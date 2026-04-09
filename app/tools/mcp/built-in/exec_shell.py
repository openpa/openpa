"""Exec Shell MCP server using stdio transport.

A standalone FastMCP server that executes shell commands on the terminal.
Detects the operating system and uses the appropriate shell (PowerShell on Windows,
/bin/bash on Linux/macOS).

Usage:
    python app/tools/mcp/built-in/exec_shell.py
"""

import asyncio
import os
import platform
import sys
from typing import Any, Dict

from fastmcp import FastMCP
from fastmcp.tools.tool import Tool, ToolResult

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

# Initialize FastMCP server
mcp = FastMCP(
    name="Exec Shell",
    instructions=f"Execute command-line instructions on the terminal. Supports Linux, Windows, and macOS. "
    f"Current OS: {_SYSTEM}. "
    f"Current shell: {_SHELL}. "
)


class ExecShellTool(Tool):
    name: str = "exec_shell"
    description: str = (
        "Executes a shell command on the terminal and returns its output. "
        "Automatically detects the operating system and uses the appropriate shell "
        "(PowerShell on Windows, /bin/bash on Linux/macOS)."
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
                "description": "The working directory to run the command in. Defaults to the current directory.",
            },
            "timeout": {
                "type": "integer",
                "description": "Maximum time in seconds to wait for the command to complete. Defaults to 120.",
            },
        },
        "required": ["command"],
    }

    async def run(self, arguments: Dict[str, Any]) -> ToolResult:
        command = arguments.get("command", "").strip()
        working_directory = arguments.get("working_directory", None)
        timeout = arguments.get("timeout", 120)

        if not command:
            return ToolResult(
                structured_content={
                    "error": "Missing parameter",
                    "message": "The 'command' parameter is required.",
                }
            )

        # Validate working directory if provided
        if working_directory:
            if not os.path.isdir(working_directory):
                return ToolResult(
                    structured_content={
                        "error": "Invalid working directory",
                        "message": f"Directory does not exist: {working_directory}",
                    }
                )

        logger.debug(f"exec_shell: running '{command}' on {_SYSTEM} with shell {_SHELL}")

        try:
            process = await asyncio.create_subprocess_exec(
                _SHELL,
                _SHELL_FLAG,
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=working_directory,
            )

            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    process.communicate(), timeout=timeout
                )
            except asyncio.TimeoutError:
                process.kill()
                await process.communicate()
                return ToolResult(
                    structured_content={
                        "error": "Timeout",
                        "message": f"Command timed out after {timeout} seconds.",
                        "command": command,
                        "os": _SYSTEM,
                        "shell": _SHELL,
                    }
                )

            stdout = stdout_bytes.decode("utf-8", errors="replace")
            stderr = stderr_bytes.decode("utf-8", errors="replace")

            return ToolResult(
                structured_content={
                    "stdout": stdout,
                    "stderr": stderr,
                    "return_code": process.returncode,
                    "command": command,
                    "os": _SYSTEM,
                    "shell": _SHELL,
                }
            )

        except Exception as e:
            return ToolResult(
                structured_content={
                    "error": "Execution error",
                    "message": f"Failed to execute command: {str(e)}",
                    "command": command,
                    "os": _SYSTEM,
                    "shell": _SHELL,
                }
            )


mcp.add_tool(ExecShellTool())


if __name__ == "__main__":
    sys.stderr.write("Starting Exec Shell MCP Server with stdio transport\n")
    mcp.run(transport="stdio")

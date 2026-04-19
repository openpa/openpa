"""Sleep built-in tool.

Pauses execution for a specified number of seconds.
"""

import asyncio
from typing import Any, Dict

from app.tools.builtin.base import BuiltInTool, BuiltInToolResult
from app.types import ToolConfig


SERVER_NAME = "Sleep"
SERVER_INSTRUCTIONS = "Pause execution for a specified number of seconds."

TOOL_CONFIG: ToolConfig = {
    "name": "sleep",
    "display_name": "Sleep",
    "default_model_group": "low",
}

MAX_SLEEP = 300  # 5 minutes


class SleepTool(BuiltInTool):
    name: str = "sleep"
    description: str = "Pause execution for a specified number of seconds."
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "seconds": {
                "type": "number",
                "description": "Number of seconds to sleep.",
            }
        },
        "required": ["seconds"],
        "additionalProperties": False,
    }

    async def run(self, arguments: Dict[str, Any]) -> BuiltInToolResult:
        seconds = arguments.get("seconds", 0)

        if not isinstance(seconds, (int, float)) or seconds < 0:
            return BuiltInToolResult(
                content=[{"type": "text", "text": "seconds must be a non-negative number"}]
            )

        if seconds > MAX_SLEEP:
            return BuiltInToolResult(
                content=[{"type": "text", "text": f"seconds exceeds maximum of {MAX_SLEEP}"}]
            )

        await asyncio.sleep(seconds)

        if seconds == int(seconds):
            label = f"{int(seconds)}s"
        else:
            label = f"{seconds:.1f}s"

        return BuiltInToolResult(
            content=[{"type": "text", "text": f"slept {label}"}]
        )


def get_tools(config: dict) -> list[BuiltInTool]:
    """Return tool instances for this server."""
    return [SleepTool()]

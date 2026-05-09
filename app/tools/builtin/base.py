"""Base classes for built-in tools.

Provides lightweight replacements for FastMCP's Tool and ToolResult so that
built-in tools can run directly in-process without MCP stdio transport.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class BuiltInToolResult:
    """Result from a built-in tool execution.

    Mirrors FastMCP's ToolResult interface with the same two primary fields
    so that the BuiltInToolAdapter's _extract_tool_result() works identically.

    Attributes:
        content: Optional list of content items (dicts with 'type'/'text'/'data' keys).
        structured_content: Optional dict returned as structured JSON data.
    """
    content: Optional[List[Any]] = None
    structured_content: Optional[Dict[str, Any]] = None


class BuiltInTool:
    """Base class for built-in tools (replaces fastmcp.tools.tool.Tool).

    Subclasses must set ``name``, ``description``, ``parameters`` (JSON Schema)
    and implement ``async run(arguments) -> BuiltInToolResult``.
    """
    name: str = ""
    description: str = ""
    parameters: Dict[str, Any] = {}

    async def run(self, arguments: Dict[str, Any]) -> BuiltInToolResult:
        """Execute the tool with the given arguments.

        Args:
            arguments: Tool arguments as validated by the JSON Schema in ``parameters``.

        Returns:
            BuiltInToolResult with either ``content`` or ``structured_content`` populated.
        """
        raise NotImplementedError

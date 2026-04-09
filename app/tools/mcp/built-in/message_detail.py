"""Message Detail MCP server using stdio transport.

A standalone FastMCP server that retrieves full details of conversation messages
by their IDs. This allows the LLM to read complete message content (beyond the
truncated 500-token preview in history), as well as internal structure such as
message parts, reasoning/thinking steps, and metadata.

Usage:
    python app/tools/mcp/built-in/message_detail.py

Environment:
    SQLITE_DB_PATH - Path to the SQLite database (default: .storage/openpa.db)
"""

import json
import os
import sqlite3
import sys
from typing import Any, Dict

from fastmcp import FastMCP
from fastmcp.tools.tool import Tool, ToolResult

SQLITE_DB_PATH = os.environ.get("SQLITE_DB_PATH", ".storage/openpa.db")

mcp = FastMCP(
    name="Message Detail",
    instructions=(
        "Retrieve the full details of conversation messages by their IDs."
    ),
)


def _parse_json_column(value: Any) -> Any:
    """Parse a JSON column value that may be a string or already decoded."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, ValueError):
            return value
    return value


class GetMessageDetailTool(Tool):
    name: str = "get_message_detail"
    description: str = (
        "This tool is used to extract the full details of a message, including message parts, thinking steps, and metadata. "
        "E.g. 'get details for <message_id>', 'show details steps reasoning for <message_id>'."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "message_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of message IDs (UUIDs) to retrieve.",
            }
        },
        "required": ["message_ids"],
        "additionalProperties": False,
    }

    async def run(self, arguments: Dict[str, Any]) -> ToolResult:
        message_ids = arguments.get("message_ids", [])
        if not message_ids:
            return ToolResult(
                structured_content={"error": "No message IDs provided."}
            )

        if not os.path.isfile(SQLITE_DB_PATH):
            return ToolResult(
                structured_content={
                    "error": f"Database file not found: {SQLITE_DB_PATH}"
                }
            )

        try:
            conn = sqlite3.connect(SQLITE_DB_PATH)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            placeholders = ",".join("?" * len(message_ids))
            cursor.execute(
                f"SELECT id, role, content, parts, thinking_steps, "
                f"metadata, created_at, ordering "
                f"FROM messages WHERE id IN ({placeholders})",
                message_ids,
            )

            rows = cursor.fetchall()
            conn.close()

            messages = []
            for row in rows:
                msg: Dict[str, Any] = {
                    "id": row["id"],
                    "role": row["role"],
                    "content": row["content"],
                    "created_at": row["created_at"],
                    "ordering": row["ordering"],
                }
                for json_col in ("parts", "thinking_steps", "metadata"):
                    parsed = _parse_json_column(row[json_col])
                    if parsed is not None:
                        msg[json_col] = parsed
                messages.append(msg)

            # Preserve the requested order
            id_order = {mid: i for i, mid in enumerate(message_ids)}
            messages.sort(key=lambda m: id_order.get(m["id"], len(message_ids)))

            found_ids = {m["id"] for m in messages}
            not_found = [mid for mid in message_ids if mid not in found_ids]

            result: Dict[str, Any] = {"messages": messages}
            if not_found:
                result["not_found"] = not_found

            return ToolResult(structured_content=result)

        except Exception as e:
            return ToolResult(
                structured_content={"error": f"Database query failed: {str(e)}"}
            )


mcp.add_tool(GetMessageDetailTool())


if __name__ == "__main__":
    sys.stderr.write("Starting Message Detail MCP Server with stdio transport\n")
    mcp.run(transport="stdio")

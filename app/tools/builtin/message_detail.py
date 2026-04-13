"""Message Detail built-in tool.

Retrieves full details of conversation messages by their IDs from SQLite.
"""

import json
import os
import sqlite3
from typing import Any, Dict

from app.tools.builtin.base import BuiltInTool, BuiltInToolResult


SERVER_NAME = "Message Detail"
SERVER_INSTRUCTIONS = "Retrieve the full details of conversation messages by their IDs."


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


class GetMessageDetailTool(BuiltInTool):
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

    def __init__(self, db_path: str = ""):
        self._db_path = db_path

    async def run(self, arguments: Dict[str, Any]) -> BuiltInToolResult:
        message_ids = arguments.get("message_ids", [])
        if not message_ids:
            return BuiltInToolResult(
                structured_content={"error": "No message IDs provided."}
            )

        if not os.path.isfile(self._db_path):
            return BuiltInToolResult(
                structured_content={
                    "error": f"Database file not found: {self._db_path}"
                }
            )

        try:
            conn = sqlite3.connect(self._db_path)
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

            return BuiltInToolResult(structured_content=result)

        except Exception as e:
            return BuiltInToolResult(
                structured_content={"error": f"Database query failed: {str(e)}"}
            )


def get_tools(config: dict) -> list[BuiltInTool]:
    """Return tool instances for this server."""
    db_path = config.get("SQLITE_DB_PATH", "")
    return [GetMessageDetailTool(db_path=db_path)]

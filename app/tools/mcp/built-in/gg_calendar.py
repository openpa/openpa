"""Google Calendar MCP server using stdio transport.

A standalone FastMCP server that provides Google Calendar tools.
Authentication is handled at the olli-agent level via MCPOAuthClient,
which manages OAuth tokens with Google and injects the access token
into tool call arguments as '_access_token'.

Based on: mcp-auth-gg-calendar/server.py

Usage:
    python app/mcp/stdio/gg_calendar.py

The access token is injected per-request by MCPAgentAdapter via the
'_access_token' argument.
"""

import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict
from app.utils.logger import logger

from fastmcp import FastMCP
from fastmcp.tools.tool import Tool, ToolResult


# Initialize FastMCP server
mcp = FastMCP(
    name="Google Calendar",
    instructions="A Google Calendar assistant that can list upcoming events and create new events.",
)


def _get_calendar_service(access_token: str):
    """Build Google Calendar service using the provided access token."""
    if not access_token:
        raise RuntimeError(
            "No access token available. "
            "Please authenticate via the Dashboard first."
        )

    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds = Credentials(
        token=access_token,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=["https://www.googleapis.com/auth/calendar.events", "https://www.googleapis.com/auth/calendar.modify"],
    )
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def _extract_access_token(arguments: Dict[str, Any]) -> str:
    """Extract access token from arguments or environment.

    The MCPAgentAdapter injects '_access_token' into tool arguments
    for stdio servers with authentication configured.
    """
    token = arguments.pop("_access_token", None)
    if token:
        return token
    raise RuntimeError(
        "Access token not found in tool arguments. "
        "Please authenticate via the Dashboard to use Google Calendar tools."
    )


class ListUpcomingEvents(Tool):
    name: str = "list_upcoming_events"
    description: str = "List upcoming events from the user's primary Google Calendar."
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "max_results": {
                "type": "integer",
                "description": "Maximum number of events to return. Default 10.",
            },
            "time_min": {
                "type": "string",
                "description": "Start time in ISO 8601 format (e.g., 2024-12-31T10:00:00Z). Defaults to now.",
            },
        },
        "required": ["max_results", "time_min"],
        "additionalProperties": False,
    }

    async def run(self, arguments: Dict[str, Any]) -> ToolResult:
        logger.debug(f"ListUpcomingEvents called with arguments: {arguments}")
        access_token = _extract_access_token(arguments)
        max_results = arguments.get("max_results", 10)
        time_min = arguments.get("time_min")

        try:
            service = _get_calendar_service(access_token)

            now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            t_min = time_min if time_min else now

            events_result = (
                service.events()
                .list(
                    calendarId="primary",
                    timeMin=t_min,
                    maxResults=max_results,
                    singleEvents=True,
                    orderBy="startTime",
                )
                .execute()
            )

            events = events_result.get("items", [])

            if not events:
                return ToolResult(
                    content=[{"type": "text", "text": "No upcoming events found."}]
                )

            result_lines = ["Upcoming events:"]
            for event in events:
                start = event["start"].get("dateTime", event["start"].get("date"))
                summary = event.get("summary", "No Title")
                description = event.get("description", "")
                html_link = event.get("htmlLink", "")
                line = f"- {start}: {summary}"
                if description:
                    line += f" ({description})"
                if html_link:
                    line += f" [Link: {html_link}]"
                result_lines.append(line)

            return ToolResult(
                content=[{"type": "text", "text": "\n".join(result_lines)}]
            )

        except RuntimeError as e:
            return ToolResult(
                content=[{"type": "text", "text": f"Authentication required: {str(e)}"}]
            )
        except Exception as e:
            return ToolResult(
                content=[{"type": "text", "text": f"Google Calendar API error: {str(e)}"}]
            )


class CreateEvent(Tool):
    name: str = "create_event"
    description: str = "Create a new event in the user's primary Google Calendar."
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "The title of the event.",
            },
            "start_time": {
                "type": "string",
                "description": "Start time in ISO 8601 format (e.g., 2024-12-31T10:00:00Z).",
            },
            "end_time": {
                "type": "string",
                "description": "End time in ISO 8601 format.",
            },
            "description": {
                "type": "string",
                "description": "Optional description/body of the event.",
            },
        },
        "required": ["summary", "start_time", "end_time"],
        "additionalProperties": False,
    }

    async def run(self, arguments: Dict[str, Any]) -> ToolResult:
        access_token = _extract_access_token(arguments)
        summary = arguments.get("summary")
        start_time = arguments.get("start_time")
        end_time = arguments.get("end_time")
        description = arguments.get("description", "")

        try:
            service = _get_calendar_service(access_token)

            event_body = {
                "summary": summary,
                "description": description,
                "start": {"dateTime": start_time, "timeZone": "UTC"},
                "end": {"dateTime": end_time, "timeZone": "UTC"},
            }

            event = (
                service.events()
                .insert(calendarId="primary", body=event_body)
                .execute()
            )

            return ToolResult(
                content=[{"type": "text", "text": f"Event '{summary}' created successfully. Link: {event.get('htmlLink', '')}"}]
            )

        except RuntimeError as e:
            return ToolResult(
                content=[{"type": "text", "text": f"Authentication required: {str(e)}"}]
            )
        except Exception as e:
            return ToolResult(
                content=[{"type": "text", "text": f"Failed to create event: {str(e)}"}]
            )


mcp.add_tool(ListUpcomingEvents())
mcp.add_tool(CreateEvent())


if __name__ == "__main__":
    sys.stderr.write("Starting Google Calendar MCP Server with stdio transport\n")
    mcp.run(transport="stdio")

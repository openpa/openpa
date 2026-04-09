"""Tool configuration API endpoints."""

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.tools.tool_config_manager import ToolConfigManager
from app.utils.logger import logger


def get_tool_routes(tool_config_manager: ToolConfigManager) -> list[Route]:

    async def handle_list_tools(request: Request) -> JSONResponse:
        """List all tools with their config status and enabled state."""
        profile = getattr(request.user, "username", "admin")
        tools = tool_config_manager.get_all_tools_status(profile=profile)
        return JSONResponse({"tools": tools})

    async def handle_get_tool_config(request: Request) -> JSONResponse:
        """Get config schema and current values for a specific tool."""
        profile = getattr(request.user, "username", "admin")
        tool_name = request.path_params["name"]
        status = tool_config_manager.get_tool_status(tool_name, profile=profile)
        schema = tool_config_manager.get_tool_config_schema(tool_name)

        if not schema:
            return JSONResponse(
                {"error": f"Tool '{tool_name}' not found"},
                status_code=404,
            )

        return JSONResponse({
            "status": status,
            "schema": schema,
        })

    async def handle_update_tool_config(request: Request) -> JSONResponse:
        """Update configuration for a specific tool."""
        profile = getattr(request.user, "username", "admin")
        tool_name = request.path_params["name"]

        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

        config = body.get("config", {})
        schema = tool_config_manager.get_tool_config_schema(tool_name)
        required_config = schema.get("tool", {}).get("required_config", {})

        for key, value in config.items():
            # Determine if this is a secret field
            field_spec = required_config.get(key, {})
            is_secret = field_spec.get("secret", False) or "secret" in key.lower() or "key" in key.lower()
            tool_config_manager.config_storage.set_tool_config(
                tool_name, key, str(value), is_secret=is_secret, profile=profile
            )

        return JSONResponse({"success": True})

    async def handle_set_tool_enabled(request: Request) -> JSONResponse:
        """Enable or disable a tool."""
        profile = getattr(request.user, "username", "admin")
        tool_name = request.path_params["name"]

        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

        enabled = body.get("enabled")
        if enabled is None:
            return JSONResponse({"error": "'enabled' field is required"}, status_code=400)

        tool_config_manager.set_tool_enabled(tool_name, bool(enabled), profile=profile)

        return JSONResponse({
            "success": True,
            "tool": tool_name,
            "enabled": bool(enabled),
        })

    return [
        Route("/api/tools", handle_list_tools, methods=["GET"]),
        Route("/api/tools/{name}/config", handle_get_tool_config, methods=["GET"]),
        Route("/api/tools/{name}/config", handle_update_tool_config, methods=["PUT"]),
        Route("/api/tools/{name}/enabled", handle_set_tool_enabled, methods=["PUT"]),
    ]

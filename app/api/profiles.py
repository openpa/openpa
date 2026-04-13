"""Profile management API."""

import json
import re
import urllib.parse

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.storage.conversation_storage import ConversationStorage
from app.tools import ToolRegistry
from app.utils import logger
from app.utils.persona import ensure_persona_file, read_persona_file, write_persona_file


def get_profile_routes(
    conversation_storage: ConversationStorage,
    *,
    registry: ToolRegistry | None = None,
) -> list[Route]:

    async def handle_list_profiles(request: Request) -> JSONResponse:
        try:
            profiles = await conversation_storage.list_profiles()
            visible = [p["name"] for p in profiles if not p["name"].startswith("__")]
            return JSONResponse({"profiles": visible}, status_code=200)
        except Exception as e:
            logger.error(f"Error listing profiles: {e}")
            return JSONResponse({"error": f"Internal server error: {e}"}, status_code=500)

    async def handle_add_profile(request: Request) -> JSONResponse:
        try:
            body = await request.json()
            name = body.get("name")
            if not name:
                return JSONResponse({"error": "Profile name is required"}, status_code=400)
            if not re.match(r"^[a-z0-9_-]+$", name):
                return JSONResponse(
                    {"error": "Profile name must contain only lowercase letters, numbers, hyphens, and underscores"},
                    status_code=400,
                )
            if await conversation_storage.profile_exists(name):
                return JSONResponse(
                    {"error": f"Profile '{name}' already exists"}, status_code=409,
                )

            profile = await conversation_storage.create_profile(name)
            ensure_persona_file(name)

            # Backfill profile_tools rows for every existing a2a/mcp tool
            if registry is not None:
                inserted = registry.on_profile_created(name)
                logger.info(
                    f"Backfilled {inserted} profile_tools row(s) for new profile '{name}'"
                )

            return JSONResponse(
                {
                    "success": True,
                    "message": f"Profile '{name}' created successfully",
                    "profile": profile["name"],
                },
                status_code=201,
            )
        except json.JSONDecodeError:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
        except Exception as e:
            logger.error(f"Error adding profile: {e}")
            return JSONResponse({"error": f"Internal server error: {e}"}, status_code=500)

    async def handle_delete_profile(request: Request) -> JSONResponse:
        try:
            profile_name = request.path_params.get("profile_name")
            if not profile_name:
                return JSONResponse({"error": "Profile name is required"}, status_code=400)
            profile_name = urllib.parse.unquote(profile_name)

            # Cascade FKs handle profile_tools / tool_configs / conversations / messages
            success = await conversation_storage.delete_profile(profile_name)
            if not success:
                return JSONResponse(
                    {"error": f"Profile '{profile_name}' not found"}, status_code=404,
                )

            return JSONResponse(
                {
                    "success": True,
                    "message": f"Profile '{profile_name}' deleted successfully",
                },
                status_code=200,
            )

        except Exception as e:
            logger.error(f"Error deleting profile: {e}")
            return JSONResponse({"error": f"Internal server error: {e}"}, status_code=500)

    async def handle_get_persona(request: Request) -> JSONResponse:
        profile_name = urllib.parse.unquote(request.path_params.get("profile_name", ""))
        if not profile_name:
            return JSONResponse({"error": "Profile name is required"}, status_code=400)
        try:
            content = read_persona_file(profile_name)
            return JSONResponse({"content": content})
        except Exception as e:
            logger.error(f"Error reading persona for '{profile_name}': {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

    async def handle_update_persona(request: Request) -> JSONResponse:
        profile_name = urllib.parse.unquote(request.path_params.get("profile_name", ""))
        if not profile_name:
            return JSONResponse({"error": "Profile name is required"}, status_code=400)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
        content = body.get("content")
        if content is None:
            return JSONResponse({"error": "'content' field is required"}, status_code=400)
        try:
            write_persona_file(profile_name, content)
            return JSONResponse({"success": True})
        except Exception as e:
            logger.error(f"Error writing persona for '{profile_name}': {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

    return [
        Route(path="/api/profiles", methods=["GET"], endpoint=handle_list_profiles),
        Route(path="/api/profiles", methods=["POST"], endpoint=handle_add_profile),
        Route(
            path="/api/profiles/{profile_name}/persona",
            methods=["GET"], endpoint=handle_get_persona,
        ),
        Route(
            path="/api/profiles/{profile_name}/persona",
            methods=["PUT"], endpoint=handle_update_persona,
        ),
        Route(
            path="/api/profiles/{profile_name}",
            methods=["DELETE"], endpoint=handle_delete_profile,
        ),
    ]

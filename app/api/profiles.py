import json
import re
import urllib.parse

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.storage.conversation_storage import ConversationStorage
from app.utils import logger


def get_profile_routes(conversation_storage: ConversationStorage,
                       remote_agent_storage=None,
                       mcp_server_storage=None, routing_agent=None) -> list[Route]:

    async def handle_list_profiles(request: Request) -> JSONResponse:
        """API endpoint to list all profiles."""
        try:
            profiles = await conversation_storage.list_profiles()
            # Filter out the __server__ pseudo-profile used internally for DCR credentials
            visible = [p["name"] for p in profiles if not p["name"].startswith("__")]
            return JSONResponse(
                content={"profiles": visible},
                status_code=200
            )
        except Exception as e:
            logger.error(f"Error listing profiles: {e}")
            return JSONResponse(
                content={"error": f"Internal server error: {str(e)}"},
                status_code=500
            )

    async def handle_add_profile(request: Request) -> JSONResponse:
        """API endpoint to add a new profile."""
        try:
            body = await request.json()
            name = body.get('name')

            if not name:
                return JSONResponse(
                    content={"error": "Profile name is required"},
                    status_code=400
                )

            # Validate profile name: lowercase letters, numbers, hyphens, underscores only
            if not re.match(r'^[a-z0-9_-]+$', name):
                return JSONResponse(
                    content={"error": "Profile name must contain only lowercase letters, numbers, hyphens, and underscores"},
                    status_code=400
                )

            # Check if profile already exists
            if await conversation_storage.profile_exists(name):
                return JSONResponse(
                    content={"error": f"Profile '{name}' already exists"},
                    status_code=409
                )

            # Add profile
            profile = await conversation_storage.create_profile(name)
            return JSONResponse(
                content={
                    "success": True,
                    "message": f"Profile '{name}' created successfully",
                    "profile": profile["name"]
                },
                status_code=201
            )

        except json.JSONDecodeError:
            return JSONResponse(
                content={"error": "Invalid JSON body"},
                status_code=400
            )
        except Exception as e:
            logger.error(f"Error adding profile: {e}")
            return JSONResponse(
                content={"error": f"Internal server error: {str(e)}"},
                status_code=500
            )

    async def handle_delete_profile(request: Request) -> JSONResponse:
        """API endpoint to delete a profile."""
        try:
            profile_name = request.path_params.get('profile_name')
            if not profile_name:
                return JSONResponse(
                    content={"error": "Profile name is required"},
                    status_code=400
                )

            # URL decode profile name
            profile_name = urllib.parse.unquote(profile_name)

            # Delete profile (cascade deletes conversations and messages)
            success = await conversation_storage.delete_profile(profile_name)
            if success:
                # Clean up profile-scoped agent and MCP server data
                if remote_agent_storage:
                    remote_agent_storage.remove_all_for_profile(profile_name)
                if mcp_server_storage:
                    mcp_server_storage.remove_all_for_profile(profile_name)
                if routing_agent:
                    routing_agent.remove_all_for_profile(profile_name)

                return JSONResponse(
                    content={
                        "success": True,
                        "message": f"Profile '{profile_name}' deleted successfully"
                    },
                    status_code=200
                )
            else:
                return JSONResponse(
                    content={"error": f"Profile '{profile_name}' not found"},
                    status_code=404
                )

        except Exception as e:
            logger.error(f"Error deleting profile: {e}")
            return JSONResponse(
                content={"error": f"Internal server error: {str(e)}"},
                status_code=500
            )

    return [
        Route(path='/api/profiles', methods=['GET'], endpoint=handle_list_profiles),
        Route(path='/api/profiles', methods=['POST'], endpoint=handle_add_profile),
        Route(path='/api/profiles/{profile_name}', methods=['DELETE'], endpoint=handle_delete_profile),
    ]

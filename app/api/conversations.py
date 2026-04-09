from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.storage.conversation_storage import ConversationStorage
from app.utils import logger


def get_conversation_routes(conversation_storage: ConversationStorage) -> list[Route]:

    async def handle_list_conversations(request: Request) -> JSONResponse:
        """List conversations for a profile.

        Query params: profile (required), limit (default 50), offset (default 0)
        """
        profile = request.query_params.get("profile")
        if not profile:
            return JSONResponse({"error": "Profile is required"}, status_code=400)

        limit = int(request.query_params.get("limit", "50"))
        offset = int(request.query_params.get("offset", "0"))

        conversations = await conversation_storage.list_conversations(profile, limit=limit, offset=offset)
        return JSONResponse({"conversations": conversations})

    async def handle_get_conversation(request: Request) -> JSONResponse:
        """Get a single conversation with its messages."""
        conversation_id = request.path_params["conversation_id"]
        conv = await conversation_storage.get_conversation(conversation_id)
        if not conv:
            return JSONResponse({"error": "Conversation not found"}, status_code=404)

        messages = await conversation_storage.get_messages(conversation_id)
        return JSONResponse({"conversation": conv, "messages": messages})

    async def handle_get_messages(request: Request) -> JSONResponse:
        """Get messages for a conversation (paginated)."""
        conversation_id = request.path_params["conversation_id"]
        limit = int(request.query_params.get("limit", "100"))
        offset = int(request.query_params.get("offset", "0"))

        conv = await conversation_storage.get_conversation(conversation_id)
        if not conv:
            return JSONResponse({"error": "Conversation not found"}, status_code=404)

        messages = await conversation_storage.get_messages(conversation_id, limit=limit, offset=offset)
        return JSONResponse({"messages": messages})

    async def handle_update_conversation(request: Request) -> JSONResponse:
        """Update a conversation (title, task_id)."""
        conversation_id = request.path_params["conversation_id"]
        body = await request.json()

        conv = await conversation_storage.get_conversation(conversation_id)
        if not conv:
            return JSONResponse({"error": "Conversation not found"}, status_code=404)

        update_fields = {}
        if "title" in body:
            update_fields["title"] = body["title"]
        if "task_id" in body:
            update_fields["task_id"] = body["task_id"]

        if update_fields:
            await conversation_storage.update_conversation(conversation_id, **update_fields)

        conv = await conversation_storage.get_conversation(conversation_id)
        return JSONResponse({"conversation": conv})

    async def handle_delete_conversation(request: Request) -> JSONResponse:
        """Delete a single conversation."""
        conversation_id = request.path_params["conversation_id"]
        deleted = await conversation_storage.delete_conversation(conversation_id)
        if not deleted:
            return JSONResponse({"error": "Conversation not found"}, status_code=404)
        return JSONResponse({"success": True})

    async def handle_delete_all_conversations(request: Request) -> JSONResponse:
        """Delete all conversations for a profile.

        Query params: profile (required)
        """
        profile = request.query_params.get("profile")
        if not profile:
            return JSONResponse({"error": "Profile is required"}, status_code=400)

        deleted_count = await conversation_storage.delete_all_conversations(profile)
        return JSONResponse({"success": True, "deleted_count": deleted_count})

    async def handle_conversations_dispatch(request: Request) -> JSONResponse:
        """Dispatch /api/conversations based on HTTP method."""
        if request.method == "GET":
            return await handle_list_conversations(request)
        elif request.method == "DELETE":
            return await handle_delete_all_conversations(request)
        return JSONResponse({"error": "Method not allowed"}, status_code=405)

    async def handle_conversation_detail_dispatch(request: Request) -> JSONResponse:
        """Dispatch /api/conversations/{id} based on HTTP method."""
        if request.method == "GET":
            return await handle_get_conversation(request)
        elif request.method == "PUT":
            return await handle_update_conversation(request)
        elif request.method == "DELETE":
            return await handle_delete_conversation(request)
        return JSONResponse({"error": "Method not allowed"}, status_code=405)

    return [
        Route(
            "/api/conversations",
            endpoint=handle_conversations_dispatch,
            methods=["GET", "DELETE"],
        ),
        Route(
            "/api/conversations/{conversation_id}",
            endpoint=handle_conversation_detail_dispatch,
            methods=["GET", "PUT", "DELETE"],
        ),
        Route(
            "/api/conversations/{conversation_id}/messages",
            endpoint=handle_get_messages,
            methods=["GET"],
        ),
    ]

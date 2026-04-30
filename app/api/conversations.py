from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.agent.stream_runner import cancel_run, make_run_id
from app.events import queue as event_queue
from app.storage.conversation_storage import ConversationStorage
from app.utils import logger
from app.utils.common import convert_db_messages_to_history


def _profile_from_request(request: Request) -> str:
    return getattr(request.user, "username", "") or ""


def _require_auth(request: Request):
    if not getattr(request.user, "is_authenticated", False):
        return JSONResponse({"error": "Unauthenticated"}, status_code=401)
    return None


def get_conversation_routes(
    conversation_storage: ConversationStorage,
    agent_executor=None,
) -> list[Route]:

    async def handle_list_conversations(request: Request) -> JSONResponse:
        """List conversations for the authenticated profile."""
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile_from_request(request)
        if not profile:
            return JSONResponse({"error": "Profile is required"}, status_code=400)

        limit = int(request.query_params.get("limit", "50"))
        offset = int(request.query_params.get("offset", "0"))

        conversations = await conversation_storage.list_conversations(profile, limit=limit, offset=offset)
        return JSONResponse({"conversations": conversations})

    async def handle_create_conversation(request: Request) -> JSONResponse:
        """Create a new (empty) conversation for the caller's profile.

        The frontend calls this *before* the first POST to
        ``/api/conversations/{id}/messages`` so message-send and stream-
        subscribe always operate on a real conversation id (no temp-id
        migration dance).
        """
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile_from_request(request)
        if not profile:
            return JSONResponse({"error": "Profile is required"}, status_code=400)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        title = (body.get("title") or "Untitled Chat") if isinstance(body, dict) else "Untitled Chat"
        conv = await conversation_storage.create_conversation(
            profile=profile, title=title,
        )
        return JSONResponse({"conversation": conv}, status_code=201)

    async def handle_get_conversation(request: Request) -> JSONResponse:
        """Get a single conversation with its messages."""
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile_from_request(request)
        conversation_id = request.path_params["conversation_id"]
        conv = await conversation_storage.get_conversation(conversation_id)
        if not conv:
            return JSONResponse({"error": "Conversation not found"}, status_code=404)
        if conv.get("profile") != profile:
            return JSONResponse({"error": "Forbidden"}, status_code=403)

        messages = await conversation_storage.get_messages(conversation_id)
        return JSONResponse({"conversation": conv, "messages": messages})

    async def handle_get_messages(request: Request) -> JSONResponse:
        """Get messages for a conversation (paginated)."""
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile_from_request(request)
        conversation_id = request.path_params["conversation_id"]
        limit = int(request.query_params.get("limit", "100"))
        offset = int(request.query_params.get("offset", "0"))

        conv = await conversation_storage.get_conversation(conversation_id)
        if not conv:
            return JSONResponse({"error": "Conversation not found"}, status_code=404)
        if conv.get("profile") != profile:
            return JSONResponse({"error": "Forbidden"}, status_code=403)

        messages = await conversation_storage.get_messages(conversation_id, limit=limit, offset=offset)
        return JSONResponse({"messages": messages})

    async def handle_post_message(request: Request) -> JSONResponse:
        """Enqueue a user message for streaming agent processing.

        Returns ``202 Accepted`` with a ``run_id`` immediately. The actual
        agent run executes server-side in a background task, publishing
        chunks to the conversation stream bus. Clients subscribe to
        ``GET /api/conversations/{id}/stream`` (separate SSE endpoint) to
        receive the live tail; multiple subscribers (tabs) all receive the
        same events, and disconnecting/reconnecting mid-run is safe.
        """
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile_from_request(request)
        if not profile:
            return JSONResponse({"error": "Profile is required"}, status_code=400)

        conversation_id = request.path_params["conversation_id"]
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

        text = (body.get("text") or "").strip()
        if not text:
            return JSONResponse(
                {"error": "Missing parameter", "message": "text is required"},
                status_code=400,
            )
        reasoning = bool(body.get("reasoning", True))

        conv = await conversation_storage.get_conversation(conversation_id)
        if conv is None:
            return JSONResponse({"error": "Conversation not found"}, status_code=404)
        if conv.get("profile") != profile:
            return JSONResponse({"error": "Forbidden"}, status_code=403)

        # Build chat history for the agent from the persisted conversation
        # (mirrors what OpenPAAgentExecutor does in the legacy A2A path).
        history_messages = []
        try:
            db_msgs = await conversation_storage.get_messages(conversation_id)
            if db_msgs:
                history_messages = convert_db_messages_to_history(db_msgs, inject_ids=True)
        except Exception:  # noqa: BLE001
            logger.exception(
                f"POST message: failed to load history for {conversation_id}"
            )

        run_id = make_run_id(conversation_id, kind="msg")

        await event_queue.enqueue_user_message(
            conversation_id=conversation_id,
            run_id=run_id,
            profile=profile,
            query=text,
            history_messages=history_messages,
            reasoning=reasoning,
            push_user_message=True,
            update_title_from_query=True,
        )

        return JSONResponse(
            {"run_id": run_id, "conversation_id": conversation_id},
            status_code=202,
        )

    async def handle_update_conversation(request: Request) -> JSONResponse:
        """Update a conversation (title, task_id)."""
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile_from_request(request)
        conversation_id = request.path_params["conversation_id"]
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

        conv = await conversation_storage.get_conversation(conversation_id)
        if not conv:
            return JSONResponse({"error": "Conversation not found"}, status_code=404)
        if conv.get("profile") != profile:
            return JSONResponse({"error": "Forbidden"}, status_code=403)

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
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile_from_request(request)
        conversation_id = request.path_params["conversation_id"]
        conv = await conversation_storage.get_conversation(conversation_id)
        if not conv:
            return JSONResponse({"error": "Conversation not found"}, status_code=404)
        if conv.get("profile") != profile:
            return JSONResponse({"error": "Forbidden"}, status_code=403)
        deleted = await conversation_storage.delete_conversation(conversation_id)
        if not deleted:
            return JSONResponse({"error": "Conversation not found"}, status_code=404)
        return JSONResponse({"success": True})

    async def handle_delete_all_conversations(request: Request) -> JSONResponse:
        """Delete all conversations for the authenticated profile."""
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile_from_request(request)
        if not profile:
            return JSONResponse({"error": "Profile is required"}, status_code=400)

        deleted_count = await conversation_storage.delete_all_conversations(profile)
        return JSONResponse({"success": True, "deleted_count": deleted_count})

    async def handle_conversations_dispatch(request: Request) -> JSONResponse:
        """Dispatch /api/conversations based on HTTP method."""
        if request.method == "GET":
            return await handle_list_conversations(request)
        elif request.method == "POST":
            return await handle_create_conversation(request)
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

    async def handle_messages_dispatch(request: Request) -> JSONResponse:
        """Dispatch /api/conversations/{id}/messages based on HTTP method."""
        if request.method == "GET":
            return await handle_get_messages(request)
        elif request.method == "POST":
            return await handle_post_message(request)
        return JSONResponse({"error": "Method not allowed"}, status_code=405)

    async def handle_cancel_task(request: Request) -> JSONResponse:
        """Cancel an in-flight agent run. Idempotent.

        Resolves a ``task_id`` (or ``run_id``) against both the new
        unified ``stream_runner`` registry and the legacy A2A executor's
        registry, so cancel works regardless of which path launched the run.
        """
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        task_id = request.path_params["task_id"]
        cancelled = cancel_run(task_id)
        if not cancelled and agent_executor is not None:
            cancelled = agent_executor.cancel_by_task_id(task_id)
        return JSONResponse({"cancelled": cancelled})

    return [
        Route(
            "/api/conversations",
            endpoint=handle_conversations_dispatch,
            methods=["GET", "POST", "DELETE"],
        ),
        Route(
            "/api/conversations/{conversation_id}",
            endpoint=handle_conversation_detail_dispatch,
            methods=["GET", "PUT", "DELETE"],
        ),
        Route(
            "/api/conversations/{conversation_id}/messages",
            endpoint=handle_messages_dispatch,
            methods=["GET", "POST"],
        ),
        Route(
            "/api/tasks/{task_id}/cancel",
            endpoint=handle_cancel_task,
            methods=["POST"],
        ),
    ]

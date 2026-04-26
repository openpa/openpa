"""Skill Event API.

REST endpoints for the conversation-scoped event subscription system:

- list / delete subscriptions
- simulate an event (drop a markdown file into the watched folder)
- discover events declared by a skill
- check / start a skill's listener daemon
- stream event-driven notifications for a profile over SSE
- stream live events for a conversation over SSE
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Dict, Optional

from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from app.events import (
    get_event_manager,
    get_event_notifications,
    get_event_stream_bus,
    get_notifications_stream_bus,
)
from app.storage import (
    get_autostart_storage,
    get_conversation_storage,
    get_event_subscription_storage,
)
from app.tools.builtin.exec_shell_autostart import spawn_from_autostart
from app.tools.builtin.register_skill_event import _resolve_skill_source
from app.utils.logger import logger


# Default IMAP poll interval is 30s (see email-cli config). Heartbeat-based
# liveness uses a generous 3× window so a single missed beat doesn't flag down.
_DEFAULT_HEARTBEAT_WINDOW_S = 90.0


def _profile_from_request(request: Request) -> str:
    return getattr(request.user, "username", "") or ""


def _require_auth(request: Request) -> Optional[JSONResponse]:
    if not getattr(request.user, "is_authenticated", False):
        return JSONResponse({"error": "Unauthenticated"}, status_code=401)
    return None


async def _list_subscriptions_for_profile(profile: str) -> list[Dict[str, Any]]:
    store = get_event_subscription_storage()
    rows = store.list_by_profile(profile)
    if not rows:
        return []
    conv_storage = get_conversation_storage()
    titles: Dict[str, str] = {}
    for row in rows:
        cid = row["conversation_id"]
        if cid in titles:
            continue
        try:
            conv = await conv_storage.get_conversation(cid)
        except Exception:  # noqa: BLE001
            conv = None
        titles[cid] = (conv or {}).get("title") or "Untitled Chat"
    enriched = []
    for row in rows:
        enriched.append({**row, "conversation_title": titles.get(row["conversation_id"], "")})
    return enriched


def _heartbeat_path(source_dir: str) -> Path:
    return Path(source_dir) / "scripts" / ".listener_heartbeat"


def _listener_command(source_dir: str) -> str:
    """Return the canonical autostart command for a skill's listener daemon."""
    script = Path(source_dir) / "scripts" / "event_listener.py"
    return f'uv run "{script}"'


def get_event_routes() -> list[Route]:

    async def handle_list(request: Request) -> JSONResponse:
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile_from_request(request)
        subs = await _list_subscriptions_for_profile(profile)
        return JSONResponse({"subscriptions": subs})

    async def handle_delete(request: Request) -> JSONResponse:
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile_from_request(request)
        sub_id = request.path_params["id"]
        store = get_event_subscription_storage()
        existing = store.get(sub_id)
        if existing is None:
            return JSONResponse({"error": "Not found"}, status_code=404)
        if profile and existing["profile"] != profile:
            return JSONResponse({"error": "Forbidden"}, status_code=403)
        store.delete(sub_id)
        try:
            get_event_manager().release_watcher(
                profile=existing["profile"],
                skill_name=existing["skill_name"],
                event_type=existing["event_type"],
            )
        except Exception:  # noqa: BLE001
            logger.exception("Failed to release watcher after delete")
        return JSONResponse({"ok": True})

    async def handle_simulate(request: Request) -> JSONResponse:
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile_from_request(request)
        sub_id = request.path_params["id"]
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
        content = body.get("content") or ""
        if not content.strip():
            return JSONResponse(
                {"error": "Missing parameter", "message": "content is required"},
                status_code=400,
            )
        # Optional user-supplied filename. Default to a unique simulate-*.md.
        # Sanitize: strip directory components, allow only basename, force .md.
        raw_name = (body.get("filename") or "").strip()
        if raw_name:
            safe_name = Path(raw_name).name  # strip any path separators
            # Disallow empty / dotfile-only / Windows-reserved characters.
            forbidden = set('<>:"/\\|?*\0')
            if not safe_name or safe_name in (".", "..") or any(
                c in forbidden for c in safe_name
            ):
                return JSONResponse(
                    {
                        "error": "invalid_filename",
                        "message": "filename contains illegal characters or is empty",
                    },
                    status_code=400,
                )
            if not safe_name.lower().endswith(".md"):
                safe_name = f"{safe_name}.md"
            filename = safe_name
        else:
            filename = f"simulate-{int(time.time() * 1000)}.md"
        existing = get_event_subscription_storage().get(sub_id)
        if existing is None:
            return JSONResponse({"error": "Not found"}, status_code=404)
        if profile and existing["profile"] != profile:
            return JSONResponse({"error": "Forbidden"}, status_code=403)
        source_dir = _resolve_skill_source(existing["skill_name"], profile)
        if not source_dir:
            return JSONResponse(
                {"error": "Skill source not found"}, status_code=404,
            )
        events_dir = Path(source_dir) / "events" / existing["event_type"]
        try:
            events_dir.mkdir(parents=True, exist_ok=True)
            file_path = events_dir / filename
            # Avoid clobbering a same-named file already waiting to fire.
            if file_path.exists():
                stem, suffix = file_path.stem, file_path.suffix
                file_path = events_dir / f"{stem}-{int(time.time() * 1000)}{suffix}"
            file_path.write_text(content, encoding="utf-8")
        except OSError as exc:
            return JSONResponse(
                {"error": "write_failed", "message": str(exc)}, status_code=500,
            )
        return JSONResponse({"ok": True, "path": str(file_path)})

    async def handle_skill_events(request: Request) -> JSONResponse:
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile_from_request(request)
        skill_name = request.path_params["skill_name"]
        source_dir = _resolve_skill_source(skill_name, profile)
        if not source_dir:
            return JSONResponse({"error": "Skill not found"}, status_code=404)
        from app.tools.builtin.register_skill_event import _read_events_metadata

        events = _read_events_metadata(Path(source_dir))
        return JSONResponse(
            {"skill_name": skill_name, "source_dir": source_dir, "events": events}
        )

    async def handle_listener_status(request: Request) -> JSONResponse:
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile_from_request(request)
        skill_name = request.path_params["skill_name"]
        source_dir = _resolve_skill_source(skill_name, profile)
        if not source_dir:
            return JSONResponse({"error": "Skill not found"}, status_code=404)
        hb = _heartbeat_path(source_dir)
        last_heartbeat: Optional[float] = None
        running = False
        if hb.exists():
            try:
                last_heartbeat = hb.stat().st_mtime
            except OSError:
                last_heartbeat = None
        if last_heartbeat is not None:
            running = (time.time() - last_heartbeat) < _DEFAULT_HEARTBEAT_WINDOW_S

        # Find the autostart row for this listener if one exists.
        command = _listener_command(source_dir)
        autostart_storage = get_autostart_storage()
        autostart_row = autostart_storage.find_duplicate(profile, command)

        return JSONResponse(
            {
                "skill_name": skill_name,
                "running": running,
                "last_heartbeat": last_heartbeat,
                "autostart_id": (autostart_row or {}).get("id"),
                "command": command,
            }
        )

    async def handle_listener_start(request: Request) -> JSONResponse:
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile_from_request(request)
        skill_name = request.path_params["skill_name"]
        source_dir = _resolve_skill_source(skill_name, profile)
        if not source_dir:
            return JSONResponse({"error": "Skill not found"}, status_code=404)
        command = _listener_command(source_dir)
        scripts_dir = str(Path(source_dir) / "scripts")
        autostart_storage = get_autostart_storage()
        row = autostart_storage.find_duplicate(profile, command)
        if row is None:
            row = autostart_storage.insert(
                profile=profile,
                command=command,
                working_dir=scripts_dir,
                is_pty=False,
            )
        process_id, error = await spawn_from_autostart(row)
        if error:
            autostart_storage.set_error(row["id"], error)
            return JSONResponse(
                {"error": "spawn_failed", "message": error}, status_code=400,
            )
        autostart_storage.clear_error(row["id"])
        return JSONResponse(
            {"ok": True, "process_id": process_id, "autostart_id": row["id"]}
        )

    async def handle_conversation_stream(request: Request) -> Any:
        """Server-Sent Events endpoint streaming live agent runs for a conversation.

        Subscribes to the in-memory :class:`ConversationStreamBus`. The first
        events yielded are a replay of the in-progress run (if any), so a UI
        client opening the conversation mid-stream catches up before the live
        tail begins. A ``ready`` marker separates replay from live events.
        """
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile_from_request(request)
        conversation_id = request.path_params["id"]

        try:
            conv = await get_conversation_storage().get_conversation(conversation_id)
        except Exception:  # noqa: BLE001
            logger.exception("Stream endpoint: failed to load conversation")
            return JSONResponse({"error": "Lookup failed"}, status_code=500)
        if conv is None:
            return JSONResponse({"error": "Not found"}, status_code=404)
        if profile and conv.get("profile") != profile:
            return JSONResponse({"error": "Forbidden"}, status_code=403)

        bus = get_event_stream_bus()
        queue, replay, is_active = await bus.subscribe(conversation_id)

        async def generator():
            def _frame(payload: Dict[str, Any]) -> bytes:
                return f"data: {json.dumps(payload)}\n\n".encode("utf-8")

            try:
                for ev in replay:
                    yield _frame(ev)
                yield _frame({"type": "ready", "data": {"is_active": is_active}})

                # Heartbeat so the client/proxy keeps the connection open
                # during quiet periods (no events for a while).
                while True:
                    if await request.is_disconnected():
                        return
                    try:
                        ev = await asyncio.wait_for(queue.get(), timeout=15.0)
                    except asyncio.TimeoutError:
                        # SSE comment line — ignored by clients, keeps proxies happy.
                        yield b": keepalive\n\n"
                        continue
                    yield _frame(ev)
            finally:
                await bus.unsubscribe(conversation_id, queue)

        headers = {
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        }
        return StreamingResponse(
            generator(), media_type="text/event-stream", headers=headers,
        )

    async def handle_notifications_stream(request: Request) -> Any:
        """SSE endpoint streaming live skill-event notifications for the caller's profile.

        On connect, the client may pass ``?since=<ms>`` as a cursor — entries
        in the buffer with ``created_at > since`` are replayed first (catching
        the client up after a transient disconnect), followed by a ``ready``
        marker and then the live tail. With ``since=0`` (or omitted), the full
        per-profile ring buffer is replayed.
        """
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile_from_request(request)

        since_raw = request.query_params.get("since") or "0"
        try:
            since_ms = float(since_raw)
        except ValueError:
            since_ms = 0.0

        bus = get_notifications_stream_bus()
        queue = bus.subscribe(profile)
        replay = get_event_notifications().since(profile, since_ms)

        async def generator():
            def _frame(payload: Dict[str, Any]) -> bytes:
                return f"data: {json.dumps(payload)}\n\n".encode("utf-8")

            try:
                for entry in replay:
                    yield _frame({"type": "notification", "data": entry})
                yield _frame({"type": "ready", "data": {}})

                while True:
                    if await request.is_disconnected():
                        return
                    try:
                        entry = await asyncio.wait_for(queue.get(), timeout=15.0)
                    except asyncio.TimeoutError:
                        yield b": keepalive\n\n"
                        continue
                    yield _frame({"type": "notification", "data": entry})
            finally:
                bus.unsubscribe(profile, queue)

        headers = {
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        }
        return StreamingResponse(
            generator(), media_type="text/event-stream", headers=headers,
        )

    return [
        Route("/api/skill-events", handle_list, methods=["GET"]),
        Route("/api/skill-events/{id}", handle_delete, methods=["DELETE"]),
        Route("/api/skill-events/{id}/simulate", handle_simulate, methods=["POST"]),
        Route("/api/skills/{skill_name}/events", handle_skill_events, methods=["GET"]),
        Route(
            "/api/skills/{skill_name}/listener-status",
            handle_listener_status, methods=["GET"],
        ),
        Route(
            "/api/skills/{skill_name}/listener-start",
            handle_listener_start, methods=["POST"],
        ),
        Route(
            "/api/skill-events/notifications/stream",
            handle_notifications_stream, methods=["GET"],
        ),
        Route(
            "/api/conversations/{id}/stream",
            handle_conversation_stream, methods=["GET"],
        ),
    ]

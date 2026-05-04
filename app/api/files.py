"""File serving API for OPENPA_WORKING_DIR.

Serves files from the configured data directory with path-traversal prevention.
Protected by the application's existing JWT authentication middleware.
"""

import asyncio
import json
import os

from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.routing import Route
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from app.config.settings import BaseConfig, get_user_working_directory
from app.utils.context_storage import get_context
from app.utils.logger import logger

_WORKING_DIR_OVERRIDE_KEY = "_working_directory_override"


def _allowed_bases() -> list[str]:
    """Directories the file-serving routes are allowed to read from.

    Includes both the internal OpenPA working dir and the user's
    working dir (where the agent operates and produces files).
    """
    bases = [os.path.realpath(BaseConfig.OPENPA_WORKING_DIR)]
    user_dir = os.path.realpath(get_user_working_directory())
    if user_dir not in bases:
        bases.append(user_dir)
    return bases


def _allowed_bases_for_conversation(conversation_id: str | None) -> list[str]:
    """Same as ``_allowed_bases`` plus the active conversation's cwd override.

    The ``change_working_directory`` tool may switch a conversation into an
    arbitrary directory outside the static bases via ``target='custom'``.
    File-tree clients pass the originating ``conversation_id`` so the API
    can widen the allowlist for that one request without weakening the
    sandbox for unattributed callers.
    """
    bases = _allowed_bases()
    if not conversation_id:
        return bases
    override = get_context(conversation_id, _WORKING_DIR_OVERRIDE_KEY)
    if not override:
        return bases
    extra = os.path.realpath(override)
    if extra not in bases:
        bases.append(extra)
    return bases


def _is_inside_allowed(target: str, conversation_id: str | None = None) -> bool:
    for base in _allowed_bases_for_conversation(conversation_id):
        if target == base or target.startswith(base + os.sep):
            return True
    return False


def _safe_resolve(relative_path: str) -> str | None:
    """Resolve a relative path inside OPENPA_WORKING_DIR.

    Returns the absolute path if safe, or None if traversal is detected.
    """
    base = os.path.realpath(BaseConfig.OPENPA_WORKING_DIR)
    target = os.path.realpath(os.path.join(base, relative_path))
    if target != base and not target.startswith(base + os.sep):
        return None
    return target


def _require_auth(request: Request):
    if not getattr(request.user, "is_authenticated", False):
        return JSONResponse({"error": "Unauthenticated"}, status_code=401)
    return None


async def _serve_file(request: Request):
    unauth = _require_auth(request)
    if unauth is not None:
        return unauth
    relative_path = request.path_params.get("path", "")
    if not relative_path:
        return JSONResponse({"error": "No path specified"}, status_code=400)

    logger.debug(f"File request: relative_path={relative_path}, OPENPA_WORKING_DIR={BaseConfig.OPENPA_WORKING_DIR}")
    target = _safe_resolve(relative_path)
    if target is None:
        return JSONResponse({"error": "Access denied"}, status_code=403)

    logger.debug(f"Resolved file path: {target}, exists={os.path.isfile(target)}")
    if not os.path.isfile(target):
        return JSONResponse(
            {"error": "File not found", "resolved_path": target, "data_dir": BaseConfig.OPENPA_WORKING_DIR},
            status_code=404,
        )

    return FileResponse(target)


async def _serve_file_by_path(request: Request):
    """Serve a file given its absolute path.

    The path must resolve inside one of the allowed bases
    (OPENPA_WORKING_DIR or the user working directory).
    """
    unauth = _require_auth(request)
    if unauth is not None:
        return unauth
    abs_path = request.query_params.get("path", "")
    if not abs_path:
        return JSONResponse({"error": "No path specified"}, status_code=400)
    conversation_id = request.query_params.get("conversation_id") or None

    target = os.path.realpath(abs_path)
    if not _is_inside_allowed(target, conversation_id):
        logger.debug(
            f"File access denied: target={target}, "
            f"allowed_bases={_allowed_bases_for_conversation(conversation_id)}"
        )
        return JSONResponse({"error": "Access denied"}, status_code=403)

    if not os.path.isfile(target):
        return JSONResponse({"error": "File not found"}, status_code=404)

    return FileResponse(target)


_DIRECTORY_LIST_CAP = 2000


async def _list_directory(request: Request):
    """List entries in a directory.

    Used by the right-side file tree to render the workspace under the
    current shell working directory. Honors the same path-allowlist as the
    file-serving endpoints.
    """
    unauth = _require_auth(request)
    if unauth is not None:
        return unauth

    abs_path = request.query_params.get("path", "")
    if not abs_path:
        return JSONResponse({"error": "No path specified"}, status_code=400)
    show_hidden = request.query_params.get("show_hidden", "0") == "1"
    conversation_id = request.query_params.get("conversation_id") or None

    target = os.path.realpath(abs_path)
    if not _is_inside_allowed(target, conversation_id):
        return JSONResponse({"error": "Access denied"}, status_code=403)
    if not os.path.isdir(target):
        return JSONResponse({"error": "Not a directory"}, status_code=404)

    entries: list[dict] = []
    try:
        with os.scandir(target) as it:
            for de in it:
                if not show_hidden and de.name.startswith("."):
                    continue
                try:
                    is_dir = de.is_dir(follow_symlinks=False)
                    is_file = de.is_file(follow_symlinks=False)
                    st = de.stat(follow_symlinks=False)
                except OSError:
                    continue
                entries.append({
                    "name": de.name,
                    "path": os.path.join(target, de.name),
                    "is_dir": is_dir,
                    "size": st.st_size if is_file else None,
                    "modified": st.st_mtime,
                })
    except PermissionError:
        return JSONResponse({"error": "Permission denied"}, status_code=403)

    entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
    truncated = len(entries) > _DIRECTORY_LIST_CAP
    if truncated:
        entries = entries[:_DIRECTORY_LIST_CAP]
    return JSONResponse({"path": target, "entries": entries, "truncated": truncated})


async def _get_cwd(request: Request):
    """Return the seed working directory for the file tree.

    The live cwd flows in via terminal WebSocket ``status`` messages once a
    terminal is open; this endpoint just provides the initial value before
    any terminal exists.
    """
    unauth = _require_auth(request)
    if unauth is not None:
        return unauth
    return JSONResponse({"cwd": get_user_working_directory()})


_WATCH_QUEUE_SIZE = 2048


async def _watch_directory(request: Request):
    """Stream filesystem-change events for a directory as Server-Sent Events.

    Uses watchdog's recursive Observer so the entire subtree under ``path``
    is monitored. Events are emitted as JSON frames with ``type`` of
    ``ready`` (handshake), ``created``, ``deleted``, ``modified``, or
    ``moved``. The same allowlist applies as the file-serving endpoints.

    Both the Vue UI and the Go CLI consume this via ordinary SSE clients —
    no extra protocol or dependency needed on the consumer side.
    """
    unauth = _require_auth(request)
    if unauth is not None:
        return unauth

    abs_path = request.query_params.get("path", "")
    if not abs_path:
        return JSONResponse({"error": "No path specified"}, status_code=400)
    conversation_id = request.query_params.get("conversation_id") or None
    target = os.path.realpath(abs_path)
    if not _is_inside_allowed(target, conversation_id):
        return JSONResponse({"error": "Access denied"}, status_code=403)
    if not os.path.isdir(target):
        return JSONResponse({"error": "Not a directory"}, status_code=404)

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue(maxsize=_WATCH_QUEUE_SIZE)

    def _enqueue(payload: dict) -> None:
        try:
            loop.call_soon_threadsafe(queue.put_nowait, payload)
        except RuntimeError:
            # Loop closed during shutdown; drop the event.
            pass
        except asyncio.QueueFull:
            # Bursty changes (e.g. ``npm install``) overflow; drop oldest.
            try:
                queue.get_nowait()
                loop.call_soon_threadsafe(queue.put_nowait, payload)
            except Exception:
                pass

    class _Handler(FileSystemEventHandler):
        def on_created(self, event: FileSystemEvent) -> None:
            _enqueue({
                "type": "created",
                "path": event.src_path,
                "is_dir": event.is_directory,
            })

        def on_deleted(self, event: FileSystemEvent) -> None:
            _enqueue({
                "type": "deleted",
                "path": event.src_path,
                "is_dir": event.is_directory,
            })

        def on_modified(self, event: FileSystemEvent) -> None:
            # Modifications fire on directories whenever a child changes —
            # that's noisy and the consumer can't act on it (its child list
            # is unaffected). Only forward file-level modifications.
            if event.is_directory:
                return
            _enqueue({
                "type": "modified",
                "path": event.src_path,
                "is_dir": False,
            })

        def on_moved(self, event: FileSystemEvent) -> None:
            _enqueue({
                "type": "moved",
                "path": event.src_path,
                "dest_path": getattr(event, "dest_path", ""),
                "is_dir": event.is_directory,
            })

    observer = Observer()
    try:
        observer.schedule(_Handler(), target, recursive=True)
        observer.start()
    except Exception as e:  # noqa: BLE001
        logger.exception("Failed to start filesystem watcher for %s", target)
        return JSONResponse({"error": f"Failed to watch: {e}"}, status_code=500)

    async def generator():
        def _frame(payload: dict) -> bytes:
            return f"data: {json.dumps(payload)}\n\n".encode("utf-8")

        try:
            yield _frame({"type": "ready", "path": target})
            while True:
                if await request.is_disconnected():
                    return
                try:
                    ev = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield b": keepalive\n\n"
                    continue
                yield _frame(ev)
        finally:
            try:
                observer.stop()
                observer.join(timeout=2.0)
            except Exception:  # noqa: BLE001
                logger.debug("Observer cleanup raised", exc_info=True)

    headers = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    }
    return StreamingResponse(
        generator(), media_type="text/event-stream", headers=headers,
    )


def get_file_routes() -> list[Route]:
    """Return routes for the file serving API."""
    # Specific routes must come before the catch-all ``/api/files/{path:path}``
    # so Starlette doesn't dispatch ``/list``, ``/cwd``, ``/watch`` to
    # ``_serve_file``.
    return [
        Route("/api/files/list", endpoint=_list_directory, methods=["GET"]),
        Route("/api/files/cwd", endpoint=_get_cwd, methods=["GET"]),
        Route("/api/files/watch", endpoint=_watch_directory, methods=["GET"]),
        Route("/api/files/open", endpoint=_serve_file_by_path, methods=["GET"]),
        Route("/api/files/{path:path}", endpoint=_serve_file, methods=["GET"]),
    ]

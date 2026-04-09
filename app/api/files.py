"""File serving API for OPENPA_WORKING_DIR.

Serves files from the configured data directory with path-traversal prevention.
Protected by the application's existing JWT authentication middleware.
"""

import os

from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route

from app.config.settings import BaseConfig
from app.utils.logger import logger


def _safe_resolve(relative_path: str) -> str | None:
    """Resolve a relative path inside OPENPA_WORKING_DIR.

    Returns the absolute path if safe, or None if traversal is detected.
    """
    base = os.path.realpath(BaseConfig.OPENPA_WORKING_DIR)
    target = os.path.realpath(os.path.join(base, relative_path))
    if target != base and not target.startswith(base + os.sep):
        return None
    return target


async def _serve_file(request: Request):
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
    """Serve a file given its absolute path (must be inside OPENPA_WORKING_DIR)."""
    abs_path = request.query_params.get("path", "")
    if not abs_path:
        return JSONResponse({"error": "No path specified"}, status_code=400)

    # Normalise and verify the absolute path is inside the working directory
    base = os.path.realpath(BaseConfig.OPENPA_WORKING_DIR)
    target = os.path.realpath(abs_path)
    if target != base and not target.startswith(base + os.sep):
        return JSONResponse({"error": "Access denied"}, status_code=403)

    if not os.path.isfile(target):
        return JSONResponse({"error": "File not found"}, status_code=404)

    return FileResponse(target)


def get_file_routes() -> list[Route]:
    """Return routes for the file serving API."""
    return [
        Route("/api/files/open", endpoint=_serve_file_by_path, methods=["GET"]),
        Route("/api/files/{path:path}", endpoint=_serve_file, methods=["GET"]),
    ]

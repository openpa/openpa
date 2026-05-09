"""Upgrade-availability endpoint for the UI's update banner.

Phase 5 ships the read-only ``/api/upgrade/check``: the UI uses it to
render a banner when a newer version is on GitHub. Actually applying
the upgrade is intentionally still a CLI operation (``opa upgrade``)
because doing it safely requires either a service supervisor or a
clean process replacement — neither of which we want to invent inside
a running HTTP server. When that supervisor lands in a follow-up,
``POST /api/upgrade/apply`` slots in here without changing the
banner's contract.

This endpoint is unauthenticated. The version itself is already
public (it's in ``/version``); knowing the latest available version
isn't a privacy escalation, and we want the banner to render in the
setup-wizard pre-token flow too.
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.__version__ import __version__ as CURRENT_VERSION


async def get_upgrade_check(_request: Request) -> JSONResponse:
    # Imports are lazy because the upgrade module pulls in urllib and
    # the manifest helpers — fine at runtime, but unnecessary work
    # during the API-route registration pass.
    try:
        from app.upgrade.manifest import fetch_latest, is_at_or_above, is_newer
    except ImportError:
        return JSONResponse({
            "current": CURRENT_VERSION,
            "status": "unavailable",
            "reason": "Upgrade module not installed.",
        })

    try:
        release = fetch_latest()
    except Exception as e:  # noqa: BLE001
        return JSONResponse({
            "current": CURRENT_VERSION,
            "status": "unreachable",
            "reason": str(e),
        })

    if not is_newer(release.version, CURRENT_VERSION):
        return JSONResponse({
            "current": CURRENT_VERSION,
            "latest": release.version,
            "status": "up_to_date",
            "release_url": release.html_url,
        })

    if not is_at_or_above(CURRENT_VERSION, release.min_supported_upgrade_from):
        # The new release refuses to migrate from us — surface it so
        # the UI can route the user to the legacy-export instructions
        # instead of pretending an in-place upgrade will work.
        return JSONResponse({
            "current": CURRENT_VERSION,
            "latest": release.version,
            "status": "too_old",
            "min_supported_upgrade_from": release.min_supported_upgrade_from,
            "release_url": release.html_url,
        })

    return JSONResponse({
        "current": CURRENT_VERSION,
        "latest": release.version,
        "status": "available",
        "min_compatible_ui": release.min_compatible_ui,
        "release_url": release.html_url,
        "release_notes": release.body,
        # The user runs ``opa upgrade`` themselves for now. The UI
        # banner shows this command verbatim so the user doesn't have
        # to leave the app to find it.
        "apply_command": "opa upgrade -y",
    })


def get_upgrade_routes() -> list[Route]:
    return [
        Route("/api/upgrade/check", get_upgrade_check, methods=["GET"]),
    ]

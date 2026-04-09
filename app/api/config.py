"""Server configuration and setup API endpoints."""

import os
import re
import secrets

import jwt
from datetime import datetime, timezone, timedelta

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.config.settings import BaseConfig
from app.storage.dynamic_config_storage import DynamicConfigStorage
from app.storage.conversation_storage import ConversationStorage
from app.utils.logger import logger

# Profile name validation: lowercase, numbers, underscore, hyphen only
PROFILE_NAME_PATTERN = re.compile(r'^[a-z0-9_-]+$')



def _validate_profile_name(name: str) -> str | None:
    """Validate profile name. Returns error message or None if valid."""
    if not name:
        return "Profile name is required"
    if not PROFILE_NAME_PATTERN.match(name):
        return "Profile name must contain only lowercase letters, numbers, hyphens, and underscores"
    if len(name) > 64:
        return "Profile name must be 64 characters or less"
    return None


def _generate_token(jwt_secret: str, profile: str, hours: int | None = None) -> tuple[str, str]:
    """Generate a JWT token for a profile. Returns (token, expires_at)."""
    if hours is None:
        hours = BaseConfig.JWT_EXPIRATION_HOURS
    now = datetime.now(timezone.utc)
    payload = {
        "sub": profile,
        "profile": profile,
        "iat": now,
        "exp": now + timedelta(hours=hours),
    }
    token = jwt.encode(payload, jwt_secret, algorithm="HS256")
    expires_at = (now + timedelta(hours=hours)).isoformat()
    return token, expires_at


def get_config_routes(
    config_storage: DynamicConfigStorage,
    conversation_storage: ConversationStorage,
) -> list[Route]:

    async def handle_setup_status(request: Request) -> JSONResponse:
        """Check if first-time setup has been completed. No auth required.

        Also accepts ?profile=xxx to check if a specific profile exists.
        """
        profile = request.query_params.get("profile")
        setup_complete = config_storage.is_setup_complete()
        result = {
            "setup_complete": setup_complete,
        }
        if profile:
            result["profile_exists"] = await conversation_storage.profile_exists(profile)
        # When setup is marked complete, report whether any profiles exist.
        # This lets clients detect the orphaned state where setup_complete=true
        # but all profiles have been deleted externally.
        if setup_complete:
            profiles = await conversation_storage.list_profiles()
            visible = [p for p in profiles if not p["name"].startswith("__")]
            result["has_profiles"] = len(visible) > 0
        return JSONResponse(result)

    async def handle_setup(request: Request) -> JSONResponse:
        """Complete setup for a profile. No auth required.

        For the first profile (admin): saves server config, LLM config, tool configs,
        creates the profile, generates token, and marks setup complete.

        For subsequent profiles: creates the profile, saves LLM and tool configs,
        and generates token. Server-level config cannot be changed for non-first profiles.

        Expects JSON body with:
        - profile: str (required) — the profile name to create
        - server_config: dict of server settings (first setup only)
        - llm_config: dict of LLM settings
        - tool_configs: dict of {tool_name: {key: value}}
        """
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

        profile_name = body.get("profile", "").strip()

        # Validate profile name
        validation_error = _validate_profile_name(profile_name)
        if validation_error:
            return JSONResponse({"error": validation_error}, status_code=400)

        is_first_setup = not config_storage.is_setup_complete()

        # First setup must be 'admin'
        if is_first_setup and profile_name != "admin":
            return JSONResponse(
                {"error": "First profile must be named 'admin'"},
                status_code=400,
            )

        # For subsequent profiles, check the profile doesn't already exist
        if not is_first_setup:
            if await conversation_storage.profile_exists(profile_name):
                return JSONResponse(
                    {"error": f"Profile '{profile_name}' already exists"},
                    status_code=409,
                )

        # Create the profile first (needed for FK constraints on llm_config / tool_configs)
        if not await conversation_storage.profile_exists(profile_name):
            await conversation_storage.create_profile(profile_name)

        # Save server config only on first setup
        if is_first_setup:
            server_config = body.get("server_config", {})
            for key, value in server_config.items():
                is_secret = key in ("jwt_secret",)
                config_storage.set("server_config", key, str(value), is_secret=is_secret)

            # Generate JWT secret if not provided
            jwt_secret = config_storage.get("server_config", "jwt_secret")
            if not jwt_secret and not BaseConfig.get_jwt_secret():
                jwt_secret = secrets.token_urlsafe(32)
                config_storage.set("server_config", "jwt_secret", jwt_secret, is_secret=True)

            config_storage.mark_setup_complete()

        # Save LLM and tool configs for ALL profiles (including first setup)
        llm_config = body.get("llm_config", {})
        for key, value in llm_config.items():
            is_secret = "api_key" in key or "service_account" in key
            config_storage.set("llm_config", key, str(value), is_secret=is_secret, profile=profile_name)

        tool_configs = body.get("tool_configs", {})
        for tool_name, configs in tool_configs.items():
            for key, value in configs.items():
                is_secret = "secret" in key.lower() or "key" in key.lower() or "password" in key.lower()
                config_storage.set_tool_config(tool_name, key, str(value), is_secret=is_secret, profile=profile_name)

        # Generate and save token
        jwt_secret = config_storage.get("server_config", "jwt_secret") or BaseConfig.get_jwt_secret()
        if not jwt_secret:
            jwt_secret = secrets.token_urlsafe(32)
            config_storage.set("server_config", "jwt_secret", jwt_secret, is_secret=True)

        token, expires_at = _generate_token(jwt_secret, profile_name)

        return JSONResponse({
            "success": True,
            "token": token,
            "expires_at": expires_at,
            "profile": profile_name,
        })

    async def handle_reconfigure(request: Request) -> JSONResponse:
        """Reset setup status to allow reconfiguration from scratch.

        Requires admin auth. Does NOT delete profiles or data.
        """
        config_storage.delete("server_config", "setup_complete")
        return JSONResponse({"success": True, "message": "Setup status reset. Reload to reconfigure."})

    async def handle_reset_orphaned_setup(request: Request) -> JSONResponse:
        """Reset setup_complete when no profiles exist (orphaned setup state).

        No auth required, but ONLY works when setup_complete=true and zero
        visible profiles exist. This handles the edge case where the DB was
        partially wiped externally.
        """
        if not config_storage.is_setup_complete():
            return JSONResponse({"error": "Setup is not complete"}, status_code=400)

        profiles = await conversation_storage.list_profiles()
        visible = [p for p in profiles if not p["name"].startswith("__")]
        if len(visible) > 0:
            return JSONResponse(
                {"error": "Profiles still exist; use authenticated reconfigure instead"},
                status_code=403,
            )

        config_storage.delete("server_config", "setup_complete")
        return JSONResponse({"success": True, "message": "Orphaned setup state cleared."})

    async def handle_get_server_config(request: Request) -> JSONResponse:
        """Get server configuration (non-secret values). Requires auth."""
        config = config_storage.get_all("server_config", include_secrets=False)
        return JSONResponse({"config": config})

    async def handle_update_server_config(request: Request) -> JSONResponse:
        """Update server configuration. Requires auth (admin only)."""
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

        config = body.get("config", {})
        for key, value in config.items():
            is_secret = key in ("jwt_secret",)
            config_storage.set("server_config", key, str(value), is_secret=is_secret)

        return JSONResponse({"success": True})

    return [
        # Unauthenticated endpoints for setup and recovery
        Route("/api/config/setup-status", handle_setup_status, methods=["GET"]),
        Route("/api/config/setup", handle_setup, methods=["POST"]),
        Route("/api/config/reset-orphaned-setup", handle_reset_orphaned_setup, methods=["POST"]),
        # Authenticated endpoints
        Route("/api/config/server", handle_get_server_config, methods=["GET"]),
        Route("/api/config/server", handle_update_server_config, methods=["PUT"]),
        Route("/api/config/reconfigure", handle_reconfigure, methods=["POST"]),
    ]

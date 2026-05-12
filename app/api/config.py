"""Server configuration and setup API endpoints."""

import json
import os
import re
import secrets
from pathlib import Path

import jwt
from datetime import datetime, timezone, timedelta

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.api._auth import require_admin
from app.config.bootstrap import bootstrap_exists, write_bootstrap
from app.config.settings import BaseConfig, set_dynamic_config_storage
from app.config.setup_profiles import get_active_setup_profile_id, list_setup_profiles
from app.runtime import BootedState, get_state
from app.storage import (
    get_conversation_storage,
    get_dynamic_config_storage,
    invalidate_storage_singletons,
)
from app.storage.dynamic_config_storage import DynamicConfigStorage
from app.storage.conversation_storage import ConversationStorage
from app.utils.logger import logger
from app.utils.persona import ensure_persona_file

# Server-config keys that may not be set via the generic ``server_config``
# write path during the wizard. The DB-provider choice itself lives in
# bootstrap.toml (chicken-and-egg: we have to know the provider before we
# can connect to any DB), so it must never leak into the SQLite/Postgres
# ``server_config`` table.
_BOOTSTRAP_ONLY_KEYS = {"db_provider", "postgres"}


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


def get_config_routes(state: BootedState) -> list[Route]:
    """Setup-wizard / server-config endpoints.

    Always registered — pre-storage too — so the Setup Wizard can run before
    any DB file exists. Handlers that need post-storage objects resolve them
    via ``state`` at call time. Endpoints that strictly require storage
    return 503 with ``"Setup is not complete"`` while ``state.storage_ready``
    is False.

    The DB-provider choice in ``server_config`` is what flips the server
    out of deferred-storage mode: :func:`handle_setup` validates the choice,
    writes ``bootstrap.toml``, then calls ``state.boot_fn`` to materialise
    storage, register the rest of the API routes, and start channel
    adapters in-process.
    """

    def _require_storage() -> JSONResponse | None:
        if not state.storage_ready:
            return JSONResponse(
                {"error": "Setup is not complete"}, status_code=503,
            )
        return None

    async def handle_setup_profiles(request: Request) -> JSONResponse:
        """Return the wizard's environment-preset catalogue plus the active id.

        Public (pre-auth) like ``/api/config/setup-status`` so the wizard can
        fetch this before any token exists.

        ``selected`` is the preset id taken from ``SETUP_WIZARD_ENV`` (read
        fresh from the process environment, populated from the project's
        ``.env`` file at startup). ``null`` when the variable is unset, blank,
        or names a profile that doesn't exist — in that case the wizard
        falls back to its built-in component-level defaults.

        The preset is purely a pre-fill hint: the wizard seeds form values
        from it, but every field stays editable so the operator can override
        anything before submitting.
        """
        try:
            profiles = list_setup_profiles()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Failed to load setup profiles: {exc}")
            profiles = []
        try:
            selected = get_active_setup_profile_id()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Failed to resolve active setup profile: {exc}")
            selected = None
        return JSONResponse({"profiles": profiles, "selected": selected})

    async def handle_setup_status(request: Request) -> JSONResponse:
        """Check if first-time setup has been completed. No auth required.

        Also accepts ?profile=xxx to check if a specific profile exists.

        In deferred-storage mode (no ``bootstrap.toml`` yet — Setup Wizard
        hasn't run) this answers ``setup_complete=false`` without touching
        the database, because doing so would create a SQLite file before
        the user has picked a backend.
        """
        profile = request.query_params.get("profile")
        if not state.storage_ready:
            result = {"setup_complete": False, "storage_ready": False}
            if profile:
                result["profile_exists"] = False
            return JSONResponse(result)

        config_storage = state.config_storage
        conversation_storage = state.conversation_storage
        setup_complete = config_storage.is_setup_complete()
        result = {
            "setup_complete": setup_complete,
            "storage_ready": True,
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

    async def _persist_db_provider_choice(server_config: dict) -> str | None:
        """Validate the wizard's ``db_provider`` choice and write bootstrap.toml.

        Returns ``None`` on success or an error string for the caller to
        surface as a 400 response. Three cases:

        - ``bootstrap.toml`` already exists: the provider choice is locked.
          Silently drop any disagreement in the body. The wizard UI hides
          the field after first setup, but treat the server as authoritative.
        - ``db_provider=sqlite`` (or missing): just write bootstrap.toml and
          rebuild the singleton from it. No connection to validate.
        - ``db_provider=postgres``: build a candidate provider, verify the
          connection, then write bootstrap.toml. Only persist if validation
          passed — a failed Postgres check must leave the working dir
          untouched so the next attempt re-enters deferred mode cleanly.

        Storage initialization (migrations, tool persistence, agent build)
        is the caller's job — see :func:`handle_setup`, which invokes
        ``state.boot_fn`` after this returns.
        """
        from app.databases import (
            create_database_provider,
            set_database_provider,
        )
        from app.databases.postgres import PostgresDatabaseProvider

        requested = (server_config.get("db_provider") or "sqlite").strip().lower()
        if requested not in ("sqlite", "postgres"):
            return f"Unknown db_provider: {requested!r}"

        bootstrap_path = Path(BaseConfig.OPENPA_WORKING_DIR) / "bootstrap.toml"
        if bootstrap_path.exists():
            # Choice is locked; silently ignore any disagreement.
            return None

        if requested == "postgres":
            pg_in = server_config.get("postgres") or {}
            required = ("host", "database", "user")
            missing = [k for k in required if not pg_in.get(k)]
            if missing:
                return f"Postgres connection requires: {', '.join(missing)}"

            # Validate the connection BEFORE touching the working dir, so
            # a failed Postgres check leaves bootstrap.toml unchanged.
            candidate = PostgresDatabaseProvider(
                host=pg_in["host"],
                port=int(pg_in.get("port") or 5432),
                database=pg_in["database"],
                user=pg_in["user"],
                password=pg_in.get("password", ""),
                sslmode=pg_in.get("sslmode", "prefer"),
            )
            try:
                await candidate.health_check()
            except Exception as exc:  # noqa: BLE001
                await candidate.dispose()
                return f"Could not connect to PostgreSQL: {exc}"
            await candidate.dispose()

            write_bootstrap({
                "db_provider": "postgres",
                "postgres": {
                    "host": pg_in["host"],
                    "port": int(pg_in.get("port") or 5432),
                    "database": pg_in["database"],
                    "user": pg_in["user"],
                    "password": pg_in.get("password", ""),
                    "sslmode": pg_in.get("sslmode", "prefer"),
                },
            })
        else:
            # SQLite path: nothing to validate, just lock in the choice.
            write_bootstrap({"db_provider": "sqlite"})

        # Rebuild the provider singleton from the freshly-written
        # bootstrap.toml. Clear caches so the next ``get_*_storage()`` call
        # builds against the new provider. In deferred mode no engine was
        # ever materialised, so there's nothing to dispose; in the legacy
        # reconfigure path the caller's lock check above already short-
        # circuited.
        set_database_provider(None)
        invalidate_storage_singletons()
        set_database_provider(create_database_provider())

        logger.info(f"Database provider configured: {requested} (first-setup).")
        return None

    async def handle_setup(request: Request) -> JSONResponse:
        """Complete setup for a profile.

        First profile (admin): unauthenticated — this is the bootstrap
        window before any JWT can possibly exist. Validates the DB-provider
        choice, writes ``bootstrap.toml``, runs the deferred boot
        (storage init + tool registration + agent build) **if it hasn't run
        yet**, then saves server / LLM / tool config, creates the profile,
        generates the token, and marks setup complete.

        Subsequent profiles: requires the admin profile's JWT. Creates
        the profile, saves LLM and tool configs, and generates a token.
        Server-level config cannot be changed for non-first profiles.

        Expects JSON body with:
        - profile: str (required) — the profile name to create
        - server_config: dict of server settings (first setup only)
        - llm_config: dict of LLM settings
        - tool_configs: dict of {tool_name: {key: value}}
        """
        # In deferred-storage mode (no ``bootstrap.toml`` yet), this is a
        # first-setup by definition — there is no DB to ask. Once storage
        # is up, defer to the persisted ``setup_complete`` flag.
        if not state.storage_ready:
            is_first_setup = True
        else:
            is_first_setup = not state.config_storage.is_setup_complete()

        # Only the admin can onboard additional profiles. The first-run
        # bootstrap is intentionally unauthenticated because no JWT can
        # exist yet.
        if not is_first_setup:
            denied = require_admin(request)
            if denied is not None:
                return denied

        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

        profile_name = body.get("profile", "").strip()

        # Validate profile name
        validation_error = _validate_profile_name(profile_name)
        if validation_error:
            return JSONResponse({"error": validation_error}, status_code=400)

        # First setup must be 'admin'
        if is_first_setup and profile_name != "admin":
            return JSONResponse(
                {"error": "First profile must be named 'admin'"},
                status_code=400,
            )

        # ── Database provider selection ──────────────────────────────────
        # First-setup-only. Validate the requested DB provider (Postgres
        # connection check if applicable), write bootstrap.toml, then run
        # the deferred boot to materialise storage and the rest of the
        # API. After this returns, all subsequent writes in this handler
        # land in the chosen DB.
        #
        # For non-first-setup calls, any db_provider field in the body is
        # silently ignored — the choice was made on first setup and is
        # locked. Other profiles can never see or change it.
        if is_first_setup:
            err = await _persist_db_provider_choice(body.get("server_config", {}) or {})
            if err is not None:
                return JSONResponse({"error": err}, status_code=400)

            # Run the deferred boot if we haven't yet. ``state.boot_fn`` is
            # idempotent (guarded by ``state.boot_lock`` + ``storage_ready``),
            # so this is also safe on the legacy reconfigure-from-already-
            # booted path.
            if not state.storage_ready and state.boot_fn is not None:
                try:
                    await state.boot_fn()
                except Exception as exc:  # noqa: BLE001
                    logger.exception("Deferred storage boot failed during setup")
                    # Roll back ``bootstrap.toml`` so the next attempt
                    # re-enters deferred mode cleanly. Best-effort —
                    # surface a 500 either way.
                    try:
                        (Path(BaseConfig.OPENPA_WORKING_DIR) / "bootstrap.toml").unlink()
                    except OSError:
                        pass
                    from app.databases import set_database_provider as _sdp
                    _sdp(None)
                    invalidate_storage_singletons()
                    state.reset_storage()
                    return JSONResponse(
                        {"error": f"Failed to initialize storage: {exc}"},
                        status_code=500,
                    )

        # Resolve storage handles from the now-booted state.
        if not state.storage_ready:
            # Defensive: only reachable if ``boot_fn`` is missing entirely
            # (misconfigured server). Avoid an opaque AttributeError below.
            return JSONResponse(
                {"error": "Setup is not complete"}, status_code=503,
            )
        config_storage = state.config_storage
        conversation_storage = state.conversation_storage
        registry = state.registry
        on_first_setup = state.on_first_setup

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

        # Create profile directory and copy PERSONA.md template
        ensure_persona_file(profile_name)

        # Save server config only on first setup
        if is_first_setup:
            server_config = body.get("server_config", {})
            for key, value in server_config.items():
                # The DB-provider keys live in bootstrap.toml, never in the
                # server_config table — they were applied above.
                if key in _BOOTSTRAP_ONLY_KEYS:
                    continue
                is_secret = key in ("jwt_secret",)
                config_storage.set("server_config", key, str(value), is_secret=is_secret)

            # Ensure the User Working Directory is recorded with a sensible
            # default so all built-in tools have a writable active path on
            # first run, even if the wizard payload didn't include it.
            user_working_dir = server_config.get("user_working_dir")
            if not user_working_dir:
                user_working_dir = os.path.join(os.path.expanduser("~"), "Documents")
                config_storage.set("server_config", "user_working_dir", user_working_dir)
            expanded_uwd = os.path.expanduser(user_working_dir) if user_working_dir.startswith("~") else user_working_dir
            try:
                os.makedirs(expanded_uwd, exist_ok=True)
            except OSError as exc:
                logger.warning(f"Could not create user working directory {expanded_uwd!r}: {exc}")

            # Generate JWT secret if not provided
            jwt_secret = config_storage.get("server_config", "jwt_secret")
            if not jwt_secret and not BaseConfig.get_jwt_secret():
                jwt_secret = secrets.token_urlsafe(32)
                config_storage.set("server_config", "jwt_secret", jwt_secret, is_secret=True)

            # Vector embedding configuration. Stored in server_config because
            # the model loads once into the OpenPA process and is shared
            # across all profiles. Only persisted on first setup.
            from app.lib.embedding_lifecycle import persist_embedding_config
            try:
                persist_embedding_config(body.get("embedding_config") or {}, config_storage)
            except ValueError as e:
                return JSONResponse({"error": str(e)}, status_code=400)

            config_storage.mark_setup_complete()

        # Save LLM and tool configs for ALL profiles (including first setup)
        # (must happen before on_first_setup so tool enabled states are persisted)
        llm_config = body.get("llm_config", {})
        for key, value in llm_config.items():
            is_secret = (
                "api_key" in key
                or "service_account" in key
                or "setup_token" in key
                or "oauth_token" in key
                or "bearer_token" in key
            )
            # auth_method selections are not secrets
            if key.endswith(".auth_method") or key == "auth_method":
                is_secret = False
            config_storage.set("llm_config", key, str(value), is_secret=is_secret, profile=profile_name)

        # Tool config payload from the wizard. The frontend bundles four
        # different settings into a single ``tool_configs[tool_id]`` dict using
        # reserved key prefixes:
        #   - ``_enabled``         -> per-profile enabled state (profile_tools)
        #   - ``_full_reasoning``  -> LLM scope override
        #   - ``_arg.<name>``      -> tool argument
        #   - everything else      -> tool variable (env-style secret/value)
        # Route each to the right storage; otherwise these settings would be
        # silently dropped into the variables table and never honored.
        tool_configs = body.get("tool_configs", {})
        if registry is not None:
            from app.tools.ids import slugify
            for tool_key, configs in tool_configs.items():
                # Tool keys may arrive as either tool_id (slug) or display name
                tool_id = tool_key if registry.get(tool_key) else slugify(tool_key)
                arg_values: dict[str, object] = {}
                for key, value in configs.items():
                    if key == "_enabled":
                        try:
                            registry.set_profile_tool_enabled(
                                profile_name, tool_id,
                                str(value).lower() == "true",
                            )
                        except (KeyError, ValueError) as e:
                            logger.warning(
                                f"Skipping _enabled for '{tool_id}': {e}"
                            )
                        continue
                    if key == "_full_reasoning":
                        registry.config.set_llm_param(
                            tool_id, profile_name, "full_reasoning",
                            str(value).lower() == "true",
                        )
                        continue
                    if key.startswith("_arg."):
                        arg_name = key[len("_arg."):]
                        try:
                            arg_values[arg_name] = json.loads(value)
                        except (TypeError, ValueError):
                            arg_values[arg_name] = value
                        continue
                    is_secret = (
                        "secret" in key.lower()
                        or "key" in key.lower()
                        or "password" in key.lower()
                    )
                    registry.config.set_variable(
                        tool_id, profile_name, key, str(value), is_secret=is_secret,
                    )
                if arg_values:
                    registry.config.set_arguments(tool_id, profile_name, arg_values)

        # Per-built-in-tool LLM overrides land in the llm scope of the new tool_configs table.
        agent_configs = body.get("agent_configs", {})
        if registry is not None and agent_configs:
            from app.tools.ids import slugify
            for tool_key, config in agent_configs.items():
                tool_id = tool_key if registry.get(tool_key) else slugify(tool_key)
                for key in ("llm_provider", "llm_model", "reasoning_effort", "full_reasoning"):
                    if key in config and config[key] is not None:
                        registry.config.set_llm_param(
                            tool_id, profile_name, key, config[key],
                        )
                if "system_prompt" in config and config["system_prompt"]:
                    registry.config.set_meta(
                        tool_id, profile_name, "system_prompt", config["system_prompt"],
                    )
                if "description" in config and config["description"]:
                    registry.config.set_meta(
                        tool_id, profile_name, "description", config["description"],
                    )

        # Channels declared during the setup wizard. Validation + creation
        # mirror POST /api/channels but happen pre-token because the setup
        # endpoint is the bootstrap entry point. A single bad channel must
        # not block account creation; per-entry errors are accumulated and
        # echoed back in the response so the wizard can surface them
        # alongside the successfully-created channels.
        channel_configs = body.get("channel_configs") or []
        created_channels: list[dict] = []
        channel_errors: list[dict] = []
        if isinstance(channel_configs, list) and channel_configs:
            from app.api.channels import (
                _decorate,
                _redact,
                create_channel_for_profile,
            )
            from app.config import load_all_channel_catalogs

            channels_catalog = load_all_channel_catalogs()
            for entry in channel_configs:
                if not isinstance(entry, dict):
                    continue
                ch, err = await create_channel_for_profile(
                    conversation_storage, profile_name, entry,
                )
                if err is not None:
                    channel_errors.append({
                        "channel_type": entry.get("channel_type"),
                        "error": err["error"],
                    })
                elif ch is not None:
                    created_channels.append(_decorate(_redact(ch, channels_catalog)))

        # Generate and save token
        jwt_secret = config_storage.get("server_config", "jwt_secret") or BaseConfig.get_jwt_secret()
        if not jwt_secret:
            jwt_secret = secrets.token_urlsafe(32)
            config_storage.set("server_config", "jwt_secret", jwt_secret, is_secret=True)

        token, expires_at = _generate_token(jwt_secret, profile_name)

        # Persist token to disk for recovery
        tokens_dir = Path(BaseConfig.OPENPA_WORKING_DIR) / "tokens"
        tokens_dir.mkdir(parents=True, exist_ok=True)
        token_file = tokens_dir / f"{profile_name}.token"
        token_file.write_text(token, encoding="utf-8")
        logger.info(f"Token saved to {token_file}")

        # Initialize built-in tools that were skipped at startup due to missing LLM
        if is_first_setup and on_first_setup:
            try:
                await on_first_setup(profile_name)
            except Exception as e:
                logger.warning(f"Post-setup built-in tool initialization failed: {e}")

        # If first setup just turned Vector Embedding on, kick off model
        # load + rebuild in the background so the agent becomes usable
        # without requiring a server restart. The wizard polls
        # /api/config/embedding/status to know when it's safe to proceed.
        if is_first_setup and BaseConfig.is_embedding_enabled():
            from app.config.embedding_state import embedding_state
            from app.lib.embedding_lifecycle import apply_embedding_config_in_background

            if not embedding_state.is_busy() and not embedding_state.is_ready():
                # On first setup the only profile that exists is the one
                # we just created, so we know what to rebuild.
                apply_embedding_config_in_background(
                    profiles=[profile_name], force_rebuild=True,
                )
                logger.info(
                    "Vector embedding initialization started in background after first setup."
                )

        from app.events.settings_state_bus import publish_settings_state_changed
        publish_settings_state_changed(profile_name)

        return JSONResponse({
            "success": True,
            "token": token,
            "expires_at": expires_at,
            "profile": profile_name,
            "embedding_enabled": BaseConfig.is_embedding_enabled(),
            "channels": created_channels,
            "channel_errors": channel_errors,
        })

    async def handle_reconfigure(request: Request) -> JSONResponse:
        """Reset setup status to allow reconfiguration from scratch.

        Requires admin auth. Does NOT delete profiles or data.
        """
        if (gate := _require_storage()):
            return gate
        state.config_storage.delete("server_config", "setup_complete")
        return JSONResponse({"success": True, "message": "Setup status reset. Reload to reconfigure."})

    async def handle_reset_orphaned_setup(request: Request) -> JSONResponse:
        """Reset setup_complete when no profiles exist (orphaned setup state).

        No auth required, but ONLY works when setup_complete=true and zero
        visible profiles exist. This handles the edge case where the DB was
        partially wiped externally.
        """
        if (gate := _require_storage()):
            return gate
        config_storage = state.config_storage
        conversation_storage = state.conversation_storage
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

    async def handle_embedding_status(request: Request) -> JSONResponse:
        """Report the lifecycle state of the vector embedding subsystem.

        Unauthenticated so the setup wizard can poll while the user is
        still pre-token. Returns ``enabled`` (config flag) plus the
        current load ``status`` and any error from the last attempt.
        """
        from app.config.embedding_state import embedding_state
        return JSONResponse({
            "enabled": BaseConfig.is_embedding_enabled(),
            **embedding_state.to_dict(),
        })

    async def handle_embedding_initialize(request: Request) -> JSONResponse:
        """Trigger an asynchronous load + rebuild of the embedding subsystem.

        Used by the setup wizard right after ``handle_setup`` saved the
        new ``embedding_config``. Returns immediately with ``status =
        initializing``; the wizard polls ``handle_embedding_status``
        until ready or failed.

        If embedding is disabled this returns the disabled state. If a
        busy operation (initialize or rebuild) is already in flight,
        returns its current state without scheduling a duplicate.
        """
        from app.config.embedding_state import embedding_state
        from app.lib.embedding_lifecycle import apply_embedding_config_in_background

        if not BaseConfig.is_embedding_enabled():
            embedding_state.mark_disabled()
            return JSONResponse({
                "enabled": False,
                **embedding_state.to_dict(),
            })

        if embedding_state.is_busy() or embedding_state.is_ready():
            return JSONResponse({
                "enabled": True,
                **embedding_state.to_dict(),
            })

        if (gate := _require_storage()):
            return gate
        profile_names = [
            row["name"] for row in await state.conversation_storage.list_profiles()
            if not row["name"].startswith("__")
        ]
        apply_embedding_config_in_background(profiles=profile_names, force_rebuild=True)

        return JSONResponse({
            "enabled": True,
            **embedding_state.to_dict(),
        }, status_code=202)

    async def handle_get_embedding_config(request: Request) -> JSONResponse:
        """Return the persisted embedding config (admin only).

        Powers the Embedding Settings panel — secret values like
        ``hf_token`` and the vector-store API keys come back unmasked
        because the user is the admin and is editing them directly.
        """
        denied = require_admin(request)
        if denied is not None:
            return denied
        if (gate := _require_storage()):
            return gate
        from app.config.embedding_state import embedding_state
        from app.lib.embedding_lifecycle import read_embedding_config
        return JSONResponse({
            "config": read_embedding_config(state.config_storage),
            **embedding_state.to_dict(),
        })

    async def handle_put_embedding_config(request: Request) -> JSONResponse:
        """Persist an updated embedding config and trigger reload + rebuild.

        Admin-only. Body shape mirrors the wizard's ``embedding_config``.
        Returns 202 with the new status; the UI polls
        ``/api/config/embedding/status`` until ready or failed. While
        busy, the agent refuses to run.
        """
        denied = require_admin(request)
        if denied is not None:
            return denied
        if (gate := _require_storage()):
            return gate

        from app.config.embedding_state import embedding_state
        from app.lib.embedding_lifecycle import (
            apply_embedding_config_in_background,
            persist_embedding_config,
        )

        if embedding_state.is_busy():
            return JSONResponse(
                {
                    "error": (
                        "Embedding subsystem is currently "
                        f"{embedding_state.status.value}; please wait."
                    ),
                    **embedding_state.to_dict(),
                },
                status_code=409,
            )

        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

        try:
            persist_embedding_config(body, state.config_storage)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)

        profile_names = [
            row["name"] for row in await state.conversation_storage.list_profiles()
            if not row["name"].startswith("__")
        ]
        apply_embedding_config_in_background(profiles=profile_names, force_rebuild=True)

        return JSONResponse(
            {"success": True, **embedding_state.to_dict()},
            status_code=202,
        )

    async def handle_channel_catalog(request: Request) -> JSONResponse:
        """Public catalog of supported channel types.

        Used by the setup wizard before any JWT exists. Mirrors the payload
        shape of the auth-guarded ``/api/channels/catalog`` — the data is
        static metadata loaded from ``app/config/channels/*.toml`` (platform
        names, supported modes, field definitions), so exposing it pre-auth
        is safe and keeps the auth boundary on ``/api/channels/*`` untouched.
        """
        from app.config import load_all_channel_catalogs
        return JSONResponse({"channels": load_all_channel_catalogs()})

    async def handle_get_server_config(request: Request) -> JSONResponse:
        """Get server configuration (non-secret values). Requires auth."""
        if (gate := _require_storage()):
            return gate
        config = state.config_storage.get_all("server_config", include_secrets=False)
        return JSONResponse({"config": config})

    async def handle_update_server_config(request: Request) -> JSONResponse:
        """Update server configuration. Requires auth (admin only)."""
        if (gate := _require_storage()):
            return gate
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

        config = body.get("config", {})
        cfg_storage = state.config_storage
        for key, value in config.items():
            # The DB-provider choice can only change during the very first
            # setup wizard. Silently drop any later attempt to override it
            # via this endpoint — the value still lives in bootstrap.toml
            # untouched.
            if key in _BOOTSTRAP_ONLY_KEYS:
                continue
            is_secret = key in ("jwt_secret",)
            cfg_storage.set("server_config", key, str(value), is_secret=is_secret)

        return JSONResponse({"success": True})

    return [
        # Unauthenticated endpoints for setup and recovery
        Route("/api/config/setup-profiles", handle_setup_profiles, methods=["GET"]),
        Route("/api/config/setup-status", handle_setup_status, methods=["GET"]),
        Route("/api/config/setup", handle_setup, methods=["POST"]),
        Route("/api/config/reset-orphaned-setup", handle_reset_orphaned_setup, methods=["POST"]),
        Route("/api/config/embedding/status", handle_embedding_status, methods=["GET"]),
        Route("/api/config/embedding/initialize", handle_embedding_initialize, methods=["POST"]),
        Route("/api/config/channel-catalog", handle_channel_catalog, methods=["GET"]),
        # Authenticated endpoints
        Route("/api/config/server", handle_get_server_config, methods=["GET"]),
        Route("/api/config/server", handle_update_server_config, methods=["PUT"]),
        Route("/api/config/embedding", handle_get_embedding_config, methods=["GET"]),
        Route("/api/config/embedding", handle_put_embedding_config, methods=["PUT"]),
        Route("/api/config/reconfigure", handle_reconfigure, methods=["POST"]),
    ]

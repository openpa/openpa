"""Tool management API.

Replaces the old per-type ``/api/tools`` + ``/api/agents`` + ``/api/mcp-servers``
endpoints with a unified surface keyed by ``tool_id``. Built-in and skill
configuration is also exposed here.
"""

from __future__ import annotations

import asyncio
import json
import urllib.parse

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.config.settings import BaseConfig
from app.lib.llm.factory import create_llm_provider
from app.tools import ToolRegistry, ToolType
from app.tools.builtin import (
    BuiltInToolGroup,
    get_builtin_tool_config,
    refresh_builtin_tool_oauth,
)
from app.utils.logger import logger


def _profile_from_request(request: Request) -> str:
    return getattr(request.user, "username", "admin")


def get_tool_routes(
    registry: ToolRegistry,
    config_storage=None,
    connect_persisted_tool=None,
) -> list[Route]:

    config_manager = registry.config

    async def handle_list_tools(request: Request) -> JSONResponse:
        """List all tools visible to the profile (excluding hidden intrinsic tools).

        Each row carries enough metadata for the dashboard to render the
        configuration form without an extra round-trip:
        - ``required_fields`` -- built-in tool required_config schema
        - ``config`` -- current per-profile values (variables/arguments/llm/meta)
        - ``full_reasoning`` -- mirrored from ``config.llm.full_reasoning`` so
          the frontend doesn't have to know about the scoping
        """
        profile = request.query_params.get("profile") or _profile_from_request(request)
        rows = registry.visible_for_profile(profile)
        enriched: list[dict] = []
        for row in rows:
            tool = registry.get(row["tool_id"])
            schema = _schema_for_tool(tool)
            required_fields = schema.get("tool", {}).get("required_config", {})
            snapshot = config_manager.snapshot(row["tool_id"], profile)
            row.update({
                "configured": _is_tool_configured(tool, snapshot),
                "config": snapshot,
                "required_fields": required_fields,
                "full_reasoning": bool(snapshot.get("llm", {}).get("full_reasoning", False)),
            })
            if hasattr(tool, "connection_error") and getattr(tool, "connection_error"):
                row["connection_error"] = tool.connection_error
                row["is_stub"] = True
            else:
                row["is_stub"] = bool(getattr(tool, "is_stub", False))
            if hasattr(tool, "is_llm_bound"):
                row["llm_bound"] = bool(tool.is_llm_bound)
            if hasattr(tool, "url"):
                row["url"] = tool.url
            if hasattr(tool, "owner_profile"):
                row["owner_profile"] = tool.owner_profile
            enriched.append(row)
        return JSONResponse({"tools": enriched})

    async def handle_get_tool(request: Request) -> JSONResponse:
        profile = request.query_params.get("profile") or _profile_from_request(request)
        tool_id = request.path_params["tool_id"]
        tool = registry.get(tool_id)
        if tool is None:
            return JSONResponse({"error": f"Tool '{tool_id}' not found"}, status_code=404)
        schema = _schema_for_tool(tool)
        snapshot = config_manager.snapshot(tool_id, profile)
        return JSONResponse({
            "tool_id": tool_id,
            "name": tool.name,
            "tool_type": tool.tool_type.value,
            "description": tool.description,
            "arguments_schema": tool.arguments_schema,
            "schema": schema,
            "config": snapshot,
            "configured": _is_tool_configured(tool, snapshot),
        })

    async def handle_set_variables(request: Request) -> JSONResponse:
        """Update Tool Variables (env-style secrets) for a tool."""
        profile = _profile_from_request(request)
        tool_id = request.path_params["tool_id"]
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
        variables = body.get("variables", {})
        if not isinstance(variables, dict):
            return JSONResponse({"error": "'variables' must be an object"}, status_code=400)

        tool = registry.get(tool_id)
        schema = _schema_for_tool(tool) if tool else {}
        required_config = schema.get("tool", {}).get("required_config", {})

        for key, value in variables.items():
            field_spec = required_config.get(key, {})
            is_secret = bool(
                field_spec.get("secret")
                or "secret" in key.lower()
                or "key" in key.lower()
                or "password" in key.lower()
            )
            config_manager.set_variable(
                tool_id, profile, key, str(value), is_secret=is_secret,
            )

        # Refresh OAuth client for built-in tools that just got their credentials
        if tool and tool.tool_type is ToolType.BUILTIN and isinstance(tool, BuiltInToolGroup):
            refresh_builtin_tool_oauth(registry, config_manager, tool_id, profile=profile)

        # Mirror skill variables to ``{skill_dir}/scripts/.env`` so the skill's
        # scripts can source them at runtime — the agent has no per-skill hook
        # to prepare the environment for the generic exec_shell tool.
        if tool and tool.tool_type is ToolType.SKILL:
            _write_skill_env_file(tool, config_manager, profile)

        return JSONResponse({"success": True})

    async def handle_set_arguments(request: Request) -> JSONResponse:
        """Update Tool Arguments (JSON-Schema parameter values)."""
        profile = _profile_from_request(request)
        tool_id = request.path_params["tool_id"]
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
        arguments = body.get("arguments", {})
        if not isinstance(arguments, dict):
            return JSONResponse({"error": "'arguments' must be an object"}, status_code=400)
        config_manager.set_arguments(tool_id, profile, arguments)
        return JSONResponse({"success": True})

    async def handle_set_llm_params(request: Request) -> JSONResponse:
        """Update LLM Parameters (provider, model, full_reasoning, reasoning_effort)."""
        profile = _profile_from_request(request)
        tool_id = request.path_params["tool_id"]
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
        llm_params = body.get("llm", {})
        if not isinstance(llm_params, dict):
            return JSONResponse({"error": "'llm' must be an object"}, status_code=400)

        for key, value in llm_params.items():
            config_manager.set_llm_param(tool_id, profile, key, value)

        tool = registry.get(tool_id)

        # If provider/model/reasoning_effort changed, rebuild the adapter's LLM.
        # Read the *post-write* DB values so partial updates (e.g. only
        # reasoning_effort in the body) are merged with the existing settings.
        if tool and hasattr(tool, "update_runtime_config") and any(
            k in llm_params for k in ("llm_provider", "llm_model", "reasoning_effort")
        ):
            try:
                merged = config_manager.get_llm_params(tool_id, profile)
                provider_name = merged.get("llm_provider") or None
                model_name = merged.get("llm_model") or None

                # When no per-tool model override, fall back to the "low"
                # model group (same source as startup in server.py).
                if not model_name:
                    group_value = BaseConfig.get_model_group("low", profile=profile)
                    if group_value:
                        parts = group_value.split("/", 1)
                        if not provider_name:
                            provider_name = parts[0]
                        model_name = parts[1] if len(parts) > 1 else parts[0]

                if not provider_name:
                    provider_name = BaseConfig.get_default_provider(profile=profile)

                if model_name:
                    new_llm = create_llm_provider(
                        provider_name=provider_name,
                        model_name=model_name,
                        config_storage=config_storage,
                        profile=profile,
                        default_reasoning_effort=merged.get("reasoning_effort"),
                    )
                    tool.update_runtime_config(llm=new_llm)
                else:
                    logger.warning(
                        f"No model resolved for tool '{tool_id}'; skipping LLM rebuild"
                    )
            except ValueError as e:
                return JSONResponse({"error": f"Invalid LLM: {e}"}, status_code=400)
            except Exception as e:  # noqa: BLE001
                logger.exception(
                    f"Failed to apply LLM change to running tool '{tool_id}'"
                )
                return JSONResponse(
                    {"error": f"Failed to apply LLM change: {e}"}, status_code=500,
                )

        # If full_reasoning changed, propagate to the running adapter
        if tool and "full_reasoning" in llm_params and hasattr(tool, "update_runtime_config"):
            tool.update_runtime_config(full_reasoning=bool(llm_params["full_reasoning"]))

        return JSONResponse({"success": True})

    async def handle_set_enabled(request: Request) -> JSONResponse:
        """Enable / disable an A2A or MCP tool for the current profile.

        On enable, if the tool is currently a stub (lazy-init placeholder or
        previous connection failure), schedule a background connect attempt.
        The HTTP response returns immediately so the UI toggle stays snappy;
        the next ``GET /api/tools`` will reflect the new connection state.
        """
        profile = _profile_from_request(request)
        tool_id = request.path_params["tool_id"]
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
        enabled = body.get("enabled")
        if enabled is None:
            return JSONResponse({"error": "'enabled' field is required"}, status_code=400)
        try:
            registry.set_profile_tool_enabled(profile, tool_id, bool(enabled))
        except KeyError:
            return JSONResponse({"error": f"Tool '{tool_id}' not found"}, status_code=404)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)

        if bool(enabled) and connect_persisted_tool is not None:
            tool = registry.get(tool_id)
            if tool is not None and getattr(tool, "is_stub", False):
                asyncio.create_task(connect_persisted_tool(tool_id))

        return JSONResponse({"success": True, "enabled": bool(enabled)})

    return [
        Route("/api/tools", handle_list_tools, methods=["GET"]),
        Route("/api/tools/{tool_id}", handle_get_tool, methods=["GET"]),
        Route("/api/tools/{tool_id}/variables", handle_set_variables, methods=["PUT"]),
        Route("/api/tools/{tool_id}/arguments", handle_set_arguments, methods=["PUT"]),
        Route("/api/tools/{tool_id}/llm", handle_set_llm_params, methods=["PUT"]),
        Route("/api/tools/{tool_id}/enabled", handle_set_enabled, methods=["PUT"]),
    ]


# ── helpers ────────────────────────────────────────────────────────────────


def _schema_for_tool(tool) -> dict:
    """Return the static config schema for a built-in tool or skill, or {} for others.

    Skills declare their environment variables via ``metadata.environment_variables``
    in ``SKILL.md`` (a list of variable names). They are surfaced through the same
    ``required_config`` shape used by built-in tools so the existing UI/save flow
    works without further branching.
    """
    if tool is None:
        return {}
    if tool.tool_type is ToolType.BUILTIN and isinstance(tool, BuiltInToolGroup):
        return get_builtin_tool_config(tool.config_name)
    if tool.tool_type is ToolType.SKILL:
        names = getattr(tool, "environment_variables", []) or []
        if not names:
            return {}
        required = {
            name: {"description": name, "type": "string", "secret": False}
            for name in names
        }
        return {"tool": {"required_config": required}}
    return {}


def _escape_env_value(value: str) -> str:
    """Quote a value for safe inclusion in a ``.env`` file.

    Wraps in double quotes and escapes embedded ``"`` / ``\\`` if the value
    contains whitespace, ``#``, or quotes; otherwise returns it as-is.
    """
    if value == "":
        return ""
    needs_quoting = any(ch in value for ch in (" ", "\t", "\n", "\r", "#", '"', "'", "\\", "$"))
    if not needs_quoting:
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _write_skill_env_file(tool, config_manager, profile: str) -> None:
    """Mirror persisted skill variables to ``{skill_dir}/scripts/.env``.

    Overwrites the file so deletions in the DB also disappear from disk. Only
    variables declared in the skill's ``environment_variables`` are written —
    stray DB rows are ignored.
    """
    try:
        scripts_dir = tool.info.dir_path / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        env_path = scripts_dir / ".env"
        all_vars = config_manager.get_variables(
            tool.tool_id, profile, include_secrets=True,
        )
        declared = set(getattr(tool, "environment_variables", []) or [])
        lines = [
            f"{k}={_escape_env_value(str(v))}"
            for k, v in all_vars.items()
            if k in declared and v != ""
        ]
        body = "\n".join(lines) + ("\n" if lines else "")
        env_path.write_text(body, encoding="utf-8")
    except Exception:  # noqa: BLE001
        logger.exception(
            f"Failed to write .env for skill '{getattr(tool, 'tool_id', '?')}'"
        )


def _is_tool_configured(tool, snapshot: dict) -> bool:
    """Return True if all required variables are populated for this tool."""
    schema = _schema_for_tool(tool)
    required = schema.get("tool", {}).get("required_config", {})
    if not required:
        return True
    have = snapshot.get("variables", {})
    return all(have.get(k) for k in required)

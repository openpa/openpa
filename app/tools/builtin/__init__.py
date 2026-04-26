"""Built-in tools (in-process, child-LLM-driven).

Each tool group lives at ``app.tools.builtin.{module_name}`` and exports:

- ``TOOL_CONFIG`` (dict)              -- static config (name, display_name,
                                         default_model_group, required_config,
                                         optional ``arguments`` / ``oauth`` /
                                         ``llm_parameters``). ``llm_parameters``
                                         carries code-level defaults for the
                                         tool's tool_instructions, system
                                         prompt, provider/model, reasoning
                                         effort and full_reasoning. DB overrides
                                         (from the ``llm``/``meta`` tool-config
                                         scopes) win per-key.
- ``SERVER_NAME`` (str)               -- display name in the UI
- ``get_tools(config: dict) -> list[BuiltInTool]`` -- in-process functions
- ``get_prepare_tools()`` (optional)  -- per-request tool customization callback
- ``_make_server_instructions(working_dir: str)`` (optional) -- dynamic
                                         override for ``tool_instructions``
                                         (wins over ``TOOL_CONFIG["llm_parameters"]["tool_instructions"]``)

Registration is driven by the explicit ``_BUILTIN_MODULE_NAMES`` tuple below.
Adding a new built-in tool means adding a module in this package, exporting
``TOOL_CONFIG``, and appending the module name to the tuple.
"""

from __future__ import annotations

import importlib
import inspect
from typing import Optional

from app.config.settings import BaseConfig
from app.lib.llm.base import LLMProvider
from app.lib.llm.factory import create_llm_provider
from app.tools.builtin.adapter import BuiltInToolAdapter
from app.tools.builtin.base import BuiltInTool, BuiltInToolResult
from app.tools.builtin.gg_calendar import Var as CalendarVar
from app.tools.builtin.tool import BuiltInToolGroup
from app.tools.config_manager import ToolConfigManager
from app.tools.ids import slugify
from app.tools.registry import ToolRegistry
from app.utils.logger import logger

__all__ = [
    "BuiltInTool",
    "BuiltInToolResult",
    "BuiltInToolAdapter",
    "BuiltInToolGroup",
    "register_builtin_tools",
    "refresh_builtin_tool_oauth",
    "get_builtin_tool_config",
]


_BUILTIN_MODULE_NAMES: tuple[str, ...] = (
    "exec_shell",
    "markdown_converter",
    "message_detail",
    "system_file",
    "weather",
    "gg_calendar",
    "gg_places",
    "browser",
    "sleep",
    "register_skill_event",
)


def get_builtin_tool_config(config_name: str) -> dict:
    """Return the schema dict for a built-in tool, matching the old TOML shape.

    Returns ``{"tool": {...}}`` so callers previously reading the TOML can use
    this function as a drop-in replacement. Returns ``{}`` if the module or
    ``TOOL_CONFIG`` export is missing.
    """
    try:
        module = importlib.import_module(f"app.tools.builtin.{config_name}")
    except ImportError:
        return {}
    tool_config = getattr(module, "TOOL_CONFIG", None)
    if not isinstance(tool_config, dict):
        return {}
    return {"tool": tool_config}


def _build_oauth_provider(*, oauth_config: Optional[dict], server_name: str):
    """Return a callable ``adapter -> MCPOAuthClient`` if OAuth is configured."""
    if not oauth_config:
        return None

    client_id = oauth_config.get("client_id", "")
    client_secret = oauth_config.get("client_secret", "")
    if not client_id or not client_secret:
        return None

    from app.tools.mcp.mcp_auth import MCPOAuthClient

    extra_params = oauth_config.get("extra_authorize_params", {})
    auth_metadata = {
        "authorization_endpoint": oauth_config["authorization_endpoint"],
        "token_endpoint": oauth_config["token_endpoint"],
        "scopes_supported": oauth_config.get("scopes")
            or oauth_config.get("scopes_supported", []),
    }

    def factory(_adapter):
        client = MCPOAuthClient(
            server_url=f"builtin://{server_name}",
            server_name=server_name,
            client_id=client_id,
            client_secret=client_secret,
            extra_authorize_params=extra_params,
        )
        client.set_auth_metadata(auth_metadata)
        return client

    return factory


async def register_builtin_tools(
    *,
    registry: ToolRegistry,
    config_manager: ToolConfigManager,
    llm_factory,
    setup_profile: str = "admin",
    config_storage=None,
    vector_store=None,
) -> None:
    """Register all built-in tool groups from ``_BUILTIN_MODULE_NAMES``.

    Args
    ----
    registry        : the central ToolRegistry.
    config_manager  : ToolConfigManager (used for variable lookup).
    llm_factory     : callable ``(module_name, profile) -> LLMProvider``. May
                      raise -- failures skip that one tool.
    setup_profile   : profile from which to read OAuth client_id/secret at
                      registration time. Per-profile overrides remain effective
                      at execution time (handled by the adapter).
    """
    for module_name in _BUILTIN_MODULE_NAMES:
        try:
            module = importlib.import_module(f"app.tools.builtin.{module_name}")
        except ImportError as e:
            logger.error(f"Built-in tool module '{module_name}' not importable: {e}")
            continue

        tool_info = getattr(module, "TOOL_CONFIG", None)
        if not isinstance(tool_info, dict):
            logger.error(
                f"Built-in tool module '{module_name}' is missing a "
                "TOOL_CONFIG dict; skipping."
            )
            continue

        if not tool_info.get("visible", True):
            logger.info(f"Built-in tool '{module_name}' is marked as not visible; skipping.")
            continue

        config: dict[str, str] = {}
        config["OPENPA_WORKING_DIR"] = BaseConfig.OPENPA_WORKING_DIR
        config["SQLITE_DB_PATH"] = BaseConfig.SQLITE_DB_PATH

        try:
            functions = module.get_tools(config)
        except Exception as e:  # noqa: BLE001
            logger.exception(f"get_tools failed for built-in '{module_name}': {e}")
            continue

        server_name = getattr(module, "SERVER_NAME", module_name)
        llm_defaults: dict = dict(tool_info.get("llm_parameters") or {})
        instructions = llm_defaults.get("tool_instructions", "") or ""
        if hasattr(module, "_make_server_instructions"):
            instructions = module._make_server_instructions(config["OPENPA_WORKING_DIR"])

        prepare_tools_fn = None
        if hasattr(module, "get_prepare_tools"):
            fn = module.get_prepare_tools
            kwargs = {}
            if "vector_store" in inspect.signature(fn).parameters:
                kwargs["vector_store"] = vector_store
            prepare_tools_fn = fn(**kwargs)

        # OAuth provider: read GOOGLE_CLIENT_ID/SECRET from the variable scope
        oauth_provider = None
        oauth_config = tool_info.get("oauth")
        if oauth_config:
            tool_id_for_lookup = slugify(server_name)
            try:
                vars_ = config_manager.get_variables(
                    tool_id_for_lookup, setup_profile, include_secrets=True,
                )
            except Exception:
                vars_ = {}
            populated = dict(oauth_config)
            populated.setdefault("client_id", "")
            populated.setdefault("client_secret", "")
            populated["client_id"] = vars_.get(CalendarVar.CLIENT_ID, populated["client_id"])
            populated["client_secret"] = vars_.get(
                CalendarVar.CLIENT_SECRET, populated["client_secret"],
            )
            oauth_provider = _build_oauth_provider(
                oauth_config=populated, server_name=server_name,
            )

        # Read persisted LLM params from DB (mirrors MCP hydration in server.py).
        # Per-key precedence: DB override > TOOL_CONFIG["llm_parameters"] default.
        tool_id_for_config = slugify(server_name)
        try:
            _llm_params = config_manager.get_llm_params(tool_id_for_config, setup_profile)
        except Exception:  # noqa: BLE001
            _llm_params = {}

        try:
            _meta = config_manager.get_meta(tool_id_for_config, setup_profile)
        except Exception:  # noqa: BLE001
            _meta = {}

        effective_llm: dict = {**llm_defaults, **(_llm_params or {})}
        effective_meta: dict = {
            k: llm_defaults[k]
            for k in ("system_prompt", "description")
            if k in llm_defaults
        }
        effective_meta.update(_meta or {})

        llm: Optional[LLMProvider] = None
        if effective_llm.get("llm_provider") or effective_llm.get("llm_model"):
            try:
                llm = create_llm_provider(
                    provider_name=effective_llm.get("llm_provider")
                        or BaseConfig.get_default_provider(),
                    model_name=effective_llm.get("llm_model") or None,
                    config_storage=config_storage,
                    profile=setup_profile,
                    default_reasoning_effort=effective_llm.get("reasoning_effort"),
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    f"Per-tool LLM hydrate failed for '{server_name}': {e}. "
                    "Falling back to default model-group LLM."
                )

        if llm is None:
            try:
                llm = llm_factory(module_name, setup_profile)
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    f"No LLM available for built-in tool '{server_name}': {e}. "
                    "Registering as unbound -- will be rebound after setup completes."
                )
                llm = None

        group = BuiltInToolGroup(
            config_name=module_name,
            display_name=server_name,
            description=effective_meta.get("description") or instructions or server_name,
            functions=functions,
            llm=llm,
            arguments_schema=tool_info.get("arguments") or None,
            oauth_provider=oauth_provider,
            prepare_tools=prepare_tools_fn,
            full_reasoning=bool(effective_llm.get("full_reasoning", False)),
            system_prompt=effective_meta.get("system_prompt") or None,
            tool_instructions=instructions or None,
            llm_factory=llm_factory,
        )
        registry.register_builtin(group, source=module_name)


def refresh_builtin_tool_oauth(
    registry: ToolRegistry,
    config_manager: ToolConfigManager,
    tool_id: str,
    profile: str = "admin",
) -> bool:
    """Reload OAuth client for a built-in tool whose variables were updated."""
    tool = registry.get(tool_id)
    if not isinstance(tool, BuiltInToolGroup):
        return False

    schema = get_builtin_tool_config(tool.config_name)
    oauth_config = schema.get("tool", {}).get("oauth")
    if not oauth_config:
        return False

    vars_ = config_manager.get_variables(tool_id, profile, include_secrets=True)
    populated = dict(oauth_config)
    populated.setdefault("client_id", "")
    populated.setdefault("client_secret", "")
    populated["client_id"] = vars_.get(CalendarVar.CLIENT_ID, populated["client_id"])
    populated["client_secret"] = vars_.get(
        CalendarVar.CLIENT_SECRET, populated["client_secret"],
    )

    factory = _build_oauth_provider(
        oauth_config=populated, server_name=tool.name,
    )
    if factory is None:
        return False
    tool.adapter._mcp_auth = factory(tool.adapter)
    logger.info(f"Refreshed OAuth for built-in tool '{tool.name}' (profile={profile})")
    return True

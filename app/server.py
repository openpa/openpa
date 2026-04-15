"""OpenPA HTTP server.

Startup sequence:
1. Initialize storage (sync config + async conversation storage).
2. Build the central :class:`ToolRegistry` (storage + scoped config manager).
3. Register intrinsic tools (always, hard-coded).
4. Initialize built-in tools (TOML-driven, child LLM = "low" model group).
5. Hydrate the registry with persisted A2A and MCP tools.
6. Scan + register skills, then start the file watcher with hot-reload.
7. Build the OpenPAAgent / executor and serve.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import jwt
import uvicorn
from starlette.applications import Starlette
from starlette.authentication import (
    AuthCredentials,
    AuthenticationBackend,
    BaseUser,
    SimpleUser,
)
from starlette.middleware import Middleware
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import HTTPConnection, Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from a2a.server.apps import A2AStarletteApplication, CallContextBuilder
from a2a.server.agent_execution.context import ServerCallContext
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentSkill,
    HTTPAuthSecurityScheme,
    SecurityScheme,
)

from app.agent.agent import OpenPAAgent
from app.agent.executor import OpenPAAgentExecutor
from app.api import get_api_routes
from app.config.settings import BaseConfig, set_dynamic_config_storage
from app.constants import INTRODUCE_ASSISTANT
from app.lib.embedding import GrpcEmbeddings
from app.lib.llm.factory import create_llm_provider
from app.lib.llm.model_groups import ModelGroupManager
from app.storage import (
    get_conversation_storage,
    get_dynamic_config_storage,
    get_tool_storage,
)
from app.tools import (
    ToolConfigManager,
    ToolRegistry,
    ToolType,
    set_tool_registry,
)
from app.tools.a2a import build_a2a_stub, build_a2a_tool
from app.tools.builtin import (
    BuiltInToolGroup,
    refresh_builtin_tool_oauth,
    register_builtin_tools,
)
from app.tools.intrinsic import register_intrinsic_tools
from app.tools.mcp import (
    build_http_mcp_tool,
    build_mcp_stub,
    build_stdio_mcp_tool,
)
from app.tools.skills import scan_skills
from app.tools.skills.tool import SkillTool
from app.tools.skills.watcher import SkillsWatcher
from app.utils.logger import logger

DEFAULT_HOST = BaseConfig.HOST
DEFAULT_PORT = BaseConfig.PORT

grpc_embedding = GrpcEmbeddings()

_pending_return_urls: dict[tuple[str, str], str] = {}


# ── auth middleware ────────────────────────────────────────────────────────


class JWTAuthBackend(AuthenticationBackend):
    """JWT auth with signature verification (secret resolved per request)."""

    def __init__(self, secret_provider, algorithms=None):
        self.secret_provider = secret_provider
        self.algorithms = algorithms or ["HS256"]

    async def authenticate(self, conn: HTTPConnection):
        auth_header = conn.headers.get("Authorization")
        if not auth_header or not auth_header.startswith("Bearer "):
            return None
        secret = self.secret_provider()
        if not secret:
            return None
        token = auth_header.split("Bearer ", 1)[1]
        try:
            payload = jwt.decode(token, secret, algorithms=self.algorithms)
            return AuthCredentials(["authenticated"]), SimpleUser(payload.get("sub", "anonymous"))
        except jwt.InvalidTokenError:
            return None


class JWTCallContextBuilder(CallContextBuilder):
    """Build ServerCallContext from a Starlette Request, extracting JWT profile."""

    def __init__(self, secret_provider):
        self.secret_provider = secret_provider

    def build(self, request: Request) -> ServerCallContext:
        state: dict = {}
        secret = self.secret_provider()
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer ") and secret:
            token = auth_header.split("Bearer ", 1)[1]
            try:
                payload = jwt.decode(token, secret, algorithms=["HS256"])
                state["profile"] = payload.get("profile", "")
                state["sub"] = payload.get("sub", "")
            except jwt.InvalidTokenError:
                pass
        return ServerCallContext(state=state)


# ── persisted-tool hydration ───────────────────────────────────────────────


_STUB_ERR_LAZY = "Not connected (disabled by all profiles)"


async def _connect_a2a_tool(row: dict):
    """Build a live A2A tool from a persisted row; fall back to a stub on failure."""
    url = row["source"]
    owner = row["owner_profile"]
    try:
        return await build_a2a_tool(url=url, owner_profile=owner)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"A2A tool '{url}' unreachable: {e}")
        return build_a2a_stub(
            url=url, name=row["name"], owner_profile=owner, error=str(e),
        )


async def _connect_mcp_tool(
    registry: ToolRegistry,
    model_group_mgr: ModelGroupManager,
    row: dict,
):
    """Build a live MCP tool from a persisted row; fall back to a stub on failure.

    Preserves startup's LLM-resolution chain: per-tool LLM override via
    ``config.get_llm_params`` → fallback to ``model_group_mgr`` "low" group.
    Honors ``full_reasoning`` and the ``stdio`` vs ``http`` transport branch.
    """
    url = row["source"]
    owner = row["owner_profile"]
    extra = row.get("extra") or {}
    try:
        llm_params = registry.config.get_llm_params(row["tool_id"], owner or "admin")
    except Exception:
        llm_params = {}
    llm = None
    try:
        if llm_params.get("llm_provider") or llm_params.get("llm_model"):
            llm = create_llm_provider(
                provider_name=llm_params.get("llm_provider")
                    or BaseConfig.get_default_provider(),
                model_name=llm_params.get("llm_model"),
                config_storage=model_group_mgr.config_storage,
                profile=owner or "admin",
                default_reasoning_effort=llm_params.get("reasoning_effort"),
            )
        else:
            llm = model_group_mgr.create_llm_for_group("low", profile=owner or "admin")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"No LLM for MCP server '{url}': {e}")

    tool = None
    if llm is not None:
        try:
            if extra.get("transport_type") == "stdio":
                tool = await build_stdio_mcp_tool(
                    command=extra["command"], args=extra.get("args", []),
                    env=extra.get("env"), llm=llm, owner_profile=owner,
                    full_reasoning=bool(llm_params.get("full_reasoning", False)),
                )
            else:
                tool = await build_http_mcp_tool(
                    url=url, llm=llm, owner_profile=owner,
                    full_reasoning=bool(llm_params.get("full_reasoning", False)),
                )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"MCP tool '{url}' unreachable: {e}")
    if tool is None:
        tool = build_mcp_stub(
            url=url, name=row["name"], owner_profile=owner,
            error="LLM not available" if llm is None else "Connection failed",
            transport_type=extra.get("transport_type", "http"),
            extra=extra,
        )
    return tool


def _lazy_a2a_stub_from_row(row: dict):
    return build_a2a_stub(
        url=row["source"], name=row["name"],
        owner_profile=row["owner_profile"], error=_STUB_ERR_LAZY,
    )


def _lazy_mcp_stub_from_row(row: dict):
    extra = row.get("extra") or {}
    return build_mcp_stub(
        url=row["source"], name=row["name"],
        owner_profile=row["owner_profile"], error=_STUB_ERR_LAZY,
        transport_type=extra.get("transport_type", "http"),
        extra=extra,
    )


async def _hydrate_persisted_tools(
    *,
    registry: ToolRegistry,
    model_group_mgr: ModelGroupManager,
) -> None:
    """Hydrate A2A / MCP tools recorded in the ``tools`` table.

    Lazy initialization: only tools with at least one profile having
    ``enabled = 1`` are actually connected. Everything else is registered
    as a stub and upgraded on-demand via :func:`connect_persisted_tool`
    when a user toggles it on in Settings.
    """
    enabled_ids = registry.storage.list_tool_ids_enabled_by_any_profile()
    persisted = registry.storage.list_tools()

    for row in persisted:
        tool_type = row["tool_type"]
        if tool_type not in (ToolType.A2A.value, ToolType.MCP.value):
            continue
        tool_id = row["tool_id"]
        if tool_id in enabled_ids:
            if tool_type == ToolType.A2A.value:
                tool = await _connect_a2a_tool(row)
            else:
                tool = await _connect_mcp_tool(registry, model_group_mgr, row)
        else:
            if tool_type == ToolType.A2A.value:
                tool = _lazy_a2a_stub_from_row(row)
            else:
                tool = _lazy_mcp_stub_from_row(row)
            logger.info(
                f"{tool_type.upper()} tool '{row['name']}' (tool_id={tool_id}) "
                f"registered as stub — no profile has it enabled"
            )
        registry._tools[tool_id] = tool  # type: ignore[attr-defined]
        tool.tool_id = tool_id


# ── lazy connect (runtime) ─────────────────────────────────────────────────

_pending_connects: dict[str, asyncio.Task] = {}


async def connect_persisted_tool(
    registry: ToolRegistry,
    model_group_mgr: ModelGroupManager,
    tool_id: str,
) -> tuple[bool, str | None]:
    """Upgrade a stub A2A/MCP tool to a live connection, in place.

    Returns ``(success, error_message)``:
    - ``(True, None)`` — tool is now connected (or was already connected).
    - ``(False, "...")`` — connection attempt failed; stub remains in place.

    Concurrent calls for the same ``tool_id`` share a single in-flight task,
    so rapid toggle-on/off/on clicks collapse to one connection attempt.
    Callers that want fire-and-forget semantics should wrap in
    ``asyncio.create_task(...)``.
    """
    existing = _pending_connects.get(tool_id)
    if existing is not None and not existing.done():
        return await existing

    async def _do() -> tuple[bool, str | None]:
        try:
            tool = registry.get(tool_id)
            if tool is None:
                return False, f"Tool '{tool_id}' not registered"
            if tool.tool_type not in (ToolType.A2A, ToolType.MCP):
                return False, f"Tool '{tool_id}' is not an A2A/MCP tool"
            if not getattr(tool, "is_stub", False):
                return True, None  # already connected

            row = registry.storage.get_tool(tool_id)
            if row is None:
                return False, f"Tool '{tool_id}' not persisted"

            if tool.tool_type is ToolType.A2A:
                new_tool = await _connect_a2a_tool(row)
            else:
                new_tool = await _connect_mcp_tool(registry, model_group_mgr, row)

            if getattr(new_tool, "is_stub", False):
                err = getattr(new_tool, "connection_error", None) or "connection failed"
                return False, str(err)
            await registry.replace_tool(tool_id, new_tool)
            logger.info(f"Lazy-connected tool '{tool_id}'")
            return True, None
        except Exception as e:  # noqa: BLE001
            logger.exception(f"Lazy connect failed for '{tool_id}'")
            return False, str(e)

    task = asyncio.create_task(_do())
    _pending_connects[tool_id] = task
    try:
        return await task
    finally:
        _pending_connects.pop(tool_id, None)


# ── main ───────────────────────────────────────────────────────────────────


async def main(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT):
    # 1. Storage
    config_storage = get_dynamic_config_storage()
    set_dynamic_config_storage(config_storage)
    conversation_storage = get_conversation_storage()
    await conversation_storage.initialize()
    tool_storage = get_tool_storage()

    # 2. Registry
    config_manager = ToolConfigManager(tool_storage)
    registry = ToolRegistry(tool_storage, config_manager)
    set_tool_registry(registry)

    # 3. Intrinsic tools
    register_intrinsic_tools(registry)

    # 4. Model groups + built-in tools
    model_group_mgr = ModelGroupManager(config_storage)

    def _builtin_llm_factory(_module_name: str, profile: str):
        return model_group_mgr.create_llm_for_group("low", profile=profile)

    # Built-in tools are always registered at startup so the setup wizard and
    # the Tools & Skills settings page can list and configure them. When no
    # LLM is available yet (pre-setup), tools register with ``llm=None`` and
    # are rebound by ``on_first_setup`` once the user picks providers/models.
    try:
        await register_builtin_tools(
            registry=registry,
            config_manager=config_manager,
            llm_factory=_builtin_llm_factory,
            setup_profile="admin",
            config_storage=config_storage,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Built-in tool registration failed: {e}")

    # 4b. If setup is already complete, rebind any built-in tools that
    #     registered with llm=None (e.g. transient LLM-factory failure).
    if config_storage.is_setup_complete():
        for tool in registry.all_tools():
            if isinstance(tool, BuiltInToolGroup) and not tool.is_llm_bound:
                try:
                    llm = _builtin_llm_factory(tool.config_name, "admin")
                    tool.update_runtime_config(llm=llm)
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        f"Could not bind LLM to built-in tool '{tool.name}' "
                        f"at startup: {e}"
                    )

    # 5. Hydrate persisted A2A / MCP tools
    await _hydrate_persisted_tools(registry=registry, model_group_mgr=model_group_mgr)

    # 6. Skills
    skills_dir = Path(BaseConfig.OPENPA_WORKING_DIR) / "skills"
    skills_watcher = None
    if skills_dir.is_dir():
        skills = scan_skills(skills_dir)
        if skills:
            await registry.sync_skills(skills, skill_factory=lambda info: SkillTool(info))
            logger.info(f"Synced {len(skills)} skill(s) from disk")
    else:
        logger.info(f"Skills directory not found: {skills_dir}")

    # 7. High-group LLM (admin) + OpenPAAgent
    runner = None
    if config_storage.is_setup_complete():
        try:
            runner = model_group_mgr.create_llm_for_group("high", profile="admin")
        except ValueError as e:
            logger.warning(f"Failed to create 'high' group LLM: {e}")

    openpa_agent = OpenPAAgent(
        registry=registry,
        embedding=grpc_embedding,
        runner=runner,
        model_group_mgr=model_group_mgr,
        config_storage=config_storage,
    )

    if skills_dir.is_dir():
        loop = asyncio.get_event_loop()

        def _on_skills_change(new_skills):
            future = asyncio.run_coroutine_threadsafe(
                registry.sync_skills(
                    new_skills, skill_factory=lambda info: SkillTool(info),
                ),
                loop,
            )
            try:
                future.result(timeout=30)
            except Exception:  # noqa: BLE001
                logger.exception("Skills sync failed")

        skills_watcher = SkillsWatcher(skills_dir, on_change=_on_skills_change)
        skills_watcher.start()

    # 8. Agent card
    skill = AgentSkill(
        id=BaseConfig.AGENT_ID,
        name=BaseConfig.AGENT_NAME,
        description=INTRODUCE_ASSISTANT,
        tags=["assistant"],
        examples=[
            "Help me with my tasks", "What can you do?", "Tell me a joke",
            "What's the weather like today?", "Set a reminder for me",
            "Control my smart home devices",
        ],
    )
    agent_card_kwargs = dict(
        name=BaseConfig.AGENT_NAME,
        description=INTRODUCE_ASSISTANT,
        url=BaseConfig.APP_URL,
        version="1.0.0",
        default_input_modes=["text"],
        default_output_modes=["text"],
        capabilities=AgentCapabilities(streaming=True),
        skills=[skill],
    )
    jwt_secret = BaseConfig.get_jwt_secret()
    if jwt_secret:
        agent_card_kwargs["security_schemes"] = {
            "bearerAuth": SecurityScheme(
                root=HTTPAuthSecurityScheme(
                    scheme="bearer", bearer_format="JWT", type="http",
                    description="JWT Bearer token authentication",
                )
            ),
        }
        agent_card_kwargs["security"] = [{"bearerAuth": []}]
    agent_card = AgentCard(**agent_card_kwargs)

    agent_executor = OpenPAAgentExecutor(openpa_agent, conversation_storage=conversation_storage)
    request_handler = DefaultRequestHandler(
        agent_executor=agent_executor, task_store=InMemoryTaskStore(),
    )
    context_builder = JWTCallContextBuilder(secret_provider=BaseConfig.get_jwt_secret)
    a2a_app = A2AStarletteApplication(
        agent_card=agent_card, http_handler=request_handler,
        context_builder=context_builder,
    )
    routes = a2a_app.routes()

    async def test_endpoint(request: Request) -> PlainTextResponse:
        return PlainTextResponse("OK")
    routes.append(Route(path="/test", methods=["GET"], endpoint=test_endpoint))

    async def on_first_setup(profile: str):
        # Built-in tools were already registered at startup (some possibly with
        # llm=None because no LLM was configured yet). Walk the registry, bind
        # an LLM to each, and refresh OAuth clients with the freshly-saved
        # profile credentials.
        for tool in registry.all_tools():
            if not isinstance(tool, BuiltInToolGroup):
                continue
            try:
                llm = _builtin_llm_factory(tool.config_name, profile)
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    f"Could not bind LLM to built-in tool '{tool.name}' "
                    f"after setup: {e}"
                )
                continue
            tool.update_runtime_config(llm=llm)
            try:
                refresh_builtin_tool_oauth(
                    registry, config_manager, tool.tool_id, profile=profile,
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    f"OAuth refresh failed for built-in tool '{tool.name}'"
                )
        if skills_dir.is_dir():
            skills = scan_skills(skills_dir)
            if skills:
                await registry.sync_skills(
                    skills, skill_factory=lambda info: SkillTool(info),
                )
        openpa_agent.update_embeddings()
        logger.info("Post-setup: built-in tools rebound and skills synced")

    def _mcp_llm_factory():
        try:
            return model_group_mgr.create_llm_for_group("low", profile="admin")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Failed to create MCP LLM: {e}")
            return None

    async def _connect_persisted_tool(tool_id: str) -> tuple[bool, str | None]:
        return await connect_persisted_tool(registry, model_group_mgr, tool_id)

    routes.extend(get_api_routes(
        registry=registry,
        pending_return_urls=_pending_return_urls,
        mcp_llm_factory=_mcp_llm_factory,
        conversation_storage=conversation_storage,
        config_storage=config_storage,
        on_first_setup=on_first_setup,
        connect_persisted_tool=_connect_persisted_tool,
    ))

    middleware_stack = [
        Middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        ),
        Middleware(
            AuthenticationMiddleware,
            backend=JWTAuthBackend(secret_provider=BaseConfig.get_jwt_secret),
        ),
    ]
    from app.middleware import A2AAuthGuard
    middleware_stack.append(Middleware(A2AAuthGuard))

    app = Starlette(routes=routes, middleware=middleware_stack)
    config = uvicorn.Config(app, host=host, port=port)
    server = uvicorn.Server(config)
    await server.serve()

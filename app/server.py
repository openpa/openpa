
import asyncio
import uvicorn
import jwt
from pathlib import Path

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
    SecurityScheme,
    HTTPAuthSecurityScheme,
)
from app.constants import INTRODUCE_ASSISTANT
from app.agent.executor import (
    OpenPAAgentExecutor,
)

from app.agent.agent import OpenPAAgent

from app.remote_agents.routing_agent import RoutingAgent
from app.utils.logger import logger
from app.config.settings import BaseConfig, set_dynamic_config_storage
from app.utils.remote_agent_storage import get_remote_agent_storage
from app.utils.mcp_server_storage import get_mcp_server_storage
from app.storage import get_conversation_storage, get_dynamic_config_storage

from app.lib.me5 import Me5Embeddings
from app.lib.llm.model_groups import ModelGroupManager
from app.tools.tool_config_manager import ToolConfigManager
from app.types import EmbeddingTable, VectorEmbeddingType

from app.api import get_api_routes

DEFAULT_HOST = BaseConfig.HOST
DEFAULT_PORT = BaseConfig.PORT

# Default size: 384
me5_small_embedding = Me5Embeddings(VectorEmbeddingType.ME5_SMALL)

# Temporary storage for return URLs during OAuth flows (keyed by (agent_name, profile))
_pending_return_urls: dict[tuple[str, str], str] = {}


class JWTAuthBackend(AuthenticationBackend):
    """JWT authentication backend with signature verification.

    Uses a callable secret_provider so the secret is resolved at request time,
    allowing setup-wizard-generated secrets to take effect without a restart.
    """

    def __init__(self, secret_provider: callable, algorithms: list[str] | None = None):
        self.secret_provider = secret_provider
        self.algorithms = algorithms or ["HS256"]

    async def authenticate(
        self, conn: HTTPConnection
    ) -> tuple[AuthCredentials, BaseUser] | None:
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
        except jwt.ExpiredSignatureError:
            return None
        except jwt.InvalidTokenError:
            return None


class JWTCallContextBuilder(CallContextBuilder):
    """Builds ServerCallContext from Starlette Request, extracting JWT profile."""

    def __init__(self, secret_provider: callable):
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


async def main(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT):
    # Initialize dynamic config storage (synchronous, for config lookups)
    config_storage = get_dynamic_config_storage()
    set_dynamic_config_storage(config_storage)

    # Initialize model group manager
    model_group_mgr = ModelGroupManager(config_storage)

    # Initialize tool config manager
    tool_config_mgr = ToolConfigManager(config_storage)

    skill = AgentSkill(
        id=BaseConfig.AGENT_ID,
        name=BaseConfig.AGENT_NAME,
        description=INTRODUCE_ASSISTANT,
        tags=["assistant"],
        examples=[
            "Help me with my tasks",
            "What can you do?",
            "Tell me a joke",
            "What's the weather like today?",
            "Set a reminder for me",
            "Control my smart home devices"],
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

    # Use JWT secret from dynamic config or env
    jwt_secret = BaseConfig.get_jwt_secret()

    if jwt_secret:
        agent_card_kwargs["security_schemes"] = {
            "bearerAuth": SecurityScheme(
                root=HTTPAuthSecurityScheme(
                    scheme="bearer",
                    bearer_format="JWT",
                    type="http",
                    description="JWT Bearer token authentication",
                )
            ),
        }
        agent_card_kwargs["security"] = [{"bearerAuth": []}]

    agent_card = AgentCard(**agent_card_kwargs)

    # Create main LLM runner using model groups (admin profile at startup)
    runner = None
    if config_storage.is_setup_complete():
        try:
            runner = model_group_mgr.create_llm_for_group("high", profile="admin")
        except ValueError as e:
            logger.warning(f"Failed to create 'high' group LLM: {e}")

    # Initialize remote agent storage
    remote_agent_storage = get_remote_agent_storage()

    # Load agent addresses per profile from storage
    all_agents_by_profile = remote_agent_storage.get_all_profiles_agents()
    total_agents = sum(len(addrs) for addrs in all_agents_by_profile.values())
    logger.info(f"Loaded {total_agents} agent(s) across {len(all_agents_by_profile)} profile(s) from storage")

    routing_agent = await RoutingAgent.create(
        remote_agent_addresses=all_agents_by_profile,
    )

    # Create MCP LLM using model groups (low group for tools, admin profile at startup)
    mcp_llm = None
    if config_storage.is_setup_complete():
        try:
            mcp_llm = model_group_mgr.create_llm_for_group("low", profile="admin")
        except ValueError as e:
            logger.warning(f"Failed to create 'low' group LLM: {e}")

    # Initialize MCP server storage (needed for both stdio and HTTP servers)
    mcp_server_storage = get_mcp_server_storage()

    # Initialize stdio MCP servers (shared across all profiles)
    # Inject tool-specific env vars from ToolConfigManager
    from app.tools.mcp import STDIO_MCP_SERVERS
    for server_config in STDIO_MCP_SERVERS:
        tool_name = server_config["name"]
        # Merge tool env from config storage (admin profile at startup)
        tool_env = tool_config_mgr.get_tool_env(tool_name, profile="admin")
        if tool_env:
            server_config["env"] = {**server_config.get("env", {}), **tool_env}
        # Inject shared app config for subprocesses
        server_config["env"]["OPENPA_WORKING_DIR"] = BaseConfig.OPENPA_WORKING_DIR
        server_config["env"]["SQLITE_DB_PATH"] = BaseConfig.SQLITE_DB_PATH

    # Filter out unconfigured tools (those with required config that isn't set)
    active_servers = []
    for server_config in STDIO_MCP_SERVERS:
        tool_name = server_config["name"]
        if tool_config_mgr.is_tool_enabled(tool_name, profile="admin"):
            active_servers.append(server_config)
        else:
            logger.info(f"Skipping MCP server '{tool_name}': not configured or disabled")

    await routing_agent.init_stdio_mcp_servers(active_servers, mcp_llm)

    # Load per-profile enabled/disabled overrides for shared (stdio) servers
    routing_agent.load_shared_overrides(mcp_server_storage)

    # Load persisted HTTP MCP servers per profile (with per-server LLM config)
    all_mcp_by_profile = mcp_server_storage.get_all_profiles_agents()
    total_mcp = sum(len(configs) for configs in all_mcp_by_profile.values())
    logger.info(f"Loaded {total_mcp} MCP server(s) across {len(all_mcp_by_profile)} profile(s) from storage")
    for profile, mcp_configs in all_mcp_by_profile.items():
        for mcp_config in mcp_configs:
            mcp_url = mcp_config["url"]
            # Skip stdio entries (their overrides are loaded above)
            if mcp_url.startswith("stdio://"):
                continue
            # Create per-server LLM if custom provider/model configured
            if mcp_config.get("llm_provider") or mcp_config.get("llm_model"):
                from app.lib.llm.factory import create_llm_provider
                try:
                    server_llm = create_llm_provider(
                        provider_name=mcp_config.get("llm_provider") or BaseConfig.get_default_provider(),
                        model_name=mcp_config.get("llm_model"),
                        config_storage=config_storage,
                        profile=profile,
                    )
                except ValueError as e:
                    logger.error(f"Failed to create LLM for MCP server {mcp_url}: {e}, using default")
                    server_llm = mcp_llm
            else:
                server_llm = mcp_llm
            server_name = await routing_agent.add_mcp_server(
                mcp_url,
                server_llm,
                system_prompt=mcp_config.get("system_prompt"),
                description=mcp_config.get("description"),
                mcp_server_storage=mcp_server_storage,
                stored_server_name=mcp_config.get("server_name"),
                profile=profile,
            )
            # Apply persisted enabled state (default True)
            if server_name:
                from app.remote_agents.routing_agent import RoutingAgent as _RA
                key = _RA._make_key(server_name, profile)
                if key in routing_agent.agents_info:
                    routing_agent.agents_info[key]['enabled'] = mcp_config.get('enabled', True)

    # Initialize conversation storage
    conversation_storage = get_conversation_storage()
    await conversation_storage.initialize()

    openpa_agent = OpenPAAgent(
        runner=runner,
        routing_agent=routing_agent,
        me5_embedding=me5_small_embedding,
        model_group_mgr=model_group_mgr,
    )

    agent_executor = OpenPAAgentExecutor(openpa_agent, conversation_storage=conversation_storage)

    request_handler = DefaultRequestHandler(
        agent_executor=agent_executor, task_store=InMemoryTaskStore()
    )

    context_builder = JWTCallContextBuilder(secret_provider=BaseConfig.get_jwt_secret)
    a2a_app = A2AStarletteApplication(
        agent_card=agent_card, http_handler=request_handler,
        context_builder=context_builder,
    )
    routes = a2a_app.routes()

    async def test_endpoint(request: Request) -> PlainTextResponse:
        return PlainTextResponse("OK")
    routes.append(Route(path='/test', methods=['GET'], endpoint=test_endpoint))

    # Add all API routes from submodules
    routes.extend(get_api_routes(
        routing_agent=routing_agent,
        remote_agent_storage=remote_agent_storage,
        pending_return_urls=_pending_return_urls,
        mcp_server_storage=mcp_server_storage,
        mcp_llm=mcp_llm,
        conversation_storage=conversation_storage,
        config_storage=config_storage,
        tool_config_manager=tool_config_mgr,
    ))

    middleware_stack = [
        Middleware(
            CORSMiddleware,
            allow_origins=["*"],  # Allow all origins for development
            allow_credentials=True,
            allow_methods=["*"],  # Allow all methods
            allow_headers=["*"],  # Allow all headers
        ),
        Middleware(
            AuthenticationMiddleware, backend=JWTAuthBackend(secret_provider=BaseConfig.get_jwt_secret)
        ),
    ]

    from app.middleware import A2AAuthGuard
    middleware_stack.append(Middleware(A2AAuthGuard))

    app = Starlette(
        routes=routes,
        middleware=middleware_stack,
    )

    config = uvicorn.Config(app, host=host, port=port)
    server = uvicorn.Server(config)
    await server.serve()

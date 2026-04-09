from starlette.routing import Route

from app.api.agents import get_agent_routes
from app.api.config import get_config_routes
from app.api.conversations import get_conversation_routes
from app.api.llm import get_llm_routes
from app.api.oauth2 import get_oauth2_routes
from app.api.profiles import get_profile_routes
from app.api.files import get_file_routes
from app.api.tokens import get_token_routes
from app.api.tools import get_tool_routes


def get_api_routes(
    routing_agent,
    remote_agent_storage,
    pending_return_urls,
    mcp_server_storage=None,
    mcp_llm=None,
    conversation_storage=None,
    config_storage=None,
    tool_config_manager=None,
) -> list[Route]:
    """Collect all API routes from submodules."""
    routes = []
    routes.extend(get_agent_routes(
        routing_agent, remote_agent_storage, pending_return_urls,
        mcp_server_storage=mcp_server_storage, mcp_llm=mcp_llm,
    ))
    routes.extend(get_oauth2_routes(routing_agent, pending_return_urls))
    routes.extend(get_profile_routes(
        conversation_storage,
        remote_agent_storage=remote_agent_storage,
        mcp_server_storage=mcp_server_storage,
        routing_agent=routing_agent,
    ))
    routes.extend(get_token_routes())
    routes.extend(get_file_routes())
    if conversation_storage:
        routes.extend(get_conversation_routes(conversation_storage))
    if config_storage:
        routes.extend(get_config_routes(config_storage, conversation_storage))
        routes.extend(get_llm_routes(config_storage))
    if tool_config_manager:
        routes.extend(get_tool_routes(tool_config_manager))
    return routes

from starlette.routing import Route

from app.api.agents import get_agent_routes
from app.api.config import get_config_routes
from app.api.conversations import get_conversation_routes
from app.api.files import get_file_routes
from app.api.llm import get_llm_routes
from app.api.oauth2 import get_oauth2_routes
from app.api.profiles import get_profile_routes
from app.api.tokens import get_token_routes
from app.api.tools import get_tool_routes


def get_api_routes(
    *,
    registry,
    pending_return_urls,
    mcp_llm_factory=None,
    conversation_storage=None,
    config_storage=None,
    on_first_setup=None,
    connect_persisted_tool=None,
) -> list[Route]:
    """Collect all API routes.

    ``connect_persisted_tool`` is an async callable ``(tool_id) -> (bool, err)``
    used by enable-toggle endpoints to lazily connect a stub MCP/A2A tool
    when a profile turns it on.
    """
    routes = []
    routes.extend(get_agent_routes(
        registry=registry,
        pending_return_urls=pending_return_urls,
        mcp_llm_factory=mcp_llm_factory,
        config_storage=config_storage,
        connect_persisted_tool=connect_persisted_tool,
    ))
    routes.extend(get_oauth2_routes(
        registry=registry, pending_return_urls=pending_return_urls,
    ))
    routes.extend(get_profile_routes(
        conversation_storage, registry=registry,
    ))
    routes.extend(get_token_routes())
    routes.extend(get_file_routes())
    if conversation_storage:
        routes.extend(get_conversation_routes(conversation_storage))
    if config_storage:
        routes.extend(get_config_routes(
            config_storage, conversation_storage,
            on_first_setup=on_first_setup, registry=registry,
        ))
        routes.extend(get_llm_routes(config_storage))
    routes.extend(get_tool_routes(
        registry, config_storage=config_storage,
        connect_persisted_tool=connect_persisted_tool,
    ))
    return routes

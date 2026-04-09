import inspect
import urllib.parse
import base64

from starlette.requests import Request
from starlette.responses import PlainTextResponse, HTMLResponse, RedirectResponse
from starlette.routing import Route

from app.config.settings import BaseConfig
from app.utils import logger


def get_dashboard_routes(routing_agent, conversation_storage, jinja_env, mcp_server_storage=None) -> list[Route]:

    async def handle_auth(request: Request) -> PlainTextResponse:
        return PlainTextResponse('Authentication successful.')

    async def handle_dashboard(request: Request) -> HTMLResponse:
        """Render the dashboard with all remote agents and their auth status for a specific profile."""
        # Get profile from query parameter (required)
        current_profile = request.query_params.get('profile')
        if not current_profile:
            # If no profile specified, check if any profiles exist and use first one
            all_profile_dicts = await conversation_storage.list_profiles()
            all_profiles = [p["name"] for p in all_profile_dicts]
            if not all_profiles:
                # Render dashboard with empty state so user can create a profile
                template = jinja_env.get_template('dashboard.html')
                html_content = template.render(
                    agents=[],
                    current_profile=None,
                    all_profiles=[]
                )
                return HTMLResponse(content=html_content)
            current_profile = all_profiles[0]

        # Get all profiles for the profile selector
        all_profile_dicts = await conversation_storage.list_profiles()
        all_profiles = [p["name"] for p in all_profile_dicts]

        # Prepare agent data for template
        agents = []

        # Get only agents visible to this profile (profile-owned + shared)
        profile_agents = routing_agent.get_agents_for_profile(current_profile)

        for agent_name, agent_info in profile_agents.items():
            remote_connection = agent_info['remote_agent_connections']
            card = agent_info['card']
            url = agent_info['url']

            # Get OAuth client for the selected profile
            oauth_client = remote_connection.get_oauth_client_for_profile(current_profile)

            # Get auth status for the selected profile
            auth_status = oauth_client.get_auth_status(current_profile)

            # Map status to badge and text
            status_map = {
                "not_supported": ("badge-secondary", "Agent does not support authentication"),
                "authenticated": ("badge-success", "Agent has been successfully authenticated"),
                "not_authenticated": ("badge-danger", "Agent has not been authenticated yet"),
                "expired": ("badge-warning", "Agent's token has expired")
            }

            badge_class, status_text = status_map.get(auth_status, ("badge-secondary", "Unknown"))

            # URL encode agent name for links
            encoded_name = urllib.parse.quote(agent_name)

            # Truncate description if too long
            description = card.description if card.description else "No description available"
            if len(description) > 100:
                description = description[:97] + "..."

            # Get expiration info for authenticated agents
            expiration_info = None
            if auth_status == "authenticated":
                expiration_info = oauth_client.get_expiration_info(current_profile)

            # Determine which buttons to show
            show_authenticate = auth_status in ["not_authenticated", "expired"]
            show_unlink = auth_status == "authenticated" or (auth_status == "expired" and oauth_client.get_token(current_profile))

            # Get arguments schema from agent card
            arguments_schema = agent_info.get('arguments_schema')

            # Get agent type and MCP config
            agent_type = agent_info.get('agent_type', 'a2a')
            mcp_config = None
            if agent_type == 'mcp' and mcp_server_storage:
                agent_profile = agent_info.get('profile', current_profile)
                storage_profile = current_profile if agent_profile == "__shared__" else agent_profile
                mcp_config = mcp_server_storage.get_agent_config(url, profile=storage_profile)

            agents.append({
                'name': agent_name,
                'encoded_name': encoded_name,
                'description': description,
                'url': url,
                'badge_class': badge_class,
                'status_text': status_text,
                'expiration_info': expiration_info,
                'show_authenticate': show_authenticate,
                'show_unlink': show_unlink,
                'arguments_schema': arguments_schema,
                'agent_type': agent_type,
                'mcp_config': mcp_config,
                'is_default': agent_info.get('is_default', False),
                'enabled': agent_info.get('enabled', True),
            })

        # Render template with profile context
        template = jinja_env.get_template('dashboard.html')
        html_content = template.render(
            agents=agents,
            current_profile=current_profile,
            all_profiles=all_profiles,
            jwt_enabled=bool(BaseConfig.get_jwt_secret()),
        )

        return HTMLResponse(content=html_content)

    async def handle_agent_auth(request: Request) -> RedirectResponse | HTMLResponse:
        """Initiate OAuth authentication for a specific agent and profile."""
        agent_name = request.path_params.get('agent_name')
        if not agent_name:
            return HTMLResponse(content="<h1>Error: Agent name is required</h1>", status_code=400)

        # URL decode agent name
        agent_name = urllib.parse.unquote(agent_name)

        # Get profile from query parameter (required)
        profile = request.query_params.get('profile')
        if not profile:
            return HTMLResponse(content="<h1>Error: Profile is required</h1>", status_code=400)

        # Look up agent for this profile
        profile_agents = routing_agent.get_agents_for_profile(profile)
        if agent_name not in profile_agents:
            return HTMLResponse(content=f"<h1>Error: Agent '{agent_name}' not found</h1>", status_code=404)

        agent_info = profile_agents[agent_name]
        remote_connection = agent_info['remote_agent_connections']

        # Get source parameter to track where auth was initiated from ('dashboard' or 'chat')
        source = request.query_params.get('source', 'dashboard')

        # Get OAuth client for the specified profile
        oauth_client = remote_connection.get_oauth_client_for_profile(profile)

        # Use unified /oauth2/callback for MCP servers, per-agent callback for A2A
        agent_type = agent_info.get('agent_type', 'a2a')
        if agent_type == 'mcp':
            redirect_uri = f"{BaseConfig.APP_URL}/oauth2/callback"
        else:
            encoded_name = urllib.parse.quote(agent_name)
            redirect_uri = f"{BaseConfig.APP_URL}/dashboard/{encoded_name}/callback"

        # Get auth URL with profile and source encoded in state parameter
        auth_url = oauth_client.get_auth_url(redirect_uri, profile, source=source)
        if inspect.isawaitable(auth_url):
            auth_url = await auth_url
        if not auth_url:
            return HTMLResponse(
                content=f"<h1>Error: Agent '{agent_name}' does not support OAuth authentication</h1>",
                status_code=400
            )

        logger.info(f"Redirecting to OAuth provider for {agent_name} (profile: {profile}): {auth_url}")
        return RedirectResponse(url=auth_url)

    async def handle_agent_callback(request: Request) -> HTMLResponse | RedirectResponse:
        """Handle OAuth callback from provider."""
        agent_name = request.path_params.get('agent_name')
        if not agent_name:
            return HTMLResponse(content="<h1>Error: Agent name is required</h1>", status_code=400)

        # URL decode agent name
        agent_name = urllib.parse.unquote(agent_name)

        # Get state parameter (contains encoded profile and source)
        state = request.query_params.get('state')

        # Extract profile and source from state
        # State format: {random_token}:{profile}:{source}
        profile = None
        source = "dashboard"
        if state:
            try:
                decoded_state = base64.urlsafe_b64decode(state).decode()
                parts = decoded_state.split(':')
                if len(parts) >= 2:
                    profile = parts[1]
                if len(parts) >= 3:
                    source = parts[2]
            except Exception as e:
                logger.warning(f"Failed to decode state: {e}")

        if not profile:
            return HTMLResponse(
                content="<h1>Error: Profile could not be determined from callback state</h1>",
                status_code=400
            )

        # Get authorization code from query params
        code = request.query_params.get('code')
        if not code:
            error = request.query_params.get('error', 'Unknown error')
            error_description = request.query_params.get('error_description', '')
            return HTMLResponse(
                content=f"<h1>Authentication Failed</h1><p>Error: {error}</p><p>{error_description}</p>",
                status_code=400
            )

        # Look up agent for this profile
        profile_agents = routing_agent.get_agents_for_profile(profile)
        if agent_name not in profile_agents:
            return HTMLResponse(content=f"<h1>Error: Agent '{agent_name}' not found</h1>", status_code=404)

        agent_info = profile_agents[agent_name]
        remote_connection = agent_info['remote_agent_connections']

        # Get OAuth client for the specified profile
        oauth_client = remote_connection.get_oauth_client_for_profile(profile)

        # Construct redirect URI (must match the one used in auth request - no query params)
        encoded_name = urllib.parse.quote(agent_name)
        redirect_uri = f"{BaseConfig.APP_URL}/dashboard/{encoded_name}/callback"

        # Exchange code for token, passing state for validation
        success = await oauth_client.handle_oauth_callback(code, redirect_uri, state, profile)

        if success:
            # Update the auth header on the remote connection for this profile
            remote_connection.update_auth_header_for_profile(profile)

            # For MCP servers: connect immediately to get real server info
            mcp_adapter = agent_info.get('mcp_adapter')
            if mcp_adapter:
                try:
                    await mcp_adapter._ensure_auth(profile)
                    logger.info(f"MCP server connected after auth: '{mcp_adapter.name}'")
                except Exception as e:
                    logger.warning(f"Failed to connect MCP server after auth: {e}")

            # If auth was initiated from chat message link, show close-tab page
            if source == "chat":
                return HTMLResponse(
                    content="<h1>Authentication successful!</h1><p>You can close this tab and continue your conversation.</p>"
                    "<script>setTimeout(function(){ window.close(); }, 2000);</script>"
                )

            # Redirect back to dashboard with profile parameter
            encoded_profile = urllib.parse.quote(profile)
            return RedirectResponse(url=f"/dashboard?profile={encoded_profile}")
        else:
            return HTMLResponse(
                content=f"<h1>Authentication Failed</h1><p>Failed to exchange authorization code for token for agent '{agent_name}' (profile: {profile}).</p>",
                status_code=500)

    async def handle_agent_unlink(request: Request) -> RedirectResponse | HTMLResponse:
        """Unlink OAuth authentication for a specific agent and profile."""
        agent_name = request.path_params.get('agent_name')
        if not agent_name:
            return HTMLResponse(content="<h1>Error: Agent name is required</h1>", status_code=400)

        # URL decode agent name
        agent_name = urllib.parse.unquote(agent_name)

        # Get profile from query parameter (required)
        profile = request.query_params.get('profile')
        if not profile:
            return HTMLResponse(content="<h1>Error: Profile is required</h1>", status_code=400)

        # Look up agent for this profile
        profile_agents = routing_agent.get_agents_for_profile(profile)
        if agent_name not in profile_agents:
            return HTMLResponse(content=f"<h1>Error: Agent '{agent_name}' not found</h1>", status_code=404)

        agent_info = profile_agents[agent_name]
        remote_connection = agent_info['remote_agent_connections']

        # Get OAuth client for the specified profile
        oauth_client = remote_connection.get_oauth_client_for_profile(profile)

        # Unlink token for the specified profile
        success = oauth_client.unlink_token(profile)

        if success:
            # Update the auth header on the remote connection
            remote_connection.update_auth_header_for_profile(profile)
            logger.info(f"Successfully unlinked authentication for {agent_name} (profile: {profile})")
        else:
            logger.warning(f"Failed to unlink authentication for {agent_name} (profile: {profile})")

        # Redirect back to dashboard with profile parameter
        encoded_profile = urllib.parse.quote(profile)
        return RedirectResponse(url=f"/dashboard?profile={encoded_profile}", status_code=303)

    return [
        Route(path='/authenticate', methods=['GET'], endpoint=handle_auth),
        Route(path='/dashboard', methods=['GET'], endpoint=handle_dashboard),
        Route(path='/dashboard/{agent_name}/authenticate', methods=['GET'], endpoint=handle_agent_auth),
        Route(path='/dashboard/{agent_name}/callback', methods=['GET'], endpoint=handle_agent_callback),
        Route(path='/dashboard/{agent_name}/unlink', methods=['POST'], endpoint=handle_agent_unlink),
    ]

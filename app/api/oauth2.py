"""Unified OAuth2 callback handler for all MCP servers.

Provides a single /oauth2/callback endpoint so only one redirect URI
needs to be registered with OAuth providers (e.g., Google Cloud Console).
The agent name, profile, and source are encoded in the OAuth state parameter.
"""

import base64
import urllib.parse

from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse
from starlette.routing import Route

from app.utils import logger


def get_oauth2_routes(routing_agent, pending_return_urls) -> list[Route]:

    async def handle_oauth2_callback(request: Request) -> HTMLResponse | RedirectResponse:
        """Handle OAuth2 callback for any MCP server.

        State format (base64-encoded): {random_token}:{profile}:{source}:{agent_name}
        """
        state = request.query_params.get('state')
        if not state:
            return HTMLResponse(
                content="<h1>Error: Missing state parameter</h1>",
                status_code=400
            )

        # Decode state to extract profile, source, and agent_name
        profile = None
        source = "dashboard"
        agent_name = None
        try:
            decoded_state = base64.urlsafe_b64decode(state).decode()
            parts = decoded_state.split(':')
            if len(parts) >= 2:
                profile = parts[1]
            if len(parts) >= 3:
                source = parts[2]
            if len(parts) >= 4:
                agent_name = ':'.join(parts[3:])  # rejoin in case name contains ':'
        except Exception as e:
            logger.warning(f"Failed to decode OAuth2 state: {e}")
            return HTMLResponse(
                content="<h1>Error: Invalid state parameter</h1>",
                status_code=400
            )

        if not profile:
            return HTMLResponse(
                content="<h1>Error: Profile could not be determined from callback state</h1>",
                status_code=400
            )

        if not agent_name:
            return HTMLResponse(
                content="<h1>Error: Agent name could not be determined from callback state</h1>",
                status_code=400
            )

        # Get authorization code
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
            return HTMLResponse(
                content=f"<h1>Error: Agent '{agent_name}' not found</h1>",
                status_code=404
            )

        agent_info = profile_agents[agent_name]
        remote_connection = agent_info['remote_agent_connections']

        # Get OAuth client for the specified profile
        oauth_client = remote_connection.get_oauth_client_for_profile(profile)

        # Use the single redirect URI (must match what was used in the auth request)
        from app.config.settings import BaseConfig
        redirect_uri = f"{BaseConfig.APP_URL}/oauth2/callback"

        # Exchange code for token
        success = await oauth_client.handle_oauth_callback(code, redirect_uri, state, profile)

        if success:
            # Update auth header on the remote connection for this profile
            remote_connection.update_auth_header_for_profile(profile)

            # For MCP servers: connect immediately to get real server info
            # (name, description, tools) and update agents_info
            mcp_adapter = agent_info.get('mcp_adapter')
            if mcp_adapter:
                try:
                    await mcp_adapter._ensure_auth(profile)
                    logger.info(f"MCP server connected after auth: '{mcp_adapter.name}'")
                except Exception as e:
                    logger.warning(f"Failed to connect MCP server after auth: {e}")

            # If auth was initiated from chat, show close-tab page
            if source == "chat":
                return HTMLResponse(
                    content="<h1>Authentication successful!</h1>"
                    "<p>You can close this tab and continue your conversation.</p>"
                    "<script>setTimeout(function(){ window.close(); }, 2000);</script>"
                )

            # If auth was initiated from API with a return URL
            if source == "api":
                return_url = pending_return_urls.pop((agent_name, profile), None)
                if return_url:
                    separator = '&' if '?' in return_url else '?'
                    return RedirectResponse(url=f"{return_url}{separator}agents=open")

                return HTMLResponse(content="""<!DOCTYPE html>
<html><head><title>Authentication Successful</title>
<style>body{font-family:system-ui,sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;background:#f8fafc;color:#334155;}
.card{text-align:center;padding:40px;border-radius:12px;background:white;box-shadow:0 4px 12px rgba(0,0,0,0.1);}
h1{color:#16a34a;margin-bottom:8px;}p{color:#64748b;}</style></head>
<body><div class="card"><h1>Authentication Successful</h1>
<p>You can close this window and return to the app.</p></div>
<script>setTimeout(function(){window.close()},3000);</script></body></html>""")

            # Default: redirect back to dashboard
            encoded_profile = urllib.parse.quote(profile)
            return RedirectResponse(url=f"/dashboard?profile={encoded_profile}")
        else:
            return HTMLResponse(
                content=f"<h1>Authentication Failed</h1>"
                f"<p>Failed to exchange authorization code for token for agent '{agent_name}' "
                f"(profile: {profile}).</p>",
                status_code=500
            )

    return [
        Route(path='/oauth2/callback', methods=['GET'], endpoint=handle_oauth2_callback),
    ]

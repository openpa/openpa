import inspect
import json
import urllib.parse
import base64

from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, JSONResponse
from starlette.routing import Route

from app.config.settings import BaseConfig
from app.utils import logger


def get_agent_routes(routing_agent, remote_agent_storage, pending_return_urls,
                     mcp_server_storage=None, mcp_llm=None) -> list[Route]:

    async def handle_add_agent(request: Request) -> JSONResponse:
        """API endpoint to add a new remote agent or MCP server.

        Request body:
            url: The agent/server URL (required)
            type: "a2a" (default) or "mcp"
            profile: The profile to add the agent to (required)
        """
        try:
            body = await request.json()
            url = body.get('url')
            agent_type = body.get('type', 'a2a')
            profile = body.get('profile')

            if not url:
                return JSONResponse(
                    content={"error": "URL is required"},
                    status_code=400
                )

            if not profile:
                return JSONResponse(
                    content={"error": "Profile is required"},
                    status_code=400
                )

            if agent_type == 'mcp':
                # Handle MCP server addition
                if not mcp_llm:
                    return JSONResponse(
                        content={"error": "MCP support is not configured"},
                        status_code=500
                    )

                # Check if URL already exists in MCP storage for this profile
                if mcp_server_storage and mcp_server_storage.exists(url, profile=profile):
                    return JSONResponse(
                        content={"error": f"MCP server with URL '{url}' already exists for this profile"},
                        status_code=409
                    )

                # Per-server LLM config (optional)
                llm_provider = body.get('llm_provider')
                llm_model = body.get('llm_model')
                system_prompt = body.get('system_prompt')
                agent_description = body.get('description')

                # Create per-server LLM if custom config provided
                if llm_provider or llm_model:
                    from app.lib.llm.factory import create_llm_provider
                    try:
                        server_llm = create_llm_provider(
                            provider_name=llm_provider or BaseConfig.get_default_provider(),
                            model_name=llm_model,
                        )
                    except ValueError as e:
                        return JSONResponse(
                            content={"error": f"Invalid LLM configuration: {str(e)}"},
                            status_code=400
                        )
                else:
                    server_llm = mcp_llm

                server_name = await routing_agent.add_mcp_server(
                    url,
                    server_llm,
                    system_prompt=system_prompt,
                    description=agent_description,
                    mcp_server_storage=mcp_server_storage,
                    profile=profile,
                )

                if not server_name:
                    return JSONResponse(
                        content={
                            "error": f"Failed to connect to MCP server at '{url}'. "
                                     "Please check the URL and ensure the server is running."},
                        status_code=400)

                # Persist config in MCP storage
                if mcp_server_storage:
                    config = {"url": url}
                    if llm_provider:
                        config["llm_provider"] = llm_provider
                    if llm_model:
                        config["llm_model"] = llm_model
                    if system_prompt:
                        config["system_prompt"] = system_prompt
                    if agent_description:
                        config["description"] = agent_description
                    mcp_server_storage.add_agent(url, config, profile=profile)

                # Get auth status
                profile_agents = routing_agent.get_agents_for_profile(profile)
                agent_info = profile_agents.get(server_name)
                auth_status = "unknown"
                if agent_info:
                    oauth_client = agent_info['remote_agent_connections'].get_oauth_client_for_profile("default")
                    auth_status = oauth_client.get_auth_status()

                return JSONResponse(
                    content={
                        "success": True,
                        "agent": {
                            "name": server_name,
                            "description": agent_info['card'].description if agent_info else "MCP Server",
                            "url": url,
                            "auth_status": auth_status,
                            "type": "mcp",
                        }
                    },
                    status_code=201
                )

            else:
                # Existing A2A agent addition logic
                # Check if URL already exists in storage for this profile
                if remote_agent_storage.exists(url, profile=profile):
                    return JSONResponse(
                        content={"error": f"Agent with URL '{url}' already exists for this profile"},
                        status_code=409
                    )

                # Add to routing agent (this will resolve the agent card)
                card = await routing_agent.add_agent(url, profile=profile)

                if not card:
                    return JSONResponse(
                        content={
                            "error": f"Failed to connect to agent at '{url}'. "
                                     "Please check the URL and ensure the agent is running."},
                        status_code=400)

                # Add to storage
                remote_agent_storage.add_agent(url, profile=profile)

                # Get auth status for response
                profile_agents = routing_agent.get_agents_for_profile(profile)
                agent_info = profile_agents.get(card.name)
                auth_status = "not_supported"
                if agent_info:
                    oauth_client = agent_info['remote_agent_connections'].oauth_client
                    if oauth_client:
                        auth_status = oauth_client.get_auth_status()

                return JSONResponse(
                    content={
                        "success": True,
                        "agent": {
                            "name": card.name,
                            "description": card.description or "No description available",
                            "url": url,
                            "auth_status": auth_status,
                            "type": "a2a",
                        }
                    },
                    status_code=201
                )

        except json.JSONDecodeError:
            return JSONResponse(
                content={"error": "Invalid JSON body"},
                status_code=400
            )
        except Exception as e:
            logger.error(f"Error adding agent: {e}")
            return JSONResponse(
                content={"error": f"Internal server error: {str(e)}"},
                status_code=500
            )

    async def handle_remove_agent(request: Request) -> JSONResponse:
        """API endpoint to remove a remote agent or MCP server."""
        try:
            agent_name = request.path_params.get('agent_name')
            if not agent_name:
                return JSONResponse(
                    content={"error": "Agent name is required"},
                    status_code=400
                )

            # URL decode agent name
            agent_name = urllib.parse.unquote(agent_name)

            profile = request.query_params.get('profile')
            if not profile:
                return JSONResponse(
                    content={"error": "Profile is required"},
                    status_code=400
                )

            # Get agent info for this profile
            profile_agents = routing_agent.get_agents_for_profile(profile)
            agent_info = profile_agents.get(agent_name)
            if not agent_info:
                return JSONResponse(
                    content={"error": f"Agent '{agent_name}' not found for this profile"},
                    status_code=404
                )

            # Block removal of default stdio MCP servers
            if agent_info.get('is_default'):
                return JSONResponse(
                    content={"error": f"Agent '{agent_name}' is a default server and cannot be removed"},
                    status_code=403
                )

            agent_url = agent_info.get('url', '')
            agent_type = agent_info.get('agent_type', 'a2a')

            # Remove from routing agent
            routing_agent.remove_agent(agent_name, profile=profile)

            # Remove from appropriate storage
            if agent_type == 'mcp' and mcp_server_storage:
                mcp_server_storage.remove_agent(agent_url, profile=profile)
            else:
                remote_agent_storage.remove_agent(agent_url, profile=profile)

            return JSONResponse(
                content={
                    "success": True,
                    "message": f"Agent '{agent_name}' removed successfully"
                },
                status_code=200
            )

        except Exception as e:
            logger.error(f"Error removing agent: {e}")
            return JSONResponse(
                content={"error": f"Internal server error: {str(e)}"},
                status_code=500
            )

    async def handle_toggle_agent_enabled(request: Request) -> JSONResponse:
        """API endpoint to enable or disable an agent."""
        try:
            agent_name = request.path_params.get('agent_name')
            if not agent_name:
                return JSONResponse(content={"error": "Agent name is required"}, status_code=400)

            agent_name = urllib.parse.unquote(agent_name)

            body = await request.json()
            enabled = body.get('enabled')
            profile = body.get('profile')
            if enabled is None:
                return JSONResponse(content={"error": "'enabled' field is required"}, status_code=400)
            if not profile:
                return JSONResponse(content={"error": "Profile is required"}, status_code=400)

            enabled = bool(enabled)

            # Resolve the agent in agents_info
            key = routing_agent._resolve_key(agent_name, profile)
            if not key:
                return JSONResponse(content={"error": f"Agent '{agent_name}' not found"}, status_code=404)

            agent_info = routing_agent.agents_info[key]
            agent_url = agent_info.get('url', '')
            is_shared = agent_info.get('profile') == "__shared__"

            if is_shared:
                # For shared (stdio) servers: use per-profile override so each
                # profile has its own enabled state without affecting others.
                routing_agent.set_shared_override(agent_name, profile, enabled=enabled)
            else:
                # For profile-scoped agents: update directly
                agent_info['enabled'] = enabled

            # Persist to MCP storage under the appropriate profile
            if mcp_server_storage:
                storage_profile = profile if is_shared else agent_info.get('profile', profile)
                existing_config = mcp_server_storage.get_agent_config(agent_url, profile=storage_profile)
                if existing_config:
                    existing_config['enabled'] = enabled
                    mcp_server_storage.update_agent_config(agent_url, existing_config, profile=storage_profile)
                else:
                    mcp_server_storage.add_agent(agent_url, {"url": agent_url, "enabled": enabled}, profile=storage_profile)

            logger.info(f"Agent '{agent_name}' enabled={enabled} (profile={profile})")
            return JSONResponse(
                content={"success": True, "enabled": enabled},
                status_code=200
            )

        except json.JSONDecodeError:
            return JSONResponse(content={"error": "Invalid JSON body"}, status_code=400)
        except Exception as e:
            logger.error(f"Error toggling agent enabled: {e}")
            return JSONResponse(content={"error": f"Internal server error: {str(e)}"}, status_code=500)

    async def handle_list_agents(request: Request) -> JSONResponse:
        """API endpoint to list all remote agents with their info for a specific profile."""
        try:
            # Get profile from query parameter (required)
            profile = request.query_params.get('profile')
            if not profile:
                return JSONResponse(
                    content={"error": "Profile is required. Please create and select a profile."},
                    status_code=400
                )

            agents = []

            # Get only agents visible to this profile (profile-owned + shared)
            profile_agents = routing_agent.get_agents_for_profile(profile)

            for agent_name, agent_info in profile_agents.items():
                remote_connection = agent_info['remote_agent_connections']
                card = agent_info['card']
                url = agent_info['url']

                # Get OAuth client for the specified profile
                oauth_client = remote_connection.get_oauth_client_for_profile(profile)

                # Get auth status for the specified profile
                auth_status = oauth_client.get_auth_status(profile)

                # Map status to badge and text
                status_map = {
                    "not_supported": ("badge-secondary", "Agent does not support authentication"),
                    "authenticated": ("badge-success", "Agent has been successfully authenticated"),
                    "not_authenticated": ("badge-danger", "Agent has not been authenticated yet"),
                    "expired": ("badge-warning", "Agent's token has expired")
                }

                badge_class, status_text = status_map.get(auth_status, ("badge-secondary", "Unknown"))

                # Get description
                description = card.description if card.description else "No description available"
                if len(description) > 100:
                    description = description[:97] + "..."

                # Get expiration info for authenticated agents
                expiration_info = None
                if auth_status == "authenticated":
                    expiration_info = oauth_client.get_expiration_info(profile)

                # Determine which buttons to show
                show_authenticate = auth_status in ["not_authenticated", "expired"]
                show_unlink = auth_status == "authenticated" or (auth_status == "expired" and oauth_client.get_token(profile))

                # Get arguments schema from agent card
                arguments_schema = agent_info.get('arguments_schema')

                # Get MCP config if this is an MCP agent
                mcp_config = None
                agent_type = agent_info.get('agent_type', 'a2a')
                if agent_type == 'mcp' and mcp_server_storage:
                    agent_profile = agent_info.get('profile', profile)
                    storage_profile = profile if agent_profile == "__shared__" else agent_profile
                    mcp_config = mcp_server_storage.get_agent_config(url, profile=storage_profile)

                agents.append({
                    'name': agent_name,
                    'encoded_name': urllib.parse.quote(agent_name),
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
                    'config_name': agent_info.get('config_name'),
                })

            return JSONResponse(content={"agents": agents}, status_code=200)

        except Exception as e:
            logger.error(f"Error listing agents: {e}")
            return JSONResponse(
                content={"error": f"Internal server error: {str(e)}"},
                status_code=500
            )

    async def handle_get_auth_url(request: Request) -> JSONResponse:
        """API endpoint to get the OAuth authorization URL for a specific agent and profile."""
        try:
            agent_name = request.path_params.get('agent_name')
            if not agent_name:
                return JSONResponse(content={"error": "Agent name is required"}, status_code=400)

            agent_name = urllib.parse.unquote(agent_name)
            profile = request.query_params.get('profile')
            if not profile:
                return JSONResponse(
                    content={"error": "Profile is required. Please create and select a profile."},
                    status_code=400
                )
            return_url = request.query_params.get('return_url')

            # Resolve agent for this profile
            profile_agents = routing_agent.get_agents_for_profile(profile)
            if agent_name not in profile_agents:
                return JSONResponse(content={"error": f"Agent '{agent_name}' not found"}, status_code=404)

            agent_info = profile_agents[agent_name]
            remote_connection = agent_info['remote_agent_connections']
            oauth_client = remote_connection.get_oauth_client_for_profile(profile)

            # Use unified /oauth2/callback for MCP servers, per-agent callback for A2A
            agent_type = agent_info.get('agent_type', 'a2a')
            if agent_type == 'mcp':
                redirect_uri = f"{BaseConfig.APP_URL}/oauth2/callback"
            else:
                encoded_name = urllib.parse.quote(agent_name)
                redirect_uri = f"{BaseConfig.APP_URL}/api/agents/{encoded_name}/callback"

            auth_url = oauth_client.get_auth_url(redirect_uri, profile, source="api")
            if inspect.isawaitable(auth_url):
                auth_url = await auth_url
            if not auth_url:
                return JSONResponse(
                    content={"error": f"Agent '{agent_name}' does not support OAuth authentication"},
                    status_code=400
                )

            # Store return URL for post-auth redirect
            if return_url:
                pending_return_urls[(agent_name, profile)] = return_url

            return JSONResponse(
                content={"auth_url": auth_url, "agent_name": agent_name},
                status_code=200
            )

        except Exception as e:
            logger.error(f"Error getting auth URL: {e}")
            return JSONResponse(content={"error": f"Internal server error: {str(e)}"}, status_code=500)

    async def handle_api_agent_callback(request: Request) -> HTMLResponse:
        """Handle OAuth callback from provider, showing a close-window page on success."""
        agent_name = request.path_params.get('agent_name')
        if not agent_name:
            return HTMLResponse(content="<h1>Error: Agent name is required</h1>", status_code=400)

        agent_name = urllib.parse.unquote(agent_name)

        state = request.query_params.get('state')
        profile = None
        if state:
            try:
                decoded_state = base64.urlsafe_b64decode(state).decode()
                parts = decoded_state.split(':')
                if len(parts) >= 2:
                    profile = parts[1]
            except Exception as e:
                logger.warning(f"Failed to decode profile from state: {e}")

        if not profile:
            return HTMLResponse(
                content="<h1>Error: Profile could not be determined from callback state</h1>",
                status_code=400
            )

        # Resolve agent for this profile
        profile_agents = routing_agent.get_agents_for_profile(profile)
        if agent_name not in profile_agents:
            return HTMLResponse(content=f"<h1>Error: Agent '{agent_name}' not found</h1>", status_code=404)

        code = request.query_params.get('code')
        if not code:
            error = request.query_params.get('error', 'Unknown error')
            error_description = request.query_params.get('error_description', '')
            return HTMLResponse(
                content=f"<h1>Authentication Failed</h1><p>Error: {error}</p><p>{error_description}</p>",
                status_code=400
            )

        agent_info = profile_agents[agent_name]
        remote_connection = agent_info['remote_agent_connections']
        oauth_client = remote_connection.get_oauth_client_for_profile(profile)

        encoded_name = urllib.parse.quote(agent_name)
        redirect_uri = f"{BaseConfig.APP_URL}/api/agents/{encoded_name}/callback"

        success = await oauth_client.handle_oauth_callback(code, redirect_uri, state, profile)

        if success:
            remote_connection.update_auth_header_for_profile(profile)

            # For MCP servers: connect immediately to get real server info
            mcp_adapter = agent_info.get('mcp_adapter')
            if mcp_adapter:
                try:
                    await mcp_adapter._ensure_auth(profile)
                    logger.info(f"MCP server connected after auth: '{mcp_adapter.name}'")
                except Exception as e:
                    logger.warning(f"Failed to connect MCP server after auth: {e}")

            # Redirect to return URL if one was stored during auth-url request
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
        else:
            return HTMLResponse(
                content=f"<h1>Authentication Failed</h1><p>Failed to exchange authorization code for token for agent '{agent_name}' (profile: {profile}).</p>",
                status_code=500
            )

    async def handle_api_agent_unlink(request: Request) -> JSONResponse:
        """API endpoint to unlink OAuth authentication for a specific agent and profile."""
        try:
            agent_name = request.path_params.get('agent_name')
            if not agent_name:
                return JSONResponse(content={"error": "Agent name is required"}, status_code=400)

            agent_name = urllib.parse.unquote(agent_name)
            profile = request.query_params.get('profile')
            if not profile:
                return JSONResponse(
                    content={"error": "Profile is required. Please create and select a profile."},
                    status_code=400
                )

            # Resolve agent for this profile
            profile_agents = routing_agent.get_agents_for_profile(profile)
            if agent_name not in profile_agents:
                return JSONResponse(content={"error": f"Agent '{agent_name}' not found"}, status_code=404)

            agent_info = profile_agents[agent_name]
            remote_connection = agent_info['remote_agent_connections']
            oauth_client = remote_connection.get_oauth_client_for_profile(profile)

            success = oauth_client.unlink_token(profile)

            if success:
                remote_connection.update_auth_header_for_profile(profile)
                logger.info(f"API: Successfully unlinked authentication for {agent_name} (profile: {profile})")
                return JSONResponse(
                    content={"success": True, "message": f"Authentication unlinked for '{agent_name}'"},
                    status_code=200
                )
            else:
                return JSONResponse(
                    content={"success": False, "message": f"No authentication token found for '{agent_name}'"},
                    status_code=200
                )

        except Exception as e:
            logger.error(f"Error unlinking agent: {e}")
            return JSONResponse(content={"error": f"Internal server error: {str(e)}"}, status_code=500)

    async def handle_get_agent_config(request: Request) -> JSONResponse:
        """API endpoint to get the stored config for an MCP server."""
        try:
            agent_name = request.path_params.get('agent_name')
            if not agent_name:
                return JSONResponse(content={"error": "Agent name is required"}, status_code=400)

            agent_name = urllib.parse.unquote(agent_name)
            profile = request.query_params.get('profile')
            if not profile:
                return JSONResponse(content={"error": "Profile is required"}, status_code=400)

            profile_agents = routing_agent.get_agents_for_profile(profile)
            agent_info = profile_agents.get(agent_name)
            if not agent_info:
                return JSONResponse(content={"error": f"Agent '{agent_name}' not found"}, status_code=404)

            if agent_info.get('agent_type') != 'mcp':
                return JSONResponse(
                    content={"error": f"Agent '{agent_name}' is not an MCP server"},
                    status_code=400
                )

            url = agent_info.get('url', '')
            agent_profile = agent_info.get('profile', profile)
            storage_profile = profile if agent_profile == "__shared__" else agent_profile
            config = mcp_server_storage.get_agent_config(url, profile=storage_profile) if mcp_server_storage else None

            return JSONResponse(content={"config": config or {"url": url}}, status_code=200)

        except Exception as e:
            logger.error(f"Error getting agent config: {e}")
            return JSONResponse(content={"error": f"Internal server error: {str(e)}"}, status_code=500)

    async def handle_update_agent_config(request: Request) -> JSONResponse:
        """API endpoint to update the LLM/prompt config for an MCP server."""
        try:
            agent_name = request.path_params.get('agent_name')
            if not agent_name:
                return JSONResponse(content={"error": "Agent name is required"}, status_code=400)

            agent_name = urllib.parse.unquote(agent_name)

            body = await request.json()
            profile = body.get('profile') or request.query_params.get('profile')
            if not profile:
                return JSONResponse(content={"error": "Profile is required"}, status_code=400)

            profile_agents = routing_agent.get_agents_for_profile(profile)
            agent_info = profile_agents.get(agent_name)
            if not agent_info:
                return JSONResponse(content={"error": f"Agent '{agent_name}' not found"}, status_code=404)

            if agent_info.get('agent_type') != 'mcp':
                return JSONResponse(
                    content={"error": f"Agent '{agent_name}' is not an MCP server"},
                    status_code=400
                )

            llm_provider = body.get('llm_provider')
            llm_model = body.get('llm_model')
            system_prompt = body.get('system_prompt')
            agent_description = body.get('description')

            mcp_adapter = agent_info.get('mcp_adapter')
            if not mcp_adapter:
                return JSONResponse(
                    content={"error": f"MCP adapter not found for agent '{agent_name}'"},
                    status_code=500
                )

            # Create new LLM if provider/model changed
            new_llm = None
            if llm_provider is not None or llm_model is not None:
                from app.lib.llm.factory import create_llm_provider
                try:
                    new_llm = create_llm_provider(
                        provider_name=llm_provider or BaseConfig.get_default_provider(),
                        model_name=llm_model if llm_model else None,
                    )
                except ValueError as e:
                    return JSONResponse(
                        content={"error": f"Invalid LLM configuration: {str(e)}"},
                        status_code=400
                    )

            # Update adapter in-place
            mcp_adapter.update_config(
                llm=new_llm,
                system_prompt=system_prompt if system_prompt is not None else None,
                description=agent_description if agent_description is not None else None,
            )

            # Regenerate synthetic card if description changed
            if agent_description is not None:
                agent_info['card'] = mcp_adapter.create_synthetic_card()

            # Persist to storage
            url = agent_info.get('url', '')
            agent_profile = agent_info.get('profile', profile)
            storage_profile = profile if agent_profile == "__shared__" else agent_profile
            if mcp_server_storage:
                existing_config = mcp_server_storage.get_agent_config(url, profile=storage_profile) or {"url": url}
                if llm_provider is not None:
                    existing_config["llm_provider"] = llm_provider if llm_provider else None
                if llm_model is not None:
                    existing_config["llm_model"] = llm_model if llm_model else None
                if system_prompt is not None:
                    existing_config["system_prompt"] = system_prompt if system_prompt else None
                if agent_description is not None:
                    existing_config["description"] = agent_description if agent_description else None
                # Clean up None values
                existing_config = {k: v for k, v in existing_config.items() if v is not None}
                existing_config["url"] = url  # always keep url
                if not mcp_server_storage.update_agent_config(url, existing_config, profile=storage_profile):
                    # Entry doesn't exist yet (e.g., shared stdio server with no prior config)
                    mcp_server_storage.add_agent(url, existing_config, profile=storage_profile)

            return JSONResponse(
                content={
                    "success": True,
                    "message": f"Configuration updated for '{agent_name}'",
                    "config": mcp_server_storage.get_agent_config(url, profile=storage_profile) if mcp_server_storage else {"url": url},
                },
                status_code=200
            )

        except json.JSONDecodeError:
            return JSONResponse(content={"error": "Invalid JSON body"}, status_code=400)
        except Exception as e:
            logger.error(f"Error updating agent config: {e}")
            return JSONResponse(content={"error": f"Internal server error: {str(e)}"}, status_code=500)

    return [
        Route(path='/api/agents', methods=['POST'], endpoint=handle_add_agent),
        Route(path='/api/agents', methods=['GET'], endpoint=handle_list_agents),
        Route(path='/api/agents/{agent_name}', methods=['DELETE'], endpoint=handle_remove_agent),
        Route(path='/api/agents/{agent_name}/config', methods=['GET'], endpoint=handle_get_agent_config),
        Route(path='/api/agents/{agent_name}/config', methods=['PUT'], endpoint=handle_update_agent_config),
        Route(path='/api/agents/{agent_name}/enabled', methods=['PUT'], endpoint=handle_toggle_agent_enabled),
        Route(path='/api/agents/{agent_name}/auth-url', methods=['GET'], endpoint=handle_get_auth_url),
        Route(path='/api/agents/{agent_name}/callback', methods=['GET'], endpoint=handle_api_agent_callback),
        Route(path='/api/agents/{agent_name}/unlink', methods=['POST'], endpoint=handle_api_agent_unlink),
    ]

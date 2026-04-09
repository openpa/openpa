import json
import uuid

from typing import Any, AsyncGenerator, Dict, List, Optional, TypedDict

import httpx

from a2a.client import A2ACardResolver
from a2a.types import (
    AgentCard,
    MessageSendParams,
    SendMessageRequest,
    SendStreamingMessageRequest,
    SendMessageResponse,
    SendMessageSuccessResponse,
    SendStreamingMessageSuccessResponse,
    Task,
    TaskArtifactUpdateEvent,
    TaskStatusUpdateEvent,
    Message,
    TaskState
)

from app.constants import ChatCompletionTypeEnum
from app.types import AgentInfo, ChatCompletionStreamResponseType
from .remote_agent_connection import (
    RemoteAgentConnections,
    TaskUpdateCallback,
)

from app.utils import logger

SHARED_PROFILE = "__shared__"


class RoutingAgent:
    """The Routing agent.

    This is the agent responsible for choosing which remote seller agents
    to send tasks to and coordinate their work.

    agents_info keys use the format "{profile}::{agent_name}" for profile-scoped
    agents, and plain "{agent_name}" for shared (stdio) agents.
    """

    def __init__(
        self,
        task_callback: TaskUpdateCallback | None = None,
    ):
        self.task_callback = task_callback
        self.agents_info: dict[str, AgentInfo] = {}
        # Per-profile config overrides for shared agents (e.g., enabled/disabled state).
        # Key: (agent_display_name, profile) -> dict of overrides to merge into AgentInfo.
        self._shared_overrides: dict[tuple[str, str], dict] = {}

    # --- Per-profile overrides for shared agents ---

    def set_shared_override(self, agent_name: str, profile: str, **overrides) -> None:
        """Set per-profile config overrides for a shared agent."""
        key = (agent_name, profile)
        existing = self._shared_overrides.get(key, {})
        existing.update(overrides)
        self._shared_overrides[key] = existing

    def get_shared_override(self, agent_name: str, profile: str) -> dict:
        """Get per-profile config overrides for a shared agent."""
        return self._shared_overrides.get((agent_name, profile), {})

    # --- Profile key helpers ---

    @staticmethod
    def _make_key(agent_name: str, profile: str) -> str:
        """Create a profile-qualified key for agents_info."""
        if profile == SHARED_PROFILE:
            return agent_name
        return f"{profile}::{agent_name}"

    @staticmethod
    def _parse_key(key: str) -> tuple[str, str]:
        """Parse a profile-qualified key into (profile, agent_name)."""
        if "::" in key:
            profile, name = key.split("::", 1)
            return profile, name
        return SHARED_PROFILE, key

    def get_agents_for_profile(self, profile: str) -> dict[str, AgentInfo]:
        """Return agents visible to a profile: profile-owned + shared.

        For shared agents, per-profile overrides (e.g., enabled state) are applied
        by returning a shallow copy with overrides merged in.

        Returns a dict keyed by display name (without profile prefix).
        """
        result = {}
        for key, info in self.agents_info.items():
            p, name = self._parse_key(key)
            if p == profile:
                result[name] = info
            elif p == SHARED_PROFILE:
                overrides = self._shared_overrides.get((name, profile))
                if overrides:
                    # Return a copy with per-profile overrides applied
                    result[name] = {**info, **overrides}
                else:
                    result[name] = info
        return result

    def _resolve_key(self, agent_name: str, profile: str) -> str | None:
        """Resolve an agent display name to its agents_info key.

        Tries profile-scoped key first, then shared key.
        """
        profile_key = self._make_key(agent_name, profile)
        if profile_key in self.agents_info:
            return profile_key
        # Fall back to shared key
        if agent_name in self.agents_info:
            return agent_name
        return None

    def remove_all_for_profile(self, profile: str) -> None:
        """Remove all agents_info entries for a specific profile."""
        keys_to_remove = [
            key for key in self.agents_info
            if self._parse_key(key)[0] == profile
        ]
        for key in keys_to_remove:
            del self.agents_info[key]
        if keys_to_remove:
            logger.info(f"Removed {len(keys_to_remove)} agent(s) from routing agent for profile '{profile}'")

    # --- Initialization ---

    async def _async_init_components(
        self, remote_agent_addresses: dict[str, list[str]]
    ) -> None:
        """Asynchronous part of initialization.

        Args:
            remote_agent_addresses: Dict mapping profile names to lists of agent URLs.
        """
        # Use a single httpx.AsyncClient for all card resolutions for efficiency
        async with httpx.AsyncClient(timeout=30) as client:
            for profile, addresses in remote_agent_addresses.items():
                for address in addresses:
                    card_resolver = A2ACardResolver(
                        client, address
                    )  # Constructor is sync
                    try:
                        card = (
                            await card_resolver.get_agent_card()
                        )  # get_agent_card is async

                        # Fetch raw card JSON to capture extra fields like 'arguments'
                        # (Pydantic strips unknown fields during AgentCard parsing)
                        arguments_schema = None
                        try:
                            raw_response = await client.get(
                                f"{address.rstrip('/')}/.well-known/agent.json"
                            )
                            if raw_response.status_code == 200:
                                raw_card_data = raw_response.json()
                                arguments_schema = raw_card_data.get("arguments")
                        except Exception as e:
                            logger.warning(
                                f'Failed to fetch raw agent card from {address}: {e}'
                            )

                        remote_connection = RemoteAgentConnections(
                            agent_card=card, agent_url=address
                        )
                        await remote_connection.authenticate()

                        key = self._make_key(card.name, profile)
                        self.agents_info[key] = {
                            'remote_agent_connections': remote_connection,
                            'context_storage': {},
                            'card': card,
                            'url': address,
                            'arguments_schema': arguments_schema,
                            'profile': profile,
                        }
                    except httpx.ConnectError as e:
                        logger.error(
                            f'ERROR: Failed to get agent card from {address}: {e}'
                        )
                    except Exception as e:  # Catch other potential errors
                        logger.error(
                            f'ERROR: Failed to initialize connection for {address}: {e}'
                        )

    @classmethod
    async def create(
        cls,
        remote_agent_addresses: dict[str, list[str]],
        task_callback: TaskUpdateCallback | None = None,
    ) -> 'RoutingAgent':
        """Create and asynchronously initialize an instance of the RoutingAgent.

        Args:
            remote_agent_addresses: Dict mapping profile names to lists of agent URLs.
        """
        instance = cls(task_callback)
        await instance._async_init_components(remote_agent_addresses)
        return instance

    async def add_agent(self, address: str, profile: str) -> AgentCard | None:
        """Add a new remote agent dynamically for a specific profile.

        Args:
            address: The agent URL to add
            profile: The profile to add the agent to

        Returns:
            The AgentCard if successful, None if failed
        """
        # Check if URL already exists for this profile
        for key, agent_info in self.agents_info.items():
            p, _ = self._parse_key(key)
            if p == profile and agent_info['url'] == address:
                logger.warning(f"Agent with URL {address} already exists for profile '{profile}'")
                return None

        async with httpx.AsyncClient(timeout=30) as client:
            card_resolver = A2ACardResolver(client, address)
            try:
                card = await card_resolver.get_agent_card()

                # Check if agent with this name already exists for this profile
                key = self._make_key(card.name, profile)
                if key in self.agents_info:
                    logger.warning(f"Agent with name '{card.name}' already exists for profile '{profile}'")
                    return None

                # Fetch raw card JSON to capture extra fields like 'arguments'
                arguments_schema = None
                try:
                    raw_response = await client.get(
                        f"{address.rstrip('/')}/.well-known/agent.json"
                    )
                    if raw_response.status_code == 200:
                        raw_card_data = raw_response.json()
                        arguments_schema = raw_card_data.get("arguments")
                except Exception as e:
                    logger.warning(
                        f'Failed to fetch raw agent card from {address}: {e}'
                    )

                remote_connection = RemoteAgentConnections(
                    agent_card=card, agent_url=address
                )
                await remote_connection.authenticate()

                self.agents_info[key] = {
                    'remote_agent_connections': remote_connection,
                    'context_storage': {},
                    'card': card,
                    'url': address,
                    'arguments_schema': arguments_schema,
                    'profile': profile,
                }

                logger.info(f"Successfully added agent '{card.name}' from {address} (profile={profile})")
                return card

            except httpx.ConnectError as e:
                logger.error(f"Failed to connect to agent at {address}: {e}")
                return None
            except Exception as e:
                logger.error(f"Failed to add agent from {address}: {e}")
                return None

    def remove_agent(self, agent_name: str, profile: str) -> bool:
        """Remove an agent by name for a specific profile.

        Args:
            agent_name: The name of the agent to remove
            profile: The profile to remove the agent from

        Returns:
            True if the agent was removed, False if not found
        """
        key = self._make_key(agent_name, profile)
        if key in self.agents_info:
            del self.agents_info[key]
            logger.info(f"Agent '{agent_name}' removed from routing agent (profile={profile})")
            return True
        logger.warning(f"Agent '{agent_name}' not found for profile '{profile}'")
        return False

    async def _probe_mcp_server(self, url: str) -> int:
        """Probe an MCP server URL with a simple HTTP POST to check if it requires auth.

        Returns the HTTP status code (200 for open, 401 for auth required, etc.)
        """
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                # MCP servers expect POST to the endpoint; a minimal probe
                resp = await client.post(url, json={"jsonrpc": "2.0", "method": "ping", "id": 1})
                return resp.status_code
        except Exception as e:
            logger.debug(f"Probe to {url} failed: {e}")
            return 0

    async def add_mcp_server(
        self,
        url: str,
        llm,
        mcp_auth_url: Optional[str] = None,
        system_prompt: Optional[str] = None,
        description: Optional[str] = None,
        mcp_server_storage=None,
        stored_server_name: Optional[str] = None,
        profile: str = "default",
    ) -> Optional[str]:
        """Add an MCP server via HTTP/SSE transport for a specific profile.

        Handles servers that require authentication: probes the server first,
        and if it returns 401, discovers OAuth metadata and registers the server
        so the user can authenticate from the Dashboard.

        Args:
            url: The MCP server URL (e.g., "http://localhost:9000/mcp")
            llm: LLM provider for the MCP agent adapter
            mcp_auth_url: Optional base URL for OAuth discovery (defaults to url's base)
            system_prompt: Optional custom system prompt for this MCP agent
            description: Optional agent description override
            mcp_server_storage: Optional storage backend to persist server info after auth
            stored_server_name: Optional previously-stored server name (from prior auth)
            profile: The profile to add the MCP server to

        Returns:
            The server name if successful, None if failed
        """
        from app.tools.mcp.mcp_connection import MCPConnection
        from app.tools.mcp.mcp_agent_adapter import MCPAgentAdapter
        from app.tools.mcp.mcp_auth import MCPOAuthClient
        from app.tools.mcp.mcp_remote_shim import MCPRemoteConnectionShim

        auth_base_url = mcp_auth_url or url.rsplit('/', 1)[0]  # strip path to get base

        try:
            # Probe the server first to check if it requires auth
            # (avoids the MCP client crashing on 401)
            status = await self._probe_mcp_server(url)

            if status == 401:
                logger.info(f"MCP server at {url} requires authentication (401), discovering OAuth metadata...")

                # Discover OAuth metadata from .well-known
                mcp_auth = MCPOAuthClient(auth_base_url, server_name="")
                has_auth = await mcp_auth.discover_auth_metadata()

                if not has_auth:
                    logger.error(f"MCP server at {url} requires auth but no OAuth metadata found")
                    return None

                server_name = stored_server_name or self._derive_server_name(url)
                mcp_auth.server_name = server_name

                key = self._make_key(server_name, profile)
                if key in self.agents_info:
                    logger.warning(f"Agent/MCP server with name '{server_name}' already exists for profile '{profile}'")
                    return None

                # Create a connection stub (not connected yet, will connect after auth)
                connection = MCPConnection()
                connection._url = url

                # Callback to update agents_info after first successful authenticated connection.
                # Re-keys agents_info from derived name to real server name, and updates card.
                # Also persists the real server name/description to storage so they survive restarts.
                # NOTE: Token storage still uses the old derived name (already saved under it).
                def on_first_connect(adapter, _old_name=server_name, _agents_info=self.agents_info,
                                     _storage=mcp_server_storage, _url=url, _profile=profile):
                    real_name = adapter.name
                    new_card = adapter.create_synthetic_card()

                    old_key = RoutingAgent._make_key(_old_name, _profile)
                    new_key = RoutingAgent._make_key(real_name, _profile)

                    if old_key in _agents_info:
                        entry = _agents_info.pop(old_key)
                        entry['card'] = new_card
                        shim = entry['remote_agent_connections']
                        shim.server_name = real_name
                        # Sync _mcp_auth.server_name so display name is consistent
                        if shim._mcp_auth:
                            shim._mcp_auth.server_name = real_name
                        # Clear per-profile client cache so clients are recreated
                        # with current state on next access
                        shim._profile_oauth_clients.clear()
                        _agents_info[new_key] = entry
                        logger.info(
                            f"Updated MCP server info: '{_old_name}' -> '{real_name}' "
                            f"(profile={_profile}, tools: {[s.name for s in new_card.skills]})"
                        )

                    # Persist real server name and description to storage for restart recovery
                    if _storage:
                        existing_config = _storage.get_agent_config(_url, profile=_profile)
                        if existing_config:
                            existing_config["server_name"] = real_name
                            existing_config["description"] = new_card.description
                            _storage.update_agent_config(_url, existing_config, profile=_profile)
                            logger.info(f"Persisted MCP server info to storage: name='{real_name}' (profile={_profile})")
                        else:
                            _storage.add_agent(_url, {
                                "url": _url,
                                "server_name": real_name,
                                "description": new_card.description,
                            }, profile=_profile)

                adapter = MCPAgentAdapter(
                    connection=connection,
                    llm=llm,
                    mcp_auth=mcp_auth,
                    description=description or f"MCP Server at {url} (authentication required)",
                    name=server_name,
                    on_first_connect=on_first_connect,
                    system_prompt=system_prompt,
                )

                shim = MCPRemoteConnectionShim(
                    server_name=server_name,
                    mcp_auth=mcp_auth,
                )

                synthetic_card = adapter.create_synthetic_card()

                self.agents_info[key] = {
                    'remote_agent_connections': shim,
                    'context_storage': adapter.get_context_storage(),
                    'card': synthetic_card,
                    'url': url,
                    'arguments_schema': None,
                    'agent_type': 'mcp',
                    'mcp_adapter': adapter,
                    'profile': profile,
                }

                logger.info(
                    f"Registered MCP server '{server_name}' from {url} "
                    f"(profile={profile}, pending authentication)"
                )
                return server_name

            # Server doesn't require auth (or probe returned unexpected status) - try full connection
            connection = MCPConnection()
            await connection.connect_http(url)

            server_name = connection.server_name
            if not server_name:
                server_name = self._derive_server_name(url)

            key = self._make_key(server_name, profile)
            if key in self.agents_info:
                logger.warning(f"Agent/MCP server with name '{server_name}' already exists for profile '{profile}'")
                return None

            # Discover OAuth metadata (server may support optional auth)
            mcp_auth = MCPOAuthClient(auth_base_url, server_name)
            has_auth = await mcp_auth.discover_auth_metadata()

            adapter = MCPAgentAdapter(
                connection=connection,
                llm=llm,
                mcp_auth=mcp_auth if has_auth else None,
                description=description,
                system_prompt=system_prompt,
            )

            shim = MCPRemoteConnectionShim(
                server_name=server_name,
                mcp_auth=mcp_auth if has_auth else None,
            )

            synthetic_card = adapter.create_synthetic_card()

            self.agents_info[key] = {
                'remote_agent_connections': shim,
                'context_storage': adapter.get_context_storage(),
                'card': synthetic_card,
                'url': url,
                'arguments_schema': None,
                'agent_type': 'mcp',
                'mcp_adapter': adapter,
                'profile': profile,
            }

            logger.info(f"Successfully added MCP server '{server_name}' from {url} (profile={profile})")
            return server_name

        except Exception as e:
            logger.error(f"Failed to add MCP server from {url}: {e}")
            return None

    @staticmethod
    def _derive_server_name(url: str) -> str:
        """Derive a human-readable server name from a URL."""
        from urllib.parse import urlparse
        parsed = urlparse(url)
        # Use hostname:port as name, e.g. "localhost:9000"
        host = parsed.hostname or "unknown"
        port = parsed.port
        if port:
            return f"MCP-{host}:{port}"
        return f"MCP-{host}"

    async def init_stdio_mcp_servers(self, stdio_configs: List[Dict], llm,
                                      mcp_server_storage=None) -> None:
        """Initialize stdio MCP servers at startup (shared across all profiles).

        Args:
            stdio_configs: List of config dicts with 'name', 'command', 'args',
                          optional 'env', and optional 'auth' for OAuth configuration
            llm: LLM provider for the MCP agent adapters
            mcp_server_storage: Optional storage to load persisted enabled state
        """
        from app.tools.mcp.mcp_connection import MCPConnection
        from app.tools.mcp.mcp_agent_adapter import MCPAgentAdapter
        from app.tools.mcp.mcp_auth import MCPOAuthClient
        from app.tools.mcp.mcp_remote_shim import MCPRemoteConnectionShim

        for config in stdio_configs:
            try:
                connection = MCPConnection()
                await connection.connect_stdio(
                    command=config['command'],
                    args=config.get('args', []),
                    env=config.get('env'),
                )

                # Create per-server LLM if config specifies provider/model
                server_llm = llm
                if config.get('llm_provider') or config.get('llm_model'):
                    from app.lib.llm.factory import create_llm_provider
                    try:
                        server_llm = create_llm_provider(
                            provider_name=config.get('llm_provider') or 'groq',
                            model_name=config.get('llm_model'),
                        )
                    except ValueError as e:
                        logger.error(
                            f"Failed to create LLM for stdio server "
                            f"'{config.get('name', '?')}': {e}, using default"
                        )
                        server_llm = llm

                server_name = connection.server_name or config.get('name', 'unknown')

                if server_name in self.agents_info:
                    logger.warning(f"Stdio MCP server name '{server_name}' conflicts with existing agent, skipping")
                    continue

                # Set up OAuth if auth config is provided
                mcp_auth = None
                auth_config = config.get('auth')
                if auth_config:
                    client_id = auth_config.get('client_id', '')
                    client_secret = auth_config.get('client_secret', '')
                    extra_params = auth_config.get('extra_authorize_params', {})

                    if client_id and client_secret:
                        mcp_auth = MCPOAuthClient(
                            server_url=f"stdio://{server_name}",
                            server_name=server_name,
                            client_id=client_id,
                            client_secret=client_secret,
                            extra_authorize_params=extra_params,
                        )
                        # Set auth metadata directly (no .well-known discovery for stdio)
                        mcp_auth.set_auth_metadata({
                            "authorization_endpoint": auth_config["authorization_endpoint"],
                            "token_endpoint": auth_config["token_endpoint"],
                            "scopes_supported": auth_config.get("scopes_supported", []),
                        })
                        logger.info(f"OAuth configured for stdio MCP server '{server_name}'")
                    else:
                        logger.warning(
                            f"Auth config for '{server_name}' missing client_id or client_secret, "
                            f"skipping OAuth setup. Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET env vars."
                        )

                adapter = MCPAgentAdapter(
                    connection=connection,
                    llm=server_llm,
                    mcp_auth=mcp_auth,
                    description=config.get('description'),
                    system_prompt=config.get('system_prompt'),
                )

                shim = MCPRemoteConnectionShim(
                    server_name=server_name,
                    mcp_auth=mcp_auth,
                )

                synthetic_card = adapter.create_synthetic_card()

                stdio_url = f"stdio://{config['command']} {' '.join(config.get('args', []))}"

                # Shared agents use plain name key (no profile prefix).
                # Default enabled=True; per-profile overrides are loaded separately
                # via load_shared_overrides() at startup.
                self.agents_info[server_name] = {
                    'remote_agent_connections': shim,
                    'context_storage': adapter.get_context_storage(),
                    'card': synthetic_card,
                    'url': stdio_url,
                    'arguments_schema': None,
                    'agent_type': 'mcp',
                    'mcp_adapter': adapter,
                    'is_default': True,
                    'enabled': True,
                    'profile': SHARED_PROFILE,
                    'config_name': config.get('name', server_name),
                }

                logger.info(f"Initialized stdio MCP server '{server_name}'")

            except Exception as e:
                logger.error(f"Failed to initialize stdio MCP server '{config.get('name', '?')}': {e}")
                await connection.cleanup()

    def load_shared_overrides(self, mcp_server_storage) -> None:
        """Load per-profile config overrides for shared (stdio) agents from storage.

        Scans all profiles in storage for config entries matching shared agent URLs,
        and populates _shared_overrides with their enabled state.
        """
        shared_agents = {
            name: info for name, info in self.agents_info.items()
            if info.get('profile') == SHARED_PROFILE
        }
        if not shared_agents:
            return

        shared_url_to_name = {info['url']: name for name, info in shared_agents.items()}

        all_profiles_configs = mcp_server_storage.get_all_profiles_agents()
        for profile, configs in all_profiles_configs.items():
            for config in configs:
                url = config.get("url", "")
                if url not in shared_url_to_name:
                    continue
                agent_name = shared_url_to_name[url]

                # Apply enabled override (existing behavior)
                if 'enabled' in config:
                    self.set_shared_override(agent_name, profile, enabled=config['enabled'])
                    logger.debug(
                        f"Loaded shared override: '{agent_name}' enabled={config['enabled']} "
                        f"for profile '{profile}'"
                    )

                # Apply config overrides (description, system_prompt, LLM)
                mcp_adapter = shared_agents[agent_name].get('mcp_adapter')
                if not mcp_adapter:
                    continue

                has_config_override = any(
                    config.get(k) for k in ('description', 'system_prompt', 'llm_provider', 'llm_model')
                )
                if not has_config_override:
                    continue

                new_llm = None
                if config.get('llm_provider') or config.get('llm_model'):
                    from app.lib.llm.factory import create_llm_provider
                    try:
                        new_llm = create_llm_provider(
                            provider_name=config.get('llm_provider') or 'groq',
                            model_name=config.get('llm_model'),
                        )
                    except ValueError as e:
                        logger.error(
                            f"Failed to create LLM from stored config for "
                            f"'{agent_name}': {e}, keeping code default"
                        )

                mcp_adapter.update_config(
                    llm=new_llm,
                    system_prompt=config.get('system_prompt'),
                    description=config.get('description'),
                )

                if config.get('description'):
                    shared_agents[agent_name]['card'] = mcp_adapter.create_synthetic_card()

                logger.info(
                    f"Applied stored config overrides for shared server '{agent_name}' "
                    f"(profile '{profile}')"
                )

    def get_agent_url(self, agent_name: str, profile: str = "default") -> str | None:
        """Get the URL for a given agent name, resolving profile-scoped keys.

        Args:
            agent_name: The name of the agent
            profile: The profile to look up

        Returns:
            The agent URL if found, None otherwise
        """
        key = self._resolve_key(agent_name, profile)
        if key:
            return self.agents_info[key]['url']
        return None

    async def send_message_streaming(self,
                                     agent_name: str,
                                     message: str,
                                     profile: str,
                                     context_id: str | None = None,
                                     task_id: str | None = None,
                                     metadata: dict | None = None) -> AsyncGenerator[Any,
                                                                                     None]:
        """Send a message with streaming support"""
        key = self._resolve_key(agent_name, profile)
        if not key:
            raise ValueError(f"Agent connection info not available for {agent_name} (profile={profile})")

        agents_info = self.agents_info[key]

        client = agents_info['remote_agent_connections']
        if not client:
            raise ValueError(f"Client not available for {agent_name}")

        context_storage = agents_info['context_storage']

        request_id = str(uuid.uuid4())
        message_id = str(uuid.uuid4())

        payload = {
            "message": {
                "role": "user",
                "parts": [{"type": "text", "text": message}],
                "messageId": message_id,
                "contextId": context_id,
                "taskId": task_id,
                # "referenceTaskIds": [task_id] if task_id else None
            },
            'metadata': metadata or {}
        }

        message_request = SendStreamingMessageRequest(
            id=request_id, params=MessageSendParams.model_validate(payload)
        )

        # logger.info(f"Starting streaming message to agent: {agent_name}")
        logger.debug(f"Profile for authentication: {profile}")
        async for chunk in client.send_message_streaming(message_request, profile=profile):
            if hasattr(chunk.root, 'error') and chunk.root.error:
                logger.error(f"JSONRPC Error from agent {agent_name}: {chunk.root.error}")
                # You might want to yield an error event here
                continue

            if hasattr(chunk.root, 'result'):
                event = chunk.root.result
            else:
                logger.warning(f"Chunk from {agent_name} has no result: {chunk}")
                continue

            # logger.debug(f"Received streaming chunk: {chunk}")
            # logger.debug(chunk.model_dump_json(exclude_none=True, indent=2))

            # Yield the actual event objects for the agent mode
            if isinstance(event, TaskStatusUpdateEvent):
                # Check if task is completed and remove from context_storage
                if hasattr(
                        event,
                        'status') and event.status and hasattr(
                        event.status,
                        'state') and event.status.state == "completed":
                    # Extract context_id from the event to remove from storage
                    event_context_id = getattr(event, 'context_id', None)
                    if event_context_id and event_context_id in context_storage:
                        removed_task_id = context_storage.pop(event_context_id)
                        logger.debug(
                            f"Removed completed task from context_storage: context_id: {event_context_id}, task_id: {removed_task_id}")

                # logger.info(f"Received TaskStatusUpdateEvent: {event.model_dump_json(exclude_none=True, indent=2)}")
                yield event
            elif isinstance(event, TaskArtifactUpdateEvent):
                # logger.info(f"Received TaskArtifactUpdateEvent: {event.model_dump_json(exclude_none=True, indent=2)}")
                yield event
            elif isinstance(event, Task):
                # Extract context_id and task_id from Task if not provided
                task_context_id = getattr(event, 'context_id', None)
                task_task_id = getattr(event, 'id', None)

                if context_id is None and task_context_id:
                    context_id = task_context_id
                if task_id is None and task_task_id:
                    task_id = task_task_id

                # Store in context_storage if both are available
                if context_id and task_id:
                    context_storage[context_id] = task_id
                    logger.debug(f"Stored context_id: {context_id}, task_id: {task_id} in context_storage from Task")

                # logger.info(f"Received Task: {event.model_dump_json(exclude_none=True, indent=2)}")
                yield event
            elif isinstance(event, Message):
                # logger.info(f"Received Message: {event}")
                yield event

    async def request(
            self,
            agent_name: str,
            query: str,
            context_id: str | None = None,
            metadata: dict | None = None,
            profile: str = "default") -> AsyncGenerator[Any, None]:
        """Process a single agent's response and stream events.

        Routes to MCP adapter or A2A client based on agent_type.
        Uses profile to resolve the correct agent in agents_info.
        """
        key = self._resolve_key(agent_name, profile)
        if not key:
            logger.error(f"Agent '{agent_name}' not found in agents_info (profile={profile})")
            return

        agent_info = self.agents_info[key]

        # Route to MCP adapter if agent_type is 'mcp'
        if agent_info.get('agent_type') == 'mcp':
            mcp_adapter = agent_info.get('mcp_adapter')
            if not mcp_adapter:
                logger.error(f"MCP adapter not found for agent '{agent_name}'")
                return

            async for event in mcp_adapter.request(
                query=query,
                context_id=context_id,
                metadata=metadata,
                profile=profile,
            ):
                yield event

                # Check for terminal states
                if isinstance(event, TaskStatusUpdateEvent):
                    if event.status.state in [
                            TaskState.completed, TaskState.failed,
                            TaskState.canceled, TaskState.unknown]:
                        logger.debug(f"[{agent_name}] MCP task reached terminal state: {event.status.state}")
                        break
            return

        # Existing A2A logic
        # Get task_id from context_storage if context_id is provided
        task_id = None
        if context_id:
            context_storage = agent_info['context_storage']
            task_id = context_storage.get(context_id)

        async for event in self.send_message_streaming(
            agent_name, query, profile, context_id, task_id, metadata
        ):
            yield event

            # Check for terminal states to stop consuming the stream
            if isinstance(event, TaskStatusUpdateEvent):
                if event.status.state in [
                        TaskState.completed,
                        TaskState.failed,
                        TaskState.canceled,
                        TaskState.unknown]:
                    logger.debug(f"[{agent_name}] Task reached terminal state: {event.status.state}")
                    break

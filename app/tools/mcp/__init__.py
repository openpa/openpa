"""MCP (Model Context Protocol) integration for olli-agent.

Provides support for connecting to MCP servers via stdio and HTTP transports,
making them appear as regular agents to the Reasoning Agent.
"""

from app.tools.mcp.mcp_connection import MCPConnection
from app.tools.mcp.mcp_agent_adapter import MCPAgentAdapter
from app.tools.mcp.mcp_auth import MCPOAuthClient
from app.tools.mcp.mcp_remote_shim import MCPRemoteConnectionShim

__all__ = [
    "MCPConnection",
    "MCPAgentAdapter",
    "MCPOAuthClient",
    "MCPRemoteConnectionShim",
    "STDIO_MCP_SERVERS",
]

# Stdio MCP server configurations.
# Each entry defines a subprocess-based MCP server to launch at startup.
# Required: 'name', 'command', 'args'.
# Optional: 'env', 'auth' (OAuth), 'description', 'system_prompt',
#           'llm_provider', 'llm_model' (per-server LLM defaults),
#           'model_group' (high/low group assignment from TOML).
# All optional fields default to None (shared defaults apply).
# Dashboard overrides (persisted to DB) take precedence over these code defaults.
STDIO_MCP_SERVERS = [
    {
        "name": "system_file",
        "command": "python",
        "args": ["app/tools/mcp/built-in/system_file.py"],
        "env": {},
        "description": None,
        "system_prompt": None,
        "llm_provider": None,
        "llm_model": None,
        "model_group": "low",
    },
    {
        "name": "markdown_converter",
        "command": "python",
        "args": ["app/tools/mcp/built-in/markdown_converter.py"],
        "env": {},
        "description": None,
        "system_prompt": None,
        "llm_provider": None,
        "llm_model": None,
        "model_group": "low",
    },
    {
        "name": "weather",
        "command": "python",
        "args": ["app/tools/mcp/built-in/weather.py"],
        "env": {},
        "description": None,
        "system_prompt": None,
        "llm_provider": None,
        "llm_model": None,
        "model_group": "low",
    },
    {
        "name": "exec_shell",
        "command": "python",
        "args": ["app/tools/mcp/built-in/exec_shell.py"],
        "env": {},
        "description": None,
        "system_prompt": None,
        "llm_provider": None,
        "llm_model": None,
        "model_group": "low",
    },
    {
        "name": "message_detail",
        "command": "python",
        "args": ["app/tools/mcp/built-in/message_detail.py"],
        "env": {},
        "description": None,
        "system_prompt": None,
        "llm_provider": None,
        "llm_model": None,
        "model_group": "low",
    },
    {
        "name": "gg_calendar",
        "command": "python",
        "args": ["app/tools/mcp/built-in/gg_calendar.py"],
        "env": {},
        "description": None,
        "system_prompt": None,
        "llm_provider": None,
        "llm_model": None,
        "model_group": "low",
    },
]

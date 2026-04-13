from enum import Enum
from typing import Any, Dict, Optional, TypedDict, Required, NotRequired, TYPE_CHECKING

import pandas as pd

from a2a.types import (
    AgentCard
)

if TYPE_CHECKING:
    from app.tools.a2a import RemoteAgentConnections

from app.constants import ChatCompletionTypeEnum


class EmbeddingTable:
    """Type-safe wrapper for embedding DataFrames.

    This class ensures that DataFrames containing embeddings are not confused
    with generic DataFrames and provides a clear semantic type for embedding operations.

    The underlying DataFrame must have columns: 'id', 'text', and 'embeddings'.
    """

    def __init__(self, df: pd.DataFrame):
        """Initialize EmbeddingTable with a DataFrame.

        Args:
            df: DataFrame with columns 'id', 'text', and 'embeddings'

        Raises:
            ValueError: If required columns are missing
        """
        required_columns = {'id', 'text', 'embeddings'}
        if not required_columns.issubset(df.columns):
            raise ValueError(
                f"DataFrame must contain columns: {required_columns}. "
                f"Got: {set(df.columns)}"
            )
        self._df = df

    @property
    def dataframe(self) -> pd.DataFrame:
        """Get the underlying DataFrame."""
        return self._df

    def __len__(self) -> int:
        """Return the number of rows in the table."""
        return len(self._df)

    def is_empty(self) -> bool:
        """Check if the embedding table is empty."""
        return self._df.empty


class MCPServerConfig(TypedDict):
    """Configuration for an MCP server, persisted in storage."""
    url: str
    llm_provider: NotRequired[Optional[str]]    # "groq"|"openai"|"ollama"|"vertexai"|"vllm"
    llm_model: NotRequired[Optional[str]]        # e.g. "openai/gpt-oss-20b"
    system_prompt: NotRequired[Optional[str]]     # custom system prompt for this agent
    description: NotRequired[Optional[str]]       # agent description override


class AgentInfo(TypedDict):
    remote_agent_connections: "RemoteAgentConnections"
    context_storage: Dict[str, str]
    card: AgentCard
    url: str
    arguments_schema: NotRequired[Optional[Dict[str, Any]]]
    agent_type: NotRequired[str]  # "a2a" (default) or "mcp"
    mcp_adapter: NotRequired[Any]  # MCPAgentAdapter instance (only for agent_type="mcp")
    is_default: NotRequired[bool]  # True for stdio MCP servers defined in source code
    enabled: NotRequired[bool]  # False to exclude from system prompt tools (default: True)
    profile: NotRequired[str]  # Owning profile name, "__shared__" for stdio MCP servers
    connection_error: NotRequired[Optional[str]]  # Error message when connection failed at startup
    is_stub: NotRequired[bool]  # True when registered via register_stub (not connected)
    config_name: NotRequired[Optional[str]]  # Links to tool config (e.g., tool_name for built-in, skill name for skills)
    skill_info: NotRequired[Any]  # SkillInfo instance (only for agent_type='skill')


# ---------------------------------------------------------------------------
# Tool result file types — canonical format for MCP tools returning files.
#
# Any built-in MCP tool that needs to surface a file to the frontend should
# return a ``ToolResultWithFiles`` dict as its ``structured_content``.
# The MCPAgentAdapter recognises this shape and converts each entry in
# ``_files`` into an A2A ``FilePart(file=FileWithUri(…))``.
#
# Single-file shorthand: a tool may set ``_files`` to a list with one item.
# ---------------------------------------------------------------------------

class ToolResultFile(TypedDict):
    """One file returned by an MCP tool.

    Fields mirror ``a2a.types.FileWithUri`` so the adapter can map 1-to-1:
      - ``uri``       (required) — absolute filesystem path to the file,
                       e.g. ``/home/user/.openpa/report.pdf``.
      - ``name``      (optional) — human-readable filename.
      - ``mime_type``  (optional) — MIME type, e.g. ``image/png``.

    Extra metadata fields are allowed and will be ignored by the adapter.
    """
    uri: str
    name: NotRequired[Optional[str]]
    mime_type: NotRequired[Optional[str]]


class ToolResultWithFiles(TypedDict):
    """Canonical structured_content format for MCP tools that return files.

    Keys:
      - ``text``   — observation text for the LLM.  Can be the full readable
                     content or a short placeholder such as
                     ``[Binary file: photo.png (image/png)]``.
      - ``_files`` — one or more ``ToolResultFile`` dicts to be converted into
                     ``FilePart`` objects by the adapter.

    Example::

        ToolResult(structured_content=ToolResultWithFiles(
            text="Here is the requested image.",
            _files=[
                ToolResultFile(uri="/home/user/.openpa/photo.png",
                               name="photo.png",
                               mime_type="image/png"),
            ],
        ))
    """
    text: str
    _files: list[ToolResultFile]


class ChatCompletionStreamResponseType(TypedDict):
    type: ChatCompletionTypeEnum
    data: Required[Optional[Any]]
    last_token: NotRequired[bool]
    input_tokens: NotRequired[Optional[int]]
    output_tokens: NotRequired[Optional[int]]
    finish_reason: NotRequired[Optional[str]]


class FunctionCallingResponseType(TypedDict):
    name: str
    index: int
    id: str
    arguments: str


class VectorEmbeddingType(Enum):
    OPENAI = "OPENAI"
    GRPC = "GRPC"


class ReasoningStreamResponseType(TypedDict):
    type: ChatCompletionTypeEnum
    data: Required[Any]
    input_tokens: NotRequired[Optional[int]]
    output_tokens: NotRequired[Optional[int]]

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class IntrinsicToolBehavior(Enum):
    """Declares what the reasoning agent should do after this tool executes."""
    CLARIFY = "clarify"      # Save context, reset step count, yield CLARIFY (conversation continues)
    TERMINATE = "terminate"   # Clear context, yield DONE (conversation ends)
    CONTINUE = "continue"     # Output result text, then continue the reasoning loop


@dataclass
class IntrinsicToolSkill:
    """Lightweight skill descriptor, parallel to AgentSkill from a2a-sdk."""
    id: str
    name: str
    description: str
    examples: List[str] = field(default_factory=list)


@dataclass
class ToolResult:
    """Result returned by an intrinsic tool's run() method.

    Mirrors the concept of fastmcp's ToolResult but kept lightweight
    for intrinsic tools that don't need MCP content types.
    """
    content: str
    error: Optional[str] = None


class IntrinsicTool(ABC):
    """Base class for intrinsic tools.

    Intrinsic tools are internal tools that are always enabled by default.
    Unlike Remote Agents or MCP servers, they have no independent LLM and
    simply take Action_Input and forward it as a response.

    They are NOT listed on the dashboard or exposed via API.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Tool name as shown in the Action enum."""
        ...

    @property
    @abstractmethod
    def description(self) -> str:
        """Tool description for the system prompt."""
        ...

    @property
    @abstractmethod
    def behavior(self) -> IntrinsicToolBehavior:
        """Post-execution behavior."""
        ...

    @property
    def skills(self) -> List[IntrinsicToolSkill]:
        """Optional skills for richer prompt formatting. Defaults to empty."""
        return []

    async def run(self, arguments: Dict[str, Any]) -> ToolResult:
        """Execute the tool with the given arguments and return a ToolResult.

        Arguments:
            arguments: Dict containing at minimum {"action_input": str}.
                       Future intrinsic tools may accept additional keys.

        Returns:
            ToolResult with the content to be sent back to the reasoning agent.

        Default implementation is a simple pass-through of action_input.
        Subclasses should override this for custom logic.
        """
        action_input = arguments.get("action_input", "")
        return ToolResult(content=action_input)

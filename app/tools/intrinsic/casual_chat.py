from typing import Any, Dict

from app.tools.intrinsic.base import IntrinsicTool, IntrinsicToolBehavior, ToolResult


class CasualChatTool(IntrinsicTool):
    """A casual chat tool that engages in general conversation.

    When invoked, the conversation continues — context is preserved
    and the step count is reset so reasoning can resume on subsequent turns.
    """

    @property
    def name(self) -> str:
        return "Casual Chat Tool"

    @property
    def description(self) -> str:
        return (
            "A casual chat tool that can engage in general conversation and "
            "provide information on various topics. Make sure to provide the "
            "content to be sent to user in the Action_Input field when using this tool."
        )

    @property
    def behavior(self) -> IntrinsicToolBehavior:
        return IntrinsicToolBehavior.CLARIFY

    async def run(self, arguments: Dict[str, Any]) -> ToolResult:
        action_input = arguments.get("action_input", "")
        return ToolResult(content=action_input)

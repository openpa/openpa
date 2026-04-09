from typing import Any, Dict

from app.tools.intrinsic.base import IntrinsicTool, IntrinsicToolBehavior, ToolResult


class UserNotificationTool(IntrinsicTool):
    """A tool for sending notifications to the user.
    """

    TOOL_NAME = "User Notification Tool"

    @property
    def name(self) -> str:
        return self.TOOL_NAME

    @property
    def description(self) -> str:
        return (
            "This tool allows sending a message to the user. It must not be called "
            "automatically and should only be triggered upon user request. "
            "It must not be used in the first round of the reasoning process. "
            "If this tool was called previously, it must not be called again. "
        )

    @property
    def behavior(self) -> IntrinsicToolBehavior:
        return IntrinsicToolBehavior.CONTINUE

    async def run(self, arguments: Dict[str, Any]) -> ToolResult:
        action_input = arguments.get("action_input", "")
        return ToolResult(content=action_input)

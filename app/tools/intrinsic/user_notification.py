"""User-notification intrinsic tool (CONTINUE behavior)."""

from app.tools.base import ToolBehavior
from app.tools.intrinsic.base import IntrinsicTool


class UserNotificationTool(IntrinsicTool):
    """Send a message to the user mid-flow; reasoning continues afterwards."""

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
    def behavior(self) -> ToolBehavior:
        return ToolBehavior.CONTINUE

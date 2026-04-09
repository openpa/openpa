from typing import List

from app.tools.intrinsic.base import IntrinsicTool, IntrinsicToolBehavior, IntrinsicToolSkill, ToolResult
from app.tools.intrinsic.casual_chat import CasualChatTool
from app.tools.intrinsic.final_answer import FinalAnswerTool
from app.tools.intrinsic.user_notification import UserNotificationTool

__all__ = [
    "IntrinsicTool",
    "IntrinsicToolBehavior",
    "IntrinsicToolSkill",
    "CasualChatTool",
    "FinalAnswerTool",
    "UserNotificationTool",
    "ToolResult",
    "get_intrinsic_tools",
]


def get_intrinsic_tools() -> List[IntrinsicTool]:
    """Return all registered intrinsic tools. Order matters for prompt formatting."""
    return [
        CasualChatTool(),
        FinalAnswerTool(),
        UserNotificationTool(),
    ]

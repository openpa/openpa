from typing import Any, Dict

from app.tools.intrinsic.base import IntrinsicTool, IntrinsicToolBehavior, ToolResult


class FinalAnswerTool(IntrinsicTool):
    """A tool for providing the final answer once all information is collected.

    When invoked, the context is cleared and the conversation ends.
    The reasoning agent treats this as the signal that the LLM has
    gathered enough information and reached a final conclusion.
    """

    @property
    def name(self) -> str:
        return "Final Answer Tool"

    @property
    def description(self) -> str:
        return (
            "A tool that is used to provide the final answer to the user when all "
            "necessary information has been collected and a conclusion has been reached. "
            "Remember only use Final Answer Tool when you've collected all the information "
            "and reached a conclusion; do not prematurely end the reasoning process with "
            "Final Answer when the information is still incomplete. When user says Bye "
            "(goodbye, see you later, etc.) you must provide a Final Answer to end the "
            "conversation. When you decide to use Final Answer Tool, make sure to provide "
            "the final answer in the Action_Input field as the answer to user."
        )

    @property
    def behavior(self) -> IntrinsicToolBehavior:
        return IntrinsicToolBehavior.TERMINATE

    async def run(self, arguments: Dict[str, Any]) -> ToolResult:
        action_input = arguments.get("action_input", "")
        return ToolResult(content=action_input)

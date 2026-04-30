"""Change Working Directory built-in tool.

Switches ``_working_directory`` for the current conversation only. The override
is held in :class:`app.utils.context_storage.ContextStorage` keyed by the
calling ``context_id`` and read back by :mod:`app.tools.builtin.adapter` (and
the reasoning agent's prompt) on every subsequent step.

Allowed targets:

- ``user_working`` -- the profile-global default (``get_user_working_directory()``).
  Selecting this clears any prior override so the conversation tracks the live
  default again.
- ``skills``       -- ``<OPENPA_WORKING_DIR>/<profile>/skills``.
- ``documents``    -- ``<OPENPA_WORKING_DIR>/<profile>/documents``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from app.config.settings import BaseConfig, get_user_working_directory
from app.skills.sync import profile_skills_dir
from app.tools.builtin.base import BuiltInTool, BuiltInToolResult
from app.types import ToolConfig
from app.utils.context_storage import clear_context, get_context, set_context

OVERRIDE_KEY = "_working_directory_override"

SERVER_NAME = "Change Working Directory"

_TARGETS = ("user_working", "skills", "documents")


def _resolve_target(target: str, profile: str) -> Path:
    if target == "skills":
        return profile_skills_dir(profile)
    if target == "documents":
        return Path(BaseConfig.OPENPA_WORKING_DIR) / profile / "documents"
    return Path(get_user_working_directory())


TOOL_CONFIG: ToolConfig = {
    "name": "change_working_directory",
    "display_name": "Change Working Directory",
    "default_model_group": "low",
    "llm_parameters": {
        "tool_instructions": (
            "Always switch the active working directory before executing any "
            "commands relevant to the user's files or skills. This ensures "
            "the agent is operating in the correct context.\n"
            "Supported targets are 'user working' (the profile default), "
            "'skills', and 'documents'.\n"
            "E.g. 'change to skills directory'"
        ),
        "system_prompt": (
            "Don't answer any questions or provide any information. "
            "Always call the 'change_working_directory' tool with the requested target."
        ),
    },
}


class ChangeWorkingDirectoryTool(BuiltInTool):
    name: str = "change_working_directory"
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "target": {
                "type": "string",
                "enum": list(_TARGETS),
                "description": (
                    "Which directory to switch to: 'user_working' (profile "
                    "default; clears any override), 'skills', or 'documents'."
                ),
            }
        },
        "required": ["target"],
        "additionalProperties": False,
    }

    async def run(self, arguments: Dict[str, Any]) -> BuiltInToolResult:
        target = arguments.get("target")
        if target not in _TARGETS:
            return BuiltInToolResult(
                content=[{"type": "text", "text": (
                    f"target must be one of {list(_TARGETS)}"
                )}]
            )

        context_id = arguments.get("_context_id") or ""
        if not context_id:
            return BuiltInToolResult(
                content=[{"type": "text", "text": (
                    "Cannot change working directory: missing conversation context."
                )}]
            )

        profile = arguments.get("_profile") or "default"

        previous = (
            get_context(context_id, OVERRIDE_KEY)
            or get_user_working_directory()
        )
        new_path = _resolve_target(target, profile)

        if target == "user_working":
            clear_context(context_id, OVERRIDE_KEY)
        else:
            new_path.mkdir(parents=True, exist_ok=True)
            set_context(context_id, OVERRIDE_KEY, str(new_path))

        new_str = str(new_path)
        return BuiltInToolResult(
            content=[{
                "type": "text",
                "text": f"Working directory switched to {new_str}",
            }],
            structured_content={
                "target": target,
                "previous": str(previous),
                "current": new_str,
            },
        )


def get_tools(config: dict) -> list[BuiltInTool]:
    """Return tool instances for this server."""
    return [ChangeWorkingDirectoryTool()]

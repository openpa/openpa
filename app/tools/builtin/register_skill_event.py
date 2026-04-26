"""Register Skill Event built-in tool.

Records a (conversation, skill, event_type, action) tuple so that whenever a
new ``*.md`` file appears in ``<skill_dir>/events/<event_type>/`` (produced by
the skill's own listener daemon), the reasoning agent re-runs ``action`` with
the file content appended — and streams the result into the conversation.

The LLM is steered toward this tool by a hint block injected into the loaded
skill's instruction text whenever a skill declares ``metadata.events``. The
tool itself only appears in the action enum once at least one such skill is
loaded in the current run (filtered in :mod:`app.agent.reasoning_agent`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from app.events.manager import get_event_manager
from app.storage import get_event_subscription_storage
from app.tools.builtin.base import BuiltInTool, BuiltInToolResult
from app.tools.builtin.exec_shell import _lookup_skill_source
from app.tools.ids import slugify
from app.types import ToolConfig
from app.utils.logger import logger


SERVER_NAME = "Register Skill Event"


def _resolve_skill(skill_name: str, profile: str) -> Optional[tuple[str, str]]:
    """Resolve a user-supplied skill name to ``(tool_id, source_dir)``.

    Skill rows are keyed by ``<profile>__<slug>`` (see
    :mod:`app.tools.registry`). The hint we inject into the system prompt
    asks the LLM to pass that tool_id form directly, but for resilience we
    also accept a bare slug or the original SKILL.md ``name`` value
    (e.g. ``email-cli``); a leading ``<profile>__`` is stripped before
    re-slugging so a stale prefix on a different profile still resolves.
    """
    raw = (skill_name or "").strip()
    if not raw or not profile:
        return None
    prefix = f"{profile}__"
    bare = raw[len(prefix):] if raw.startswith(prefix) else raw
    candidates = [
        f"{profile}__{slugify(bare)}",
        slugify(bare),
        raw,
    ]
    seen: set[str] = set()
    for cand in candidates:
        if not cand or cand in seen:
            continue
        seen.add(cand)
        source = _lookup_skill_source(cand, profile)
        if source:
            return cand, source
    return None


def _resolve_skill_source(skill_name: str, profile: str) -> Optional[str]:
    """Convenience: source dir only (used by callers that don't need the id)."""
    resolved = _resolve_skill(skill_name, profile)
    return resolved[1] if resolved else None

TOOL_CONFIG: ToolConfig = {
    "name": "register_skill_event",
    "display_name": "Register Skill Event",
    "default_model_group": "low",
    "llm_parameters": {
        "tool_instructions": (
            "Subscribe the current conversation to a skill's filesystem event so a "
            "saved instruction is run automatically whenever the event fires.\n\n"
            "Call this tool with three string arguments:\n"
            "- skill_name: the name of an already-loaded skill that declares "
            "metadata.events (e.g. 'email-cli').\n"
            "- trigger: one of that skill's declared event names (e.g. "
            "'new_email'). The 'when' is fully captured by the trigger.\n"
            "- action: ONLY what to do — a short imperative instruction such "
            "as 'Summarize the email content'. Do NOT repeat the trigger "
            "condition (no 'when a new email arrives', 'on new email', "
            "'whenever ...', etc.). The event file's content will be "
            "appended to the action automatically."
        ),
        "system_prompt": (
            "You convert a registration request into a structured tool call. "
            "Always call register_skill_event with skill_name, trigger, and "
            "action. The action must contain only the work to perform; never "
            "restate the trigger condition. Do not produce any other output."
        ),
    },
}


def _read_events_metadata(source_dir: Path) -> List[Dict[str, Any]]:
    """Return the list under ``metadata.events.event_type`` from SKILL.md."""
    skill_md = source_dir / "SKILL.md"
    if not skill_md.exists():
        return []
    try:
        text = skill_md.read_text(encoding="utf-8")
    except OSError:
        return []
    stripped = text.lstrip()
    if not stripped.startswith("---"):
        return []
    end_idx = stripped.find("---", 3)
    if end_idx == -1:
        return []
    try:
        data = yaml.safe_load(stripped[3:end_idx]) or {}
    except yaml.YAMLError:
        return []
    if not isinstance(data, dict):
        return []
    metadata = data.get("metadata") or {}
    if not isinstance(metadata, dict):
        return []
    events = metadata.get("events") or {}
    if not isinstance(events, dict):
        return []
    items = events.get("event_type") or []
    if not isinstance(items, list):
        return []
    cleaned: List[Dict[str, Any]] = []
    for item in items:
        if isinstance(item, dict) and item.get("name"):
            cleaned.append(item)
    return cleaned


class RegisterSkillEventTool(BuiltInTool):
    name: str = "register_skill_event"
    description: str = (
        "Subscribe this conversation to a skill's filesystem event. The "
        "skill must already be loaded and must declare metadata.events in "
        "its SKILL.md."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "skill_name": {
                "type": "string",
                "description": (
                    "Tool-id of an already-loaded skill that declares "
                    "metadata.events. Use the exact identifier the parent "
                    "agent passes in (typically the profile-prefixed form, "
                    "e.g. 'admin__email_cli'). Do NOT trim or rewrite the "
                    "prefix."
                ),
            },
            "trigger": {
                "type": "string",
                "description": (
                    "One of the event names declared by the skill (e.g., "
                    "'new_email')."
                ),
            },
            "action": {
                "type": "string",
                "description": (
                    "Imperative instruction describing ONLY what to do — e.g. "
                    "'Summarize the email content'. Do not include the "
                    "trigger condition (no 'when a new email arrives', 'on "
                    "new email', 'whenever ...'); the trigger field already "
                    "captures that. The event file's content is appended "
                    "automatically when the action runs."
                ),
            },
        },
        "required": ["skill_name", "trigger", "action"],
        "additionalProperties": False,
    }

    async def run(self, arguments: Dict[str, Any]) -> BuiltInToolResult:
        profile: Optional[str] = arguments.get("_profile")
        context_id: Optional[str] = arguments.get("_context_id")
        skill_name: str = (arguments.get("skill_name") or "").strip()
        trigger: str = (arguments.get("trigger") or "").strip()
        action: str = (arguments.get("action") or "").strip()

        if not profile:
            return _err("Internal error: profile not provided to register_skill_event.")
        if not context_id:
            return _err("Internal error: context_id not provided to register_skill_event.")
        if not skill_name:
            return _err("skill_name is required.")
        if not trigger:
            return _err("trigger is required.")
        if not action:
            return _err("action is required.")

        resolved = _resolve_skill(skill_name, profile)
        if resolved is None:
            return _err(
                f"Skill '{skill_name}' was not found for profile '{profile}'. "
                f"Make sure the skill is installed and enabled."
            )
        canonical_skill_id, source_dir_str = resolved
        source_dir = Path(source_dir_str)

        events = _read_events_metadata(source_dir)
        valid_names = [e["name"] for e in events]
        if not valid_names:
            return _err(
                f"Skill '{skill_name}' does not declare any events in its "
                f"metadata.events. Cannot register a trigger."
            )
        if trigger not in valid_names:
            return _err(
                f"trigger '{trigger}' is not declared by skill '{skill_name}'. "
                f"Valid triggers: {', '.join(valid_names)}."
            )

        # Resolve (or create) the conversation row. On the very first user
        # turn the executor has not yet persisted a conversation row — that
        # only happens after the reasoning loop finishes (see
        # ``app/agent/executor.py``). Creating it eagerly here gives the
        # subscription a valid FK target without waiting for another turn,
        # and the executor's later ``get_or_create_conversation`` call is a
        # no-op because we now share the same ``context_id``.
        from app.storage import get_conversation_storage

        conv_storage = get_conversation_storage()
        try:
            conv = await conv_storage.get_or_create_conversation(
                profile=profile, context_id=context_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("register_skill_event: get_or_create_conversation failed")
            return _err(f"Could not resolve the active conversation: {exc}")
        if conv is None:
            return _err("Could not resolve the active conversation.")
        conversation_id = conv["id"]

        # Persist + watch using the canonical tool_id so every entry agrees
        # regardless of the surface form the LLM happened to pass.
        store = get_event_subscription_storage()
        try:
            row = store.upsert(
                conversation_id=conversation_id,
                profile=profile,
                skill_name=canonical_skill_id,
                event_type=trigger,
                action=action,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("register_skill_event: upsert failed")
            return _err(f"Failed to save subscription: {exc}")

        try:
            get_event_manager().ensure_watcher(
                profile=profile,
                skill_name=canonical_skill_id,
                source_dir=source_dir_str,
                event_type=trigger,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("register_skill_event: ensure_watcher failed")
            return _err(
                f"Subscription saved but the watcher could not start: {exc}. "
                f"Check server logs."
            )

        confirmation = (
            f"Subscribed this conversation to the '{trigger}' event of skill "
            f"'{canonical_skill_id}'. Whenever a new event arrives in "
            f"{source_dir / 'events' / trigger}, I'll run: "
            f"{action.strip()}."
        )
        logger.info(
            f"register_skill_event: id={row['id']} conv={conversation_id} "
            f"skill={canonical_skill_id} trigger={trigger}"
        )
        return BuiltInToolResult(
            content=[{"type": "text", "text": confirmation}]
        )


def _err(message: str) -> BuiltInToolResult:
    return BuiltInToolResult(content=[{"type": "text", "text": message}])


def get_tools(config: dict) -> list[BuiltInTool]:
    return [RegisterSkillEventTool()]

"""Run the reasoning agent for one skill event with no chat history.

This is a thin adapter over :func:`app.agent.stream_runner.run_agent_to_bus`.
The skill-event path used to maintain its own copy of the agent loop and
persistence logic; that work has been folded into ``stream_runner`` so the
user-typed-message path and the skill-event path share one implementation
and one streaming protocol (SSE off the conversation stream bus).

The runner is set up by the server at boot via :func:`set_globals` so the
event manager and queue can call :func:`run_event` without holding direct
references to the agent and storage objects.
"""

from __future__ import annotations

from typing import Any

from app.utils.logger import logger


_openpa_agent: Any = None
_conversation_storage: Any = None


def set_globals(*, openpa_agent: Any, conversation_storage: Any) -> None:
    """Wire the runner to its collaborators (called once at server boot)."""
    global _openpa_agent, _conversation_storage
    _openpa_agent = openpa_agent
    _conversation_storage = conversation_storage


def get_openpa_agent() -> Any:
    return _openpa_agent


def get_conversation_storage() -> Any:
    return _conversation_storage


async def run_event(
    *,
    conversation_id: str,
    profile: str,
    skill_name: str,
    event_type: str,
    action: str,
    file_content: str,
) -> None:
    """Persist a synthetic user message, run the reasoning agent, persist the reply.

    Delegates to :func:`stream_runner.run_agent_to_bus`, which handles
    publishing every chunk to the conversation stream bus, persisting the
    assistant message, and emitting the terminal ``complete`` event.
    """
    if _openpa_agent is None or _conversation_storage is None:
        logger.error("Event runner globals not initialized; dropping event")
        return

    # Lazy import to break the circular dependency cycle:
    # events.__init__ → manager → queue → runner → stream_runner →
    # events.notifications_buffer → events.__init__.
    from app.agent.stream_runner import make_run_id, run_agent_to_bus

    query = f"{action.strip()}\n\n{file_content.strip()}"
    metadata = {
        "source": "skill_event",
        "skill_name": skill_name,
        "event_type": event_type,
    }

    await run_agent_to_bus(
        openpa_agent=_openpa_agent,
        conversation_storage=_conversation_storage,
        conversation_id=conversation_id,
        run_id=make_run_id(conversation_id, kind="event"),
        profile=profile,
        query=query,
        history_messages=[],
        reasoning=True,
        user_message_metadata=metadata,
        agent_message_metadata=metadata,
        push_user_message=True,
        publish_notification=True,
        # Skill events run on existing conversations whose titles are
        # already meaningful; never rewrite them from the synthetic query.
        update_title_from_query=False,
    )

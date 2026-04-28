"""Unified streaming runner for agent runs.

This module owns a single agent-to-bus pipeline used by *both* user-typed
messages (via ``POST /api/conversations/{id}/messages``) and skill-event
triggered runs (via :mod:`app.events.runner`). The previous codebase had two
near-identical implementations -- one in :class:`OpenPAAgentExecutor` and one
in :func:`app.events.runner.run_event` -- which diverged in subtle ways and
forced clients to choose between two streaming protocols (A2A SDK over a
client-owned HTTP request vs. SSE subscription with replay). Consolidating
here lets the SSE path serve both: a browser client POSTs a message, the run
executes in a background asyncio task, and any number of SSE subscribers
(across tabs, navigations, reconnects) receive the same chunks.

Responsibilities:

* Run :meth:`OpenPAAgent.run` for a conversation.
* Publish each chunk to the :class:`ConversationStreamBus` using the
  vocabulary already understood by the frontend (``text``, ``thinking``,
  ``result``, ``file``, ``terminal``, ``token_usage``, ``phase``,
  ``summary``, ``complete``, ``error``, ``user_message``).
* Persist the user message (when applicable) and the final assistant message
  to SQLite so a reload reproduces what was rendered.
* Register the asyncio task in a single registry keyed by ``run_id`` so a
  uniform cancel API can target it regardless of how the run was launched.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any, Dict, List, Optional

from a2a.types import DataPart, FilePart, Part

from app.agent.summary import summarize_reasoning
from app.constants import ChatCompletionTypeEnum
from app.events.notifications_buffer import get_event_notifications
from app.events.stream_bus import get_event_stream_bus
from app.utils.logger import logger
from app.utils.task_context import current_task_id_var


# ── unified cancellation registry ───────────────────────────────────────────

# Maps run_id -> the asyncio.Task running the agent loop. Populated when a
# run starts; cleared when it ends. Both the A2A executor and the user-message
# POST handler register here so a single cancel endpoint targets either.
_running_runs: Dict[str, asyncio.Task] = {}


def cancel_run(run_id: str) -> bool:
    """Cancel the running asyncio task for ``run_id``. Idempotent."""
    task = _running_runs.get(run_id)
    if task is None or task.done():
        return False
    task.cancel()
    return True


def is_running(run_id: str) -> bool:
    task = _running_runs.get(run_id)
    return bool(task and not task.done())


# ── helpers (lifted from runner.py) ─────────────────────────────────────────


def _trim(text: str, n: int = 240) -> str:
    text = text.strip()
    if len(text) <= n:
        return text
    return text[: n - 1].rstrip() + "…"


def _serialize_observation(observation_parts: List[Any]) -> List[Dict[str, Any]]:
    """Serialize Part objects to dicts for the frontend DataPart payload."""
    serialized: List[Dict[str, Any]] = []
    for obs_part in observation_parts:
        if hasattr(obs_part, "root") and hasattr(obs_part.root, "model_dump"):
            serialized.append(obs_part.root.model_dump(mode="json"))
        elif hasattr(obs_part, "model_dump"):
            serialized.append(obs_part.model_dump(mode="json"))
        elif isinstance(obs_part, dict):
            serialized.append(obs_part)
    return serialized


def _terminal_payloads(observation_parts: List[Any]) -> List[Dict[str, Any]]:
    """Extract one terminal payload per long-running subprocess in an observation."""
    seen: set[str] = set()
    out: List[Dict[str, Any]] = []
    for obs_part in observation_parts:
        root = getattr(obs_part, "root", obs_part)
        if not isinstance(root, DataPart):
            continue
        data = root.data or {}
        candidates: list[dict] = []
        if isinstance(data, dict):
            for value in data.values():
                if isinstance(value, dict):
                    candidates.append(value)
            candidates.append(data)
        for payload in candidates:
            pid = payload.get("process_id")
            if (
                payload.get("category") == "long_running"
                and isinstance(pid, str)
                and pid not in seen
            ):
                seen.add(pid)
                cmd = str(payload.get("command", "") or "")
                short = cmd if len(cmd) <= 36 else cmd[:36].rstrip() + " …"
                out.append({
                    "process_id": pid,
                    "command": cmd,
                    "command_short": short,
                    "working_directory": payload.get("working_directory", ""),
                    "pty": bool(payload.get("pty", False)),
                })
    return out


# ── unified runner ──────────────────────────────────────────────────────────


async def run_agent_to_bus(
    *,
    openpa_agent: Any,
    conversation_storage: Any,
    conversation_id: str,
    run_id: str,
    profile: str,
    query: str,
    history_messages: List[Any],
    reasoning: bool = True,
    user_parts: List[Any] | None = None,
    user_message_metadata: Dict[str, Any] | None = None,
    agent_message_metadata: Dict[str, Any] | None = None,
    push_user_message: bool = True,
    publish_notification: bool = False,
    update_title_from_query: bool = True,
) -> None:
    """Run the reasoning agent for one conversation, publishing chunks to the bus.

    On entry, marks the run active on the bus so any SSE subscriber receives
    each chunk live. On exit (success, cancel, or failure), publishes a
    terminal ``complete`` (or ``error``) event, persists the assistant message
    to SQLite, and clears the bus's ring buffer so a fresh subscriber doesn't
    re-render persisted history.

    Idempotent re: bus state: both ``start_run`` and ``end_run`` are safe to
    call when no subscriber is connected.
    """
    bus = get_event_stream_bus()

    # Mark the run active on the bus before we publish anything. Late
    # subscribers (e.g. a tab opened after the user typed) get the replay.
    await bus.start_run(conversation_id)

    # Tag spawned subprocesses with this run's id so cancellation can target
    # them precisely (mirrors the legacy executor's ContextVar usage).
    ctx_token = current_task_id_var.set(run_id)
    _running_runs[run_id] = asyncio.current_task()

    # Capture conversation context up front. We allow get_conversation to
    # fail (e.g. transient DB hiccup) without aborting the run -- the agent
    # loop only really needs ``context_id`` for tool storage scoping, and
    # falling back to ``conversation_id`` is harmless.
    conv: Optional[dict] = None
    try:
        conv = await conversation_storage.get_conversation(conversation_id)
    except Exception:  # noqa: BLE001
        logger.exception(
            f"stream_runner: failed to load conversation {conversation_id}"
        )
    context_id = (conv or {}).get("context_id") or conversation_id
    title = (conv or {}).get("title") or "Untitled Chat"

    # Back-fill context_id on the conv row when it was created via the web
    # POST /api/conversations path (which leaves context_id=NULL). Without
    # this, tools like register_skill_event do get_conversation_by_context
    # and miss → they spawn a stray "Untitled Chat" sibling instead of
    # attaching to the active conversation.
    if conv and not conv.get("context_id"):
        try:
            await conversation_storage.update_conversation(
                conversation_id, context_id=context_id,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                f"stream_runner: failed to back-fill context_id for {conversation_id}"
            )

    # Skill-event runs have no client-side POST, so the frontend never sets
    # the 'streaming' tracker that opens the per-conversation SSE. Push a
    # started notification on the global notifications stream so the sidebar
    # can lazily open that SSE and light up the streaming-dot.
    if publish_notification:
        try:
            logger.info(
                f"[debug:started] pushing started notification "
                f"profile={profile} conversation_id={conversation_id} title={title!r}"
            )
            entry = get_event_notifications().push(
                profile=profile,
                conversation_id=conversation_id,
                conversation_title=title,
                message_preview="",
                kind="started",
            )
            logger.info(f"[debug:started] pushed entry id={entry.get('id')} kind={entry.get('kind')}")
        except Exception:  # noqa: BLE001
            logger.exception("stream_runner: failed to push started notification")

    final_text_parts: List[str] = []
    collected_thinking_steps: List[dict] = []
    collected_file_parts: List[Part] = []
    total_input_tokens = 0
    total_output_tokens = 0
    reasoning_input_section: str | None = None
    errored = False
    cancelled = False

    try:
        # 1. Persist the user message and announce it on the bus.
        if push_user_message:
            user_msg_id: Optional[str] = None
            try:
                user_msg = await conversation_storage.add_message(
                    conversation_id=conversation_id,
                    role="user",
                    content=query,
                    parts=user_parts,
                    metadata=user_message_metadata,
                )
                user_msg_id = user_msg.get("id") if isinstance(user_msg, dict) else None
            except Exception:  # noqa: BLE001
                logger.exception(
                    f"stream_runner: failed to persist user message for {conversation_id}"
                )

            await bus.publish(conversation_id, "user_message", {
                "id": user_msg_id,
                "content": query,
                "metadata": user_message_metadata or {},
            })

        # 2. Stream the agent loop, mirroring chunks to the bus and collecting
        #    enough state to persist the final assistant message verbatim.
        try:
            async for chunk in openpa_agent.run(
                query=query,
                task_history=history_messages,
                context_id=context_id,
                profile=profile,
                reasoning=reasoning,
            ):
                ctype = chunk.get("type")

                if ctype == ChatCompletionTypeEnum.CONTENT:
                    data = chunk.get("data")
                    if data:
                        final_text_parts.append(data)
                        await bus.publish(conversation_id, "text", {"token": data})

                elif ctype == ChatCompletionTypeEnum.THINKING_ARTIFACT:
                    thinking_data = chunk.get("data", {}) or {}
                    await bus.publish(conversation_id, "thinking", thinking_data)
                    collected_thinking_steps.append({
                        "thought": thinking_data.get("Thought", ""),
                        "action": thinking_data.get("Action", ""),
                        "action_input": thinking_data.get("Action_Input", ""),
                        "model_label": thinking_data.get("Model_Label"),
                        "reasoning_model_label": thinking_data.get("Reasoning_Model_Label"),
                    })

                elif ctype == ChatCompletionTypeEnum.RESULT_ARTIFACT:
                    result_data = chunk.get("data", {}) or {}
                    observation_parts = result_data.get("Observation", []) or []
                    serialized_observation = _serialize_observation(observation_parts)

                    await bus.publish(conversation_id, "result", {
                        "Observation": serialized_observation,
                    })

                    for obs_part in observation_parts:
                        if hasattr(obs_part, "root") and isinstance(obs_part.root, FilePart):
                            collected_file_parts.append(obs_part)
                            file_payload = obs_part.root.model_dump(mode="json")
                            await bus.publish(conversation_id, "file", file_payload)

                    for terminal in _terminal_payloads(observation_parts):
                        await bus.publish(conversation_id, "terminal", terminal)

                    if collected_thinking_steps:
                        for step in reversed(collected_thinking_steps):
                            if "observation" not in step:
                                step["observation"] = serialized_observation
                                break

                elif ctype in (
                    ChatCompletionTypeEnum.DONE,
                    ChatCompletionTypeEnum.CLARIFY,
                ):
                    data = chunk.get("data")
                    if data:
                        final_text_parts.append(data)
                        await bus.publish(conversation_id, "text", {"token": data})
                    total_input_tokens = chunk.get("input_tokens") or total_input_tokens
                    total_output_tokens = chunk.get("output_tokens") or total_output_tokens
                    if (
                        ctype == ChatCompletionTypeEnum.DONE
                        and chunk.get("input_section")
                    ):
                        reasoning_input_section = chunk["input_section"]
        except asyncio.CancelledError:
            cancelled = True
            logger.info(f"stream_runner: run {run_id} cancelled")
            try:
                from app.tools.builtin.exec_shell import cancel_processes_by_task
                killed = await cancel_processes_by_task(run_id)
                if killed:
                    logger.info(
                        f"stream_runner: killed {killed} subprocess(es) for cancelled run {run_id}"
                    )
            except Exception:  # noqa: BLE001
                logger.exception("stream_runner: subprocess cancellation failed")
            try:
                await bus.publish(conversation_id, "error", {
                    "message": "Stopped by user.",
                    "cancelled": True,
                })
            except Exception:  # noqa: BLE001
                logger.exception("stream_runner: failed to publish cancel event")
        except Exception:  # noqa: BLE001
            errored = True
            logger.exception(f"stream_runner: agent run failed for {conversation_id}")
            final_text_parts.append("(agent run failed; see server logs)")
            try:
                await bus.publish(conversation_id, "error", {
                    "message": "agent run failed; see server logs",
                })
            except Exception:  # noqa: BLE001
                logger.exception("stream_runner: failed to publish error event")

        # 3. Optional reasoning summary (skip on cancel/error to keep the UI
        #    state consistent with the partial response).
        summary_text: str | None = None
        if reasoning_input_section and not errored and not cancelled:
            try:
                await bus.publish(conversation_id, "phase", {"phase": "summarizing"})
                summary_text = await summarize_reasoning(
                    openpa_agent, reasoning_input_section, profile,
                )
                if summary_text:
                    await bus.publish(conversation_id, "summary", {"summary": summary_text})
            except Exception:  # noqa: BLE001
                logger.exception("stream_runner: failed to produce reasoning summary")
                summary_text = None

        # 4. Token usage (after summary so any summary tokens are included
        #    upstream; here we only report what the agent loop reported).
        if total_input_tokens or total_output_tokens:
            await bus.publish(conversation_id, "token_usage", {
                "token_usage": {
                    "input_tokens": total_input_tokens,
                    "output_tokens": total_output_tokens,
                },
            })

        # 5. Persist the final assistant message so a reload reproduces the
        #    rendered state. Failures are logged but do not break the stream
        #    -- the user has already seen the response.
        final_text = (
            "".join(final_text_parts).strip()
            or ("(stopped)" if cancelled else "(no response)")
        )
        token_usage_data: dict | None = None
        if total_input_tokens or total_output_tokens:
            token_usage_data = {
                "input_tokens": total_input_tokens,
                "output_tokens": total_output_tokens,
            }
        persist_parts = collected_file_parts if collected_file_parts else None

        assistant_msg_id: Optional[str] = None
        try:
            assistant_msg = await conversation_storage.add_message(
                conversation_id=conversation_id,
                role="agent",
                content=final_text,
                parts=persist_parts,
                thinking_steps=collected_thinking_steps or None,
                token_usage=token_usage_data,
                summary=summary_text,
                metadata=agent_message_metadata,
            )
            assistant_msg_id = (
                assistant_msg.get("id") if isinstance(assistant_msg, dict) else None
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                f"stream_runner: failed to persist assistant message for {conversation_id}"
            )

        # 6. Update the conversation row (title from first query, task_id).
        try:
            update_kwargs: Dict[str, Any] = {"task_id": run_id}
            if (
                update_title_from_query
                and (conv or {}).get("title") == "Untitled Chat"
                and query.strip()
            ):
                short = query.strip()[:40] + ("..." if len(query.strip()) > 40 else "")
                update_kwargs["title"] = short
                title = short  # for the optional notification below
            await conversation_storage.update_conversation(
                conversation_id, **update_kwargs,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                f"stream_runner: failed to update conversation row for {conversation_id}"
            )

        # 7. Terminal event so subscribers can flip isStreaming=false.
        await bus.publish(conversation_id, "complete", {
            "assistant_id": assistant_msg_id,
            "errored": errored,
            "cancelled": cancelled,
        })

        # 8. Optional notification (only the skill-event path opts in today).
        if publish_notification:
            try:
                get_event_notifications().push(
                    profile=profile,
                    conversation_id=conversation_id,
                    conversation_title=title,
                    message_preview=_trim(final_text),
                    kind="error" if errored else "completed",
                )
            except Exception:  # noqa: BLE001
                logger.exception("stream_runner: failed to push notification")
    finally:
        _running_runs.pop(run_id, None)
        if ctx_token is not None:
            current_task_id_var.reset(ctx_token)
        await bus.end_run(conversation_id)


def make_run_id(conversation_id: str, kind: str = "msg") -> str:
    """Generate a unified run id. ``kind`` is purely informational (``msg`` or ``event``)."""
    return f"{kind}:{conversation_id}:{uuid.uuid4()}"

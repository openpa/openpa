import asyncio
import uuid
from typing import Any, Dict, List, Tuple, cast
from tiktoken import encoding_for_model

from a2a.server.agent_execution import AgentExecutor
from a2a.server.agent_execution.context import RequestContext
from a2a.server.events.event_queue import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import (
    AgentCard,
    DataPart,
    FilePart,
    TaskState,
    TextPart,
    Part,
    Message,
    Role,
    UnsupportedOperationError,
)

from a2a.utils import new_agent_text_message, new_task
from a2a.utils.errors import ServerError

from app.agent.agent import OpenPAAgent
from app.remote_agents import RoutingAgent
from app.config.settings import BaseConfig
from app.constants import ChatCompletionTypeEnum
from app.lib.llm import (LLMProvider, GroqLLMProvider)
from app.storage.conversation_storage import ConversationStorage

from app.utils import logger, build_table_embeddings, find_similar_items
from app.utils.common import convert_db_messages_to_history, convert_task_history_to_messages
from app.utils.context_storage import get_context


class OpenPAAgentExecutor(AgentExecutor):
    """An AgentExecutor that runs the OpenPAAgent."""

    def __init__(self, olli_agent: OpenPAAgent, conversation_storage: ConversationStorage | None = None):
        logger.debug("Initializing OpenPAAgentExecutor...")
        self._active_sessions: set[str] = set()
        self.olli_agent: OpenPAAgent | None = olli_agent
        self.conversation_storage = conversation_storage

    async def execute(
        self,
        context: RequestContext,
        event_queue: EventQueue,
    ):
        logger.info("Starting execution")
        # dump context for debugging
        params = getattr(context, "_params", None)
        metadata = getattr(params, "metadata", None)
        
        # Extract profile from JWT token (via call_context.state) or fallback to metadata
        profile = None
        call_context = context._call_context
        if call_context and hasattr(call_context, 'state') and call_context.state:
            profile = call_context.state.get("profile")

        # Fallback: extract from metadata (for clients not using JWT auth)
        if not profile and metadata and isinstance(metadata, dict):
            profile_data = metadata.get("profile")
            if isinstance(profile_data, dict):
                profile = profile_data.get("id")
            elif isinstance(profile_data, str):
                profile = profile_data

        if not profile:
            raise ServerError(error=UnsupportedOperationError(
                message="Profile is required. Please authenticate with a valid token or provide a profile."
            ))
        
        logger.debug(f"Profile: {profile}")
        logger.debug(f"Metadata: {metadata}")
        logger.debug(context.context_id)
        logger.debug(context.task_id)

        context_message = context.message
        if context_message and hasattr(context_message, "parts"):
            logger.debug(context_message.parts)
        else:
            logger.debug("No message parts available on context.")

        query = context.get_user_input()
        logger.debug(query)

        task = context.current_task

        context_id = context.context_id

        if not task:
            if context_message is None:
                logger.error("Request context is missing the originating message.")
                raise ServerError(error=UnsupportedOperationError())

            task = new_task(context_message)
            await event_queue.enqueue_event(task)

        updater = TaskUpdater(event_queue, task.id, task.context_id)

        if not self.olli_agent:
            # Should not happen if initialized correctly
            logger.error("OpenPAAgent not initialized")
            raise ServerError(error=UnsupportedOperationError())

        artifact_id = str(uuid.uuid4())
        has_sent_first_chunk = False
        final_response_text_parts = []
        total_input_tokens = 0
        total_output_tokens = 0
        collected_thinking_steps: list[dict] = []
        collected_file_parts: list[Part] = []

        # Initialize tiktoken encoder for token-by-token streaming
        encoder = encoding_for_model("gpt-4o")

        # Load history from DB (includes message IDs for the message_detail tool).
        # Fall back to task.history when no DB conversation exists yet.
        history_messages = []
        if self.conversation_storage and profile and context_id:
            try:
                conv = await self.conversation_storage.get_conversation_by_context(
                    profile=profile, context_id=context_id,
                )
                if conv:
                    db_msgs = await self.conversation_storage.get_messages(conv["id"])
                    if db_msgs:
                        history_messages = convert_db_messages_to_history(db_msgs, inject_ids=True)
            except Exception:
                logger.debug("Failed to load history from DB, falling back to task.history", exc_info=True)

        if not history_messages:
            history_messages = convert_task_history_to_messages(task.history or [])

        try:
            async for chunk in self.olli_agent.run(query, history_messages, context_id, profile=profile, metadata=metadata):
                # logger.debug(f"Received chunk from OpenPAAgent: {chunk}")
                if chunk["type"] == ChatCompletionTypeEnum.CONTENT:
                    content = chunk.get("data")
                    if content:
                        final_response_text_parts.append(content)

                        # Tokenize content and stream token by token
                        tokens = encoder.encode(content)
                        for token in tokens:
                            token_text = encoder.decode([token])
                            await updater.add_artifact(
                                [Part(root=TextPart(text=token_text))],
                                artifact_id,
                                name="Text Response",
                                append=has_sent_first_chunk,
                                last_chunk=False
                            )
                            has_sent_first_chunk = True
                            # Add delay to simulate token latency
                            await asyncio.sleep(0.01)  # 10ms delay per token
                elif chunk["type"] == ChatCompletionTypeEnum.THINKING_ARTIFACT:
                    # Emit thinking artifact as a separate DataPart
                    thinking_data = chunk.get("data", {})
                    await updater.add_artifact(
                        [Part(root=DataPart(data=thinking_data, kind="data", metadata=None))],
                        name="thinking",
                        append=False,
                        last_chunk=True
                    )
                    # Collect for persistence
                    collected_thinking_steps.append({
                        "thought": thinking_data.get("Thought", ""),
                        "action": thinking_data.get("Action", ""),
                        "action_input": thinking_data.get("Action_Input", ""),
                    })
                elif chunk["type"] == ChatCompletionTypeEnum.RESULT_ARTIFACT:
                    # Observation is now list[Part] from the reasoning agent
                    result_data = chunk.get("data", {})
                    observation_parts = result_data.get("Observation", [])

                    # Serialize Part objects to dicts for the frontend DataPart
                    serialized_observation = []
                    for obs_part in observation_parts:
                        if hasattr(obs_part, "root") and hasattr(obs_part.root, "model_dump"):
                            serialized_observation.append(obs_part.root.model_dump(mode="json"))
                        elif hasattr(obs_part, "model_dump"):
                            serialized_observation.append(obs_part.model_dump(mode="json"))
                        elif isinstance(obs_part, dict):
                            serialized_observation.append(obs_part)

                    await updater.add_artifact(
                        [Part(root=DataPart(data={"Observation": serialized_observation}, kind="data", metadata=None))],
                        name="result",
                        append=False,
                        last_chunk=True
                    )

                    # Emit each FilePart as a separate "file" artifact for the frontend
                    for obs_part in observation_parts:
                        if hasattr(obs_part, "root") and isinstance(obs_part.root, FilePart):
                            collected_file_parts.append(obs_part)
                            await updater.add_artifact(
                                [obs_part],
                                name="file",
                                append=False,
                                last_chunk=True,
                            )

                    # Attach observation parts to the latest thinking step for storage
                    if collected_thinking_steps:
                        for step in reversed(collected_thinking_steps):
                            if "observation" not in step:
                                step["observation"] = serialized_observation
                                break
                elif chunk["type"] in [ChatCompletionTypeEnum.DONE, ChatCompletionTypeEnum.CLARIFY]:
                    content = chunk.get("data")
                    if content:
                        final_response_text_parts.append(content)

                        # Tokenize content and stream token by token
                        tokens = encoder.encode(content)
                        for token in tokens:
                            token_text = encoder.decode([token])
                            await updater.add_artifact(
                                [Part(root=TextPart(text=token_text))],
                                artifact_id,
                                name="Text Response",
                                append=has_sent_first_chunk,
                                last_chunk=False
                            )
                            has_sent_first_chunk = True
                            # Add delay to simulate token latency
                            await asyncio.sleep(0.01)  # 10ms delay per token
                    # Capture token usage from the final chunk
                    total_input_tokens = chunk.get("input_tokens") or 0
                    total_output_tokens = chunk.get("output_tokens") or 0
        except ServerError:
            raise
        except Exception as e:
            logger.error(f"Error during agent execution: {e}")
            await updater.update_status(
                TaskState.failed,
                message=new_agent_text_message(str(e)),
            )
            raise ServerError(error=UnsupportedOperationError(message=str(e)))

        final_response_text = "".join(final_response_text_parts)
        # logger.debug(f"Final response text from OpenPAAgent: {final_response_text}")

        # Mark artifact as done
        await updater.add_artifact(
            [Part(root=TextPart(text=""))],
            artifact_id,
            name="Text Response",
            append=has_sent_first_chunk,
            last_chunk=True,
        )

        # Emit token_usage artifact if we have any token data
        if total_input_tokens > 0 or total_output_tokens > 0:
            logger.info(f"Total token usage - input: {total_input_tokens}, output: {total_output_tokens}")
            await updater.add_artifact(
                [Part(root=DataPart(data={
                    "token_usage": {
                        "input_tokens": total_input_tokens,
                        "output_tokens": total_output_tokens,
                    }
                }, kind="data", metadata=None))],
                name="token_usage"
            )

        final_parts: list[Part] = [Part(root=TextPart(text=final_response_text))]
        final_parts.extend(collected_file_parts)

        await updater.update_status(
            TaskState.working,
            Message(
                role=Role.agent,
                parts=final_parts,
                message_id=str(uuid.uuid4()),
                task_id=task.id,
                context_id=task.context_id,
            ),
        )

        # Persist completed messages to SQLite
        if self.conversation_storage and profile:
            try:
                conv = await self.conversation_storage.get_or_create_conversation(
                    profile=profile, context_id=context_id, task_id=task.id,
                )
                conversation_id = conv["id"]

                # Save user message
                user_parts = list(context_message.parts) if context_message and context_message.parts else None
                await self.conversation_storage.add_message(
                    conversation_id=conversation_id,
                    role="user",
                    content=query,
                    parts=user_parts,
                )

                # Save agent message
                token_usage_data = None
                if total_input_tokens > 0 or total_output_tokens > 0:
                    token_usage_data = {
                        "input_tokens": total_input_tokens,
                        "output_tokens": total_output_tokens,
                    }

                # Only persist non-text parts (FileParts); text is already in `content` column
                persist_parts = collected_file_parts if collected_file_parts else None

                await self.conversation_storage.add_message(
                    conversation_id=conversation_id,
                    role="agent",
                    content=final_response_text,
                    parts=persist_parts,
                    thinking_steps=collected_thinking_steps if collected_thinking_steps else None,
                    token_usage=token_usage_data,
                )

                # Update conversation title from first user message if still default
                if conv.get("title") == "Untitled Chat" and query:
                    title = query.strip()[:40] + ("..." if len(query.strip()) > 40 else "")
                    await self.conversation_storage.update_conversation(
                        conversation_id, title=title, task_id=task.id,
                    )
                else:
                    await self.conversation_storage.update_conversation(
                        conversation_id, task_id=task.id,
                    )

                logger.debug(f"Persisted messages for conversation {conversation_id}")
            except Exception as e:
                logger.error(f"Failed to persist conversation messages: {e}")

    async def cancel(self, context: RequestContext, event_queue: EventQueue):
        """Cancel the execution for the given context.

        Currently logs the cancellation attempt as the underlying ADK runner
        doesn't support direct cancellation of ongoing tasks.
        """
        session_id = context.context_id
        if session_id in self._active_sessions:
            logger.info(
                f"Cancellation requested for active weather session: {session_id}"
            )
            # TODO: Implement proper cancellation when ADK supports it
            self._active_sessions.discard(session_id)
        else:
            logger.debug(
                f"Cancellation requested for inactive weather session: {session_id}"
            )

        raise ServerError(error=UnsupportedOperationError())

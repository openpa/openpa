import asyncio
import json
import uuid
from typing import Any, Dict, List, Tuple, cast, Generator, AsyncGenerator, Optional
from enum import Enum

from openai.types.chat import ChatCompletionMessageParam
from openai.types import ResponseFormatJSONSchema

from a2a.types import Role, Message

from app.agent.reasoning_agent import ReasoningAgent
from app.lib.llm import LLMProvider
from app.lib.llm.model_groups import ModelGroupManager
from app.remote_agents import RoutingAgent
from app.storage import get_dynamic_config_storage
from app.constants import INTRODUCE_ASSISTANT, ChatCompletionTypeEnum
from app.constants.prompts import REQUIREMENT_PROMPT, LANGUAGE_INSTRUCTION
from app.lib.template import AssistantTemplateRenderer
from app.types import ReasoningStreamResponseType
from app.utils import logger, find_similar_items
from app.utils.common import convert_task_history_to_messages
from app.utils.context_storage import get_context, set_context
from app.utils.event_parser import parse_agent_events
from app.lib.me5 import Me5Embeddings
from app.utils import logger, build_table_embeddings


class OpenPAAgent:
    def __init__(
        self,
        runner: LLMProvider | None,
        routing_agent: RoutingAgent,
        me5_embedding: Me5Embeddings,
        model_group_mgr: ModelGroupManager | None = None,
    ):
        self._runners: dict[str, LLMProvider] = {}
        if runner is not None:
            self._runners["admin"] = runner
        self.routing_agent = routing_agent
        self.me5_embedding = me5_embedding
        self._model_group_mgr = model_group_mgr
        self.agents_name = list(routing_agent.agents_info.keys())

        agent_data = {
            agent_info['card'].name: agent_info['card'].model_dump_json()
            for agent_name, agent_info in routing_agent.agents_info.items()
        }
        self.embedding_table = build_table_embeddings(self.me5_embedding, agent_data)
        logger.debug(f"Agent card embeddings data: {self.embedding_table.dataframe}")

    def _ensure_runner(self, profile: str) -> LLMProvider:
        """Create (or re-create) the LLM provider for a profile from current config.

        Always reads the latest config so that model group changes take effect
        without requiring a server restart.
        """
        if self._model_group_mgr is None:
            config_storage = get_dynamic_config_storage()
            self._model_group_mgr = ModelGroupManager(config_storage)

        if not self._model_group_mgr.config_storage.is_setup_complete():
            raise ValueError("LLM provider is not configured. Run the setup wizard first.")

        runner = self._model_group_mgr.create_llm_for_group("high", profile=profile)
        self._runners[profile] = runner
        return runner

    async def run(
        self,
        query: str,
        task_history: List[Any],
        context_id: str | None,
        profile: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> AsyncGenerator[ReasoningStreamResponseType, None]:
        logger.info(f"Running OpenPAAgent with query: {query} and profile: {profile}")
        runner = self._ensure_runner(profile)
        self.reasoning_agent = ReasoningAgent(
            llm=runner,
            agents_info=self.routing_agent.get_agents_for_profile(profile),
            routing_agent=self.routing_agent,
            context_id=context_id,
            profile=profile,
            metadata=metadata,
        )

        async for result in self.reasoning_agent.run(query, task_history):
            # logger.debug(f"Yielding reasoning agent result: {result}")
            yield result

    async def _collect_agent_responses(
        self,
        agent_names: List[str],
        query: str,
        context_id: str | None,
    ) -> Dict[str, str]:
        is_empty_agent = not agent_names
        if is_empty_agent:
            return {}

        agent_tasks = [
            self.routing_agent.request(agent_name, query, context_id)
            for agent_name in agent_names
        ]

        results = await asyncio.gather(*agent_tasks, return_exceptions=True)
        responses: Dict[str, str] = {}

        logger.debug(f"Agent tasks results: {results}")

        for index, agent_name in enumerate(agent_names):
            result = results[index]
            if isinstance(result, Exception):
                logger.error(f"Agent {agent_name} failed with error: {result}")
                responses[agent_name] = f"Error processing agent {agent_name}: {result}"
            else:
                events = cast(List[Any], result)
                response_text, _ = parse_agent_events(events)
                logger.debug(f"Agent {agent_name} aggregated response: {response_text}")
                responses[agent_name] = response_text

        return responses

    def _format_agent_responses(
        self,
        agent_responses: Dict[str, str],
    ) -> Tuple[List[str], List[str]]:
        data_tool_contents: List[str] = []
        # Since we're waiting for completion, all responses here are considered completed
        # but we'll return the same content for both lists to maintain interface compatibility

        for agent_name, response_text in agent_responses.items():
            if response_text:
                content = f"Tool: {agent_name} - Response data: {response_text}\n"
                data_tool_contents.append(content)

        return data_tool_contents, data_tool_contents

    def _build_messages(
        self,
        system_prompt: str,
        task_history: list[Message] | None,
    ) -> List[ChatCompletionMessageParam]:
        messages: List[ChatCompletionMessageParam] = [
            {"role": "system", "content": system_prompt},
        ]

        history_messages = convert_task_history_to_messages(task_history or [])
        messages.extend(history_messages)
        return messages

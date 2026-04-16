"""Top-level OpenPA agent.

Wraps the registry-driven :class:`ReasoningAgent` for each profile and
manages the per-profile "high" group LLM. Embeddings of the connected tools
(used for semantic tool search) are kept in sync with the registry through a
change callback.
"""

from __future__ import annotations

from typing import Any, AsyncGenerator, List

from app.agent.reasoning_agent import ReasoningAgent
from app.lib.embedding import GrpcEmbeddings
from app.lib.llm import LLMProvider
from app.lib.llm.model_groups import ModelGroupManager
from app.storage import get_dynamic_config_storage
from app.storage.dynamic_config_storage import DynamicConfigStorage
from app.tools import ToolRegistry
from app.types import ReasoningStreamResponseType
from app.utils import build_table_embeddings, logger


class OpenPAAgent:
    """Per-profile reasoning entry point."""

    def __init__(
        self,
        registry: ToolRegistry,
        embedding: GrpcEmbeddings,
        runner: LLMProvider | None = None,
        model_group_mgr: ModelGroupManager | None = None,
        config_storage: DynamicConfigStorage | None = None,
    ):
        self._runners: dict[str, LLMProvider] = {}
        if runner is not None:
            self._runners["admin"] = runner
        self.registry = registry
        self.embedding = embedding
        self._model_group_mgr = model_group_mgr
        self._config_storage = config_storage
        self.embedding_table = self._build_connected_embeddings()

        # Refresh embeddings whenever the registry mutates
        self.registry.set_change_callback(self.update_embeddings)

    def _build_connected_embeddings(self):
        """Build embedding table from connectable tools (skip stubs / hidden)."""
        agent_data: dict[str, str] = {}
        for tool in self.registry.all_tools():
            if tool.hidden:
                continue
            if getattr(tool, "is_stub", False):
                continue
            card = tool.get_card()
            if card is None:
                continue
            agent_data[card.name] = card.model_dump_json()
        table = build_table_embeddings(self.embedding, agent_data)
        logger.debug(f"Tool card embeddings: {table.dataframe}")
        return table

    def update_embeddings(self) -> None:
        """Rebuild the embedding table after tools are added/removed."""
        self.embedding_table = self._build_connected_embeddings()
        logger.info(f"Updated tool embeddings ({len(self.embedding_table)} entries)")

    def _ensure_runner(self, profile: str) -> LLMProvider:
        """Create (or re-create) the LLM provider for ``profile`` from current config."""
        if self._model_group_mgr is None:
            self._model_group_mgr = ModelGroupManager(get_dynamic_config_storage())

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
        reasoning: bool = True,
    ) -> AsyncGenerator[ReasoningStreamResponseType, None]:
        logger.info(f"Running OpenPAAgent with query: {query} and profile: {profile}")
        runner = self._ensure_runner(profile)
        reasoning_agent = ReasoningAgent(
            llm=runner,
            registry=self.registry,
            profile=profile,
            context_id=context_id,
            reasoning=reasoning,
        )

        async for result in reasoning_agent.run(query, task_history):
            yield result

"""LLM provider factory for creating provider instances by name.

Used to create per-MCP-server LLM providers from stored configuration.
Supports reading API keys from DynamicConfigStorage (SQLite) with env fallback.
"""

import json
from typing import Optional

from app.config.settings import BaseConfig
from .base import LLMProvider

SUPPORTED_LLM_PROVIDERS = ["groq", "openai", "ollama", "vertexai", "vllm"]


def _get_api_key(provider_name: str, config_storage=None, profile: str | None = None) -> str:
    """Get API key for a provider from config storage or env fallback."""
    if config_storage is not None:
        kwargs = {"profile": profile} if profile is not None else {}
        val = config_storage.get("llm_config", f"{provider_name}.api_key", **kwargs)
        if val:
            return val
    return BaseConfig.get_provider_api_key(provider_name, profile=profile)


def create_llm_provider(
    provider_name: str,
    model_name: Optional[str] = None,
    config_storage=None,
    profile: str | None = None,
    default_reasoning_effort: Optional[str] = None,
) -> LLMProvider:
    """Create an LLMProvider instance from a provider name and model.

    Args:
        provider_name: One of "groq", "openai", "ollama", "vertexai", "vllm"
        model_name: Model identifier. Defaults to BaseConfig.REASONING_MODEL_NAME.
        config_storage: Optional DynamicConfigStorage for reading API keys from SQLite.
        profile: Profile name for scoped config lookup. Defaults to "admin" in storage.

    Returns:
        An LLMProvider instance.

    Raises:
        ValueError: If provider_name is unknown or required credentials are missing.
    """
    model = model_name or BaseConfig.REASONING_MODEL_NAME
    # Strip provider prefix if model string starts with "provider_name/"
    # (UI sends "provider/model_id" format, e.g. "groq/openai/gpt-oss-120b")
    if model and provider_name and model.startswith(f"{provider_name}/"):
        model = model[len(provider_name) + 1:]
    _kw = {"profile": profile} if profile is not None else {}

    provider: LLMProvider

    if provider_name == "groq":
        api_key = _get_api_key("groq", config_storage, profile=profile)
        if not api_key:
            raise ValueError("Groq API key is not configured")
        from .groq import GroqLLMProvider
        provider = GroqLLMProvider(api_key=api_key, model_name=model, default_reasoning_effort=default_reasoning_effort)

    elif provider_name == "openai":
        api_key = _get_api_key("openai", config_storage, profile=profile)
        if not api_key:
            raise ValueError("OpenAI API key is not configured")
        from .openai import OpenAILLMProvider
        provider = OpenAILLMProvider(api_key=api_key, model_name=model, default_reasoning_effort=default_reasoning_effort)

    elif provider_name == "ollama":
        from .ollama import OllamaLLMProvider
        provider = OllamaLLMProvider(api_key="ollama", model_name=model, default_reasoning_effort=default_reasoning_effort)

    elif provider_name == "vertexai":
        service_account = None
        if config_storage:
            service_account = config_storage.get("llm_config", "vertexai.service_account", **_kw)
        if not service_account:
            raise ValueError("VertexAI service account is not configured")

        credentials = json.loads(service_account)

        project_id = None
        if config_storage:
            project_id = config_storage.get("llm_config", "vertexai.project_id", **_kw)

        location = None
        if config_storage:
            location = config_storage.get("llm_config", "vertexai.location", **_kw)
        if not location:
            location = "global"

        from .vertexai import VertexAILLMProvider
        provider = VertexAILLMProvider(
            credentials=credentials,
            project_id=project_id,
            location=location,
            model_name=model,
            default_reasoning_effort=default_reasoning_effort,
        )

    elif provider_name == "vllm":
        from .vllm import VllmLLMProvider
        provider = VllmLLMProvider(api_key="vllm", model_name=model, default_reasoning_effort=default_reasoning_effort)

    else:
        raise ValueError(
            f"Unknown LLM provider '{provider_name}'. "
            f"Supported: {', '.join(SUPPORTED_LLM_PROVIDERS)}"
        )

    provider.provider_name = provider_name
    return provider

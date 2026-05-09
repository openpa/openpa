from openai.types import ResponseFormatJSONObject, ResponseFormatJSONSchema, ResponseFormatText
from openai.types.chat import ChatCompletionMessageParam, ChatCompletionToolUnionParam
from typing import AsyncGenerator, Any, Dict, List, Optional, Union
from abc import ABC, abstractmethod

from app.types import ChatCompletionStreamResponseType


class LLMProvider(ABC):
    provider_name: str = ""

    @property
    def model_label(self) -> str:
        """Human-readable label combining provider and model, e.g. 'Groq GPT-OSS-120B'."""
        name = getattr(self, "model_name", "")
        if self.provider_name and name:
            return f"{self.provider_name.capitalize()} {name}"
        return name or "unknown"

    @abstractmethod
    def chat_completion_stream(
        self,
        messages: List[ChatCompletionMessageParam],
        response_format: Optional[Union[ResponseFormatText, ResponseFormatJSONSchema, ResponseFormatJSONObject]] = None,
        tools: Optional[List[ChatCompletionToolUnionParam]] = None,
        # "auto" | "none" | "required" | ChatCompletionNamedToolChoice
        tool_choice: Optional[Union[str, Dict[str, Any]]] = None,
        parallel_tool_calls: Optional[bool] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        reasoning_effort: Optional[str] = None,  # "low" | "medium" | "high"
        max_tokens: Optional[int] = None,
        stop: Optional[str] = None,
        retry: Optional[int] = None,
        args: Optional[Dict[str, Any]] = None,
    ) -> AsyncGenerator[ChatCompletionStreamResponseType, None]:
        pass

    @abstractmethod
    def chat_completion(
        self,
        messages: List[ChatCompletionMessageParam],
        response_format: Optional[Union[ResponseFormatText, ResponseFormatJSONSchema, ResponseFormatJSONObject]] = None,
        tools: Optional[List[ChatCompletionToolUnionParam]] = None,
        # "auto" | "none" | "required" | ChatCompletionNamedToolChoice
        tool_choice: Optional[Union[str, Dict[str, Any]]] = None,
        parallel_tool_calls: Optional[bool] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        reasoning_effort: Optional[str] = None,  # "low" | "medium" | "high"
        max_tokens: Optional[int] = None,
        stop: Optional[str] = None,
        retry: Optional[int] = None,
        args: Optional[Dict[str, Any]] = None,
    ) -> AsyncGenerator[ChatCompletionStreamResponseType, None]:
        pass

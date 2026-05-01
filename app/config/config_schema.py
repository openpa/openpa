"""Declarative schema for the Settings → Config page.

Each :class:`Field` describes one user-tunable runtime setting: its primitive
type, the dotted TOML path of its default, a hardcoded fallback if the TOML
is missing the key, and optional validation hints (``min``/``max``/``step``,
``enum``). Fields are grouped into :class:`ConfigGroup`s which become the
sections in the UI.

Stored values live in the per-profile ``user_config`` SQLite table. Reads
follow the priority chain: SQLite override > TOML default > ``default_fallback``.

Adding a new tunable knob is a 4-step process:
  1. Add an entry to ``CONFIG_SCHEMA`` here, in the appropriate group.
  2. Add the matching default under the same dotted path in ``settings.toml``.
  3. Replace the hardcoded literal at the consumption site with
     ``get_user_config("group.key", profile=...)``.
  4. (No frontend change — the page renders dynamically from the schema.)
"""

from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
from typing import Any, Literal

FieldType = Literal["number", "string", "boolean", "enum"]


@dataclass(frozen=True)
class Field:
    """Schema for one configurable value."""

    type: FieldType
    default_toml: str
    default_fallback: Any
    label: str | None = None
    description: str | None = None
    min: float | None = None
    max: float | None = None
    step: float | None = None
    enum: tuple[str, ...] | None = None

    def coerce(self, raw: Any) -> Any:
        """Convert a raw stored value (string from SQLite, or native from TOML) to the declared type."""
        if raw is None:
            return None
        if self.type == "boolean":
            if isinstance(raw, bool):
                return raw
            return str(raw).strip().lower() in ("true", "1", "yes", "on")
        if self.type == "number":
            if isinstance(raw, bool):
                return int(raw)
            if isinstance(raw, (int, float)):
                # Preserve int when no fractional part, else float.
                return int(raw) if float(raw).is_integer() and isinstance(self.default_fallback, int) else float(raw)
            text = str(raw).strip()
            try:
                if "." in text or "e" in text.lower():
                    return float(text)
                return int(text)
            except ValueError as exc:
                raise ValueError(f"Cannot parse {text!r} as number") from exc
        if self.type == "enum":
            text = str(raw)
            if self.enum and text not in self.enum:
                raise ValueError(f"Value {text!r} not in enum {self.enum}")
            return text
        return str(raw)

    def validate(self, value: Any) -> None:
        """Raise ``ValueError`` if ``value`` violates declared bounds or enum."""
        if self.type == "number":
            if self.min is not None and value < self.min:
                raise ValueError(f"Value {value} below minimum {self.min}")
            if self.max is not None and value > self.max:
                raise ValueError(f"Value {value} above maximum {self.max}")
        elif self.type == "enum":
            if self.enum and value not in self.enum:
                raise ValueError(f"Value {value!r} not in enum {self.enum}")


@dataclass(frozen=True)
class ConfigGroup:
    """A logical grouping of fields (one card/section in the UI)."""

    label: str
    description: str
    fields: dict[str, Field] = dataclass_field(default_factory=dict)


CONFIG_SCHEMA: dict[str, ConfigGroup] = {
    "agent": ConfigGroup(
        label="Reasoning Agent",
        description="Controls the ReAct loop's iteration limits and per-call LLM parameters.",
        fields={
            "max_steps": Field(
                type="number", default_toml="agent.max_steps", default_fallback=40,
                label="Max steps",
                description="Maximum ReAct iterations before the agent stops a turn.",
                min=1, max=200,
            ),
            "max_llm_retries": Field(
                type="number", default_toml="agent.max_llm_retries", default_fallback=2,
                label="Max LLM retries",
                description="How many times the loop retries after an LLM error before giving up.",
                min=0, max=10,
            ),
            "reasoning_temperature": Field(
                type="number", default_toml="agent.reasoning_temperature", default_fallback=1.0,
                label="Reasoning temperature",
                description="Sampling temperature for the main reasoning LLM call.",
                min=0, max=2, step=0.1,
            ),
            "reasoning_max_tokens": Field(
                type="number", default_toml="agent.reasoning_max_tokens", default_fallback=32768,
                label="Reasoning max tokens",
                description="Output token cap for the reasoning LLM call.",
                min=256, max=131072,
            ),
            "reasoning_retry": Field(
                type="number", default_toml="agent.reasoning_retry", default_fallback=3,
                label="Per-call retry count",
                description="How many times an individual reasoning LLM call retries on transient errors.",
                min=0, max=10,
            ),
            "steps_length": Field(
                type="number", default_toml="agent.steps_length", default_fallback=80,
                label="Steps history length",
                description="Maximum number of recent ReAct step entries kept in the prompt context. Older entries are dropped once this is exceeded.",
                min=5, max=500,
            ),
        },
    ),
    "history": ConfigGroup(
        label="Conversation History",
        description="Token-budget limits applied when assembling the message window for each LLM call.",
        fields={
            "max_tokens_total": Field(
                type="number", default_toml="history.max_tokens_total", default_fallback=5000,
                label="Total history tokens",
                description="Maximum total tokens of past messages included in each prompt.",
                min=500, max=200000,
            ),
            "max_tokens_per_message": Field(
                type="number", default_toml="history.max_tokens_per_message", default_fallback=500,
                label="Per-message tokens",
                description="Each message is truncated to at most this many tokens before assembly.",
                min=50, max=20000,
            ),
        },
    ),
    "skill_classifier": ConfigGroup(
        label="Skill Classifier",
        description="Lightweight LLM that decides whether a request maps to a registered skill.",
        fields={
            "temperature": Field(
                type="number", default_toml="skill_classifier.temperature", default_fallback=0.0,
                label="Temperature",
                description="Sampling temperature; keep low for deterministic classification.",
                min=0, max=2, step=0.1,
            ),
            "max_tokens": Field(
                type="number", default_toml="skill_classifier.max_tokens", default_fallback=64,
                label="Max tokens",
                description="Output token cap for the classifier call.",
                min=8, max=2048,
            ),
            "retry": Field(
                type="number", default_toml="skill_classifier.retry", default_fallback=2,
                label="Retry count",
                description="Retries on transient classifier LLM errors.",
                min=0, max=10,
            ),
        },
    ),
    "summarizer": ConfigGroup(
        label="Trace Summarizer",
        description="LLM that compresses long reasoning traces back into the conversation history.",
        fields={
            "temperature": Field(
                type="number", default_toml="summarizer.temperature", default_fallback=0.3,
                label="Temperature",
                description="Sampling temperature for the summarization call.",
                min=0, max=2, step=0.1,
            ),
            "max_tokens": Field(
                type="number", default_toml="summarizer.max_tokens", default_fallback=1024,
                label="Max tokens",
                description="Output token cap for the summary.",
                min=128, max=8192,
            ),
            "retry": Field(
                type="number", default_toml="summarizer.retry", default_fallback=2,
                label="Retry count",
                description="Retries on transient summarizer LLM errors.",
                min=0, max=10,
            ),
        },
    ),
}


def lookup(key: str) -> tuple[str, str, Field]:
    """Resolve a dotted ``group.field`` key to ``(group_name, field_name, Field)``.

    Raises ``KeyError`` if the group or field is unknown.
    """
    if "." not in key:
        raise KeyError(f"Config key must be of the form 'group.field', got {key!r}")
    group_name, field_name = key.split(".", 1)
    group = CONFIG_SCHEMA.get(group_name)
    if group is None:
        raise KeyError(f"Unknown config group: {group_name!r}")
    field = group.fields.get(field_name)
    if field is None:
        raise KeyError(f"Unknown config field: {key!r}")
    return group_name, field_name, field


def all_keys() -> list[str]:
    """Every dotted ``group.field`` key declared in the schema."""
    return [
        f"{group_name}.{field_name}"
        for group_name, group in CONFIG_SCHEMA.items()
        for field_name in group.fields
    ]

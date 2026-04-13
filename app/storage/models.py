"""SQLAlchemy ORM models for OpenPA's persistence layer.

All tool-related state is keyed by ``tool_id`` (a slugified, globally-unique
identifier). Profiles cascade-delete their conversations, messages, configs,
profile-tool memberships, and per-profile tool configs.

Tables
------
- profiles            : tenant identity
- conversations       : chat threads (FK profile, CASCADE)
- messages            : per-conversation messages (FK conversation, CASCADE)
- auth_tokens         : per-profile OAuth tokens for tools (FK profile, CASCADE)
- tools               : global tool registry (one row per tool_id)
- profile_tools       : M:N join for A2A and MCP tools per profile
                        (FK profile CASCADE, FK tool CASCADE)
- tool_configs        : per-profile per-tool scoped config
                        (FK profile CASCADE, FK tool CASCADE)
- server_config       : global server settings (no FK)
- llm_config          : per-profile LLM settings (FK profile, CASCADE)
"""

import uuid
from typing import Any

from sqlalchemy import JSON, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from a2a.server.models import Base


class ProfileModel(Base):
    __tablename__ = "profiles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    created_at: Mapped[float] = mapped_column(Float, nullable=False)
    updated_at: Mapped[float] = mapped_column(Float, nullable=False)


class ConversationModel(Base):
    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    profile: Mapped[str] = mapped_column(
        String(128), ForeignKey("profiles.name", ondelete="CASCADE"), nullable=False, index=True
    )
    context_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    task_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    title: Mapped[str] = mapped_column(String(256), default="Untitled Chat")
    created_at: Mapped[float] = mapped_column(Float, nullable=False)
    updated_at: Mapped[float] = mapped_column(Float, nullable=False)


class MessageModel(Base):
    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    conversation_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    parts: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    thinking_steps: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    token_usage: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    message_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True, name="metadata")
    created_at: Mapped[float] = mapped_column(Float, nullable=False)
    ordering: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class AuthTokenModel(Base):
    __tablename__ = "auth_tokens"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    # ``agent_name`` is kept as the lookup key (instead of FK-ing to tools)
    # because OAuth tokens are sometimes saved before the corresponding tool
    # row has been created (e.g., during MCP server registration). Profile
    # cascade still cleans them up on profile deletion.
    agent_name: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    profile: Mapped[str] = mapped_column(
        String(128), ForeignKey("profiles.name", ondelete="CASCADE"), nullable=False, index=True
    )
    agent_type: Mapped[str] = mapped_column(String(32), nullable=False, default="a2a")
    token_kind: Mapped[str] = mapped_column(String(32), nullable=False, default="access_token")
    token: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[float] = mapped_column(Float, nullable=False)


class ToolModel(Base):
    """Global tool registry. Intrinsic tools are NOT persisted here (always in-memory)."""
    __tablename__ = "tools"

    tool_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    tool_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    arguments_schema: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    extra: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    owner_profile: Mapped[str | None] = mapped_column(
        String(128), ForeignKey("profiles.name", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[float] = mapped_column(Float, nullable=False)
    updated_at: Mapped[float] = mapped_column(Float, nullable=False)

    __table_args__ = (
        UniqueConstraint("tool_type", "source", name="uq_tools_type_source"),
    )


class ProfileToolModel(Base):
    """M:N visibility/enabled state. Populated only for a2a / mcp tool types."""
    __tablename__ = "profile_tools"

    profile: Mapped[str] = mapped_column(
        String(128), ForeignKey("profiles.name", ondelete="CASCADE"), primary_key=True
    )
    # ON UPDATE CASCADE so a tool can be atomically renamed (used to displace a
    # dynamic tool when a fixed-name intrinsic/built-in tool claims its slug).
    tool_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("tools.tool_id", ondelete="CASCADE", onupdate="CASCADE"),
        primary_key=True,
    )
    enabled: Mapped[bool] = mapped_column(Integer, nullable=False, default=0)
    added_at: Mapped[float] = mapped_column(Float, nullable=False)


class ToolConfigModel(Base):
    """Per-profile per-tool configuration scoped by purpose.

    scope ∈ {'arg', 'variable', 'llm', 'meta'}.
    """
    __tablename__ = "tool_configs"

    profile: Mapped[str] = mapped_column(
        String(128), ForeignKey("profiles.name", ondelete="CASCADE"), primary_key=True
    )
    tool_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("tools.tool_id", ondelete="CASCADE", onupdate="CASCADE"),
        primary_key=True,
    )
    scope: Mapped[str] = mapped_column(String(16), primary_key=True)
    key: Mapped[str] = mapped_column(String(256), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    is_secret: Mapped[bool] = mapped_column(Integer, default=False)
    updated_at: Mapped[float] = mapped_column(Float, nullable=False)


class ServerConfigModel(Base):
    """Global server-wide dynamic configuration."""
    __tablename__ = "server_config"

    key: Mapped[str] = mapped_column(String(256), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    is_secret: Mapped[bool] = mapped_column(Integer, default=False)
    updated_at: Mapped[float] = mapped_column(Float, nullable=False)


class LLMConfigModel(Base):
    """Per-profile LLM provider credentials and model group assignments."""
    __tablename__ = "llm_config"

    profile: Mapped[str] = mapped_column(
        String(128), ForeignKey("profiles.name", ondelete="CASCADE"), primary_key=True,
    )
    key: Mapped[str] = mapped_column(String(256), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    is_secret: Mapped[bool] = mapped_column(Integer, default=False)
    updated_at: Mapped[float] = mapped_column(Float, nullable=False)

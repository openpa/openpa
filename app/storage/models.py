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
    agent_name: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    profile: Mapped[str] = mapped_column(
        String(128), ForeignKey("profiles.name", ondelete="CASCADE"), nullable=False, index=True
    )
    agent_type: Mapped[str] = mapped_column(String(32), nullable=False, default="a2a")
    token_kind: Mapped[str] = mapped_column(String(32), nullable=False, default="access_token")
    token: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[float] = mapped_column(Float, nullable=False)


class RemoteAgentModel(Base):
    __tablename__ = "remote_agents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    url: Mapped[str] = mapped_column(String(1024), nullable=False)
    profile: Mapped[str] = mapped_column(
        String(128), ForeignKey("profiles.name", ondelete="CASCADE"), nullable=False, index=True
    )
    created_at: Mapped[float] = mapped_column(Float, nullable=False)


class MCPServerModel(Base):
    __tablename__ = "mcp_servers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    url: Mapped[str] = mapped_column(String(1024), nullable=False)
    profile: Mapped[str] = mapped_column(
        String(128), ForeignKey("profiles.name", ondelete="CASCADE"), nullable=False, index=True
    )
    config_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[float] = mapped_column(Float, nullable=False)


class ServerConfigModel(Base):
    """Server-wide dynamic configuration (set during setup wizard, mutable via admin)."""
    __tablename__ = "server_config"

    key: Mapped[str] = mapped_column(String(256), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    is_secret: Mapped[bool] = mapped_column(Integer, default=False)
    updated_at: Mapped[float] = mapped_column(Float, nullable=False)


class LLMConfigModel(Base):
    """Dynamic LLM configuration (provider credentials, model group assignments), scoped per profile."""
    __tablename__ = "llm_config"

    profile: Mapped[str] = mapped_column(
        String(128), ForeignKey("profiles.name", ondelete="CASCADE"), primary_key=True,
    )
    key: Mapped[str] = mapped_column(String(256), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    is_secret: Mapped[bool] = mapped_column(Integer, default=False)
    updated_at: Mapped[float] = mapped_column(Float, nullable=False)


class ToolConfigModel(Base):
    """Per-tool configuration (secrets, enabled state, model overrides), scoped per profile."""
    __tablename__ = "tool_configs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    profile: Mapped[str] = mapped_column(
        String(128), ForeignKey("profiles.name", ondelete="CASCADE"), nullable=False, index=True,
    )
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    config_key: Mapped[str] = mapped_column(String(256), nullable=False)
    config_value: Mapped[str] = mapped_column(Text, nullable=False)
    is_secret: Mapped[bool] = mapped_column(Integer, default=False)
    updated_at: Mapped[float] = mapped_column(Float, nullable=False)

    __table_args__ = (
        UniqueConstraint("profile", "tool_name", "config_key", name="uq_tool_configs_profile_name_key"),
    )

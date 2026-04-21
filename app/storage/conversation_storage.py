import time
import uuid
from pathlib import Path

from sqlalchemy import Table, delete, event, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import class_mapper

from a2a.server.models import Base

from app.storage.models import (
    AuthTokenModel, ConversationModel, LLMConfigModel,
    MessageModel, ProfileModel, ProfileToolModel, ServerConfigModel,
    ToolConfigModel, ToolModel,
)
from app.utils.logger import logger


class ConversationStorage:
    """Async SQLite-backed conversation and message storage."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        # Ensure parent directory exists
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.engine: AsyncEngine = create_async_engine(
            f"sqlite+aiosqlite:///{db_path}",
            echo=False,
        )
        self.async_session_maker = async_sessionmaker(self.engine, expire_on_commit=False)
        self._initialized = False

    async def initialize(self):
        """Create tables if they don't exist and enable WAL mode + foreign keys."""
        if self._initialized:
            return

        # Enable WAL mode and foreign key enforcement
        async with self.engine.begin() as conn:
            await conn.execute(text("PRAGMA journal_mode=WAL"))
            await conn.execute(text("PRAGMA foreign_keys=ON"))

        # Enforce foreign keys on every new connection
        @event.listens_for(self.engine.sync_engine, "connect")
        def _set_sqlite_pragma(dbapi_conn, connection_record):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        # Create tables. Order matters because of FK dependencies:
        # profiles → tools → (profile_tools, tool_configs, auth_tokens)
        async with self.engine.begin() as conn:
            tables_to_create = []
            for model_class in [
                ProfileModel,
                ConversationModel, MessageModel,
                ServerConfigModel, LLMConfigModel,
                ToolModel,
                ProfileToolModel, ToolConfigModel, AuthTokenModel,
            ]:
                mapper = class_mapper(model_class)
                for table in mapper.tables:
                    if isinstance(table, Table):
                        tables_to_create.append(table)
            await conn.run_sync(Base.metadata.create_all, tables=tables_to_create)

            # Additive migrations for columns added after the initial schema.
            # SQLite doesn't support IF NOT EXISTS on ADD COLUMN, so we catch
            # the duplicate-column error from repeated boots.
            try:
                await conn.execute(text(
                    "ALTER TABLE profiles ADD COLUMN skill_mode VARCHAR(16) "
                    "NOT NULL DEFAULT 'manual'"
                ))
            except Exception:  # noqa: BLE001
                pass

        self._initialized = True
        logger.info(f"ConversationStorage initialized with database: {self.db_path}")

    async def _ensure_initialized(self):
        if not self._initialized:
            await self.initialize()

    # ── Profile CRUD ──

    async def create_profile(self, name: str) -> dict:
        await self._ensure_initialized()
        now = time.time() * 1000
        profile = ProfileModel(
            id=str(uuid.uuid4()),
            name=name,
            created_at=now,
            updated_at=now,
        )
        async with self.async_session_maker.begin() as session:
            session.add(profile)
        return self._profile_to_dict(profile)

    async def get_profile(self, name: str) -> dict | None:
        await self._ensure_initialized()
        async with self.async_session_maker() as session:
            result = await session.execute(
                select(ProfileModel).where(ProfileModel.name == name)
            )
            profile = result.scalar_one_or_none()
            return self._profile_to_dict(profile) if profile else None

    async def list_profiles(self) -> list[dict]:
        await self._ensure_initialized()
        async with self.async_session_maker() as session:
            result = await session.execute(
                select(ProfileModel).order_by(ProfileModel.name.asc())
            )
            profiles = result.scalars().all()
            return [self._profile_to_dict(p) for p in profiles]

    async def delete_profile(self, name: str) -> bool:
        await self._ensure_initialized()
        async with self.async_session_maker.begin() as session:
            # Cascade will handle conversations and messages
            result = await session.execute(
                delete(ProfileModel).where(ProfileModel.name == name)
            )
            return result.rowcount > 0

    async def profile_exists(self, name: str) -> bool:
        await self._ensure_initialized()
        async with self.async_session_maker() as session:
            result = await session.execute(
                select(func.count()).select_from(ProfileModel).where(ProfileModel.name == name)
            )
            return result.scalar() > 0

    async def get_skill_mode(self, name: str) -> str:
        await self._ensure_initialized()
        async with self.async_session_maker() as session:
            result = await session.execute(
                select(ProfileModel.skill_mode).where(ProfileModel.name == name)
            )
            mode = result.scalar_one_or_none()
            return mode or "manual"

    async def set_skill_mode(self, name: str, mode: str) -> bool:
        if mode not in ("manual", "automatic"):
            raise ValueError(f"Invalid skill_mode: {mode!r}")
        await self._ensure_initialized()
        async with self.async_session_maker.begin() as session:
            result = await session.execute(
                update(ProfileModel)
                .where(ProfileModel.name == name)
                .values(skill_mode=mode, updated_at=time.time() * 1000)
            )
            return result.rowcount > 0

    # ── Conversation CRUD ──

    async def create_conversation(
        self, profile: str, context_id: str | None = None,
        task_id: str | None = None, title: str = "Untitled Chat",
    ) -> dict:
        await self._ensure_initialized()
        now = time.time() * 1000
        conv = ConversationModel(
            id=str(uuid.uuid4()),
            profile=profile,
            context_id=context_id,
            task_id=task_id,
            title=title,
            created_at=now,
            updated_at=now,
        )
        async with self.async_session_maker.begin() as session:
            session.add(conv)
        return self._conv_to_dict(conv)

    async def get_conversation(self, conversation_id: str) -> dict | None:
        await self._ensure_initialized()
        async with self.async_session_maker() as session:
            result = await session.execute(
                select(ConversationModel).where(ConversationModel.id == conversation_id)
            )
            conv = result.scalar_one_or_none()
            return self._conv_to_dict(conv) if conv else None

    async def get_conversation_by_context(self, profile: str, context_id: str) -> dict | None:
        await self._ensure_initialized()
        async with self.async_session_maker() as session:
            result = await session.execute(
                select(ConversationModel).where(
                    ConversationModel.profile == profile,
                    ConversationModel.context_id == context_id,
                )
            )
            conv = result.scalar_one_or_none()
            return self._conv_to_dict(conv) if conv else None

    async def list_conversations(self, profile: str, limit: int = 50, offset: int = 0) -> list[dict]:
        await self._ensure_initialized()
        async with self.async_session_maker() as session:
            # Subquery to count messages per conversation
            msg_count_subq = (
                select(
                    MessageModel.conversation_id,
                    func.count(MessageModel.id).label("message_count"),
                )
                .group_by(MessageModel.conversation_id)
                .subquery()
            )
            result = await session.execute(
                select(ConversationModel, msg_count_subq.c.message_count)
                .outerjoin(msg_count_subq, ConversationModel.id == msg_count_subq.c.conversation_id)
                .where(ConversationModel.profile == profile)
                .order_by(ConversationModel.updated_at.desc())
                .limit(limit)
                .offset(offset)
            )
            rows = result.all()
            return [
                {**self._conv_to_dict(conv), "message_count": count or 0}
                for conv, count in rows
            ]

    async def update_conversation(self, conversation_id: str, **kwargs) -> None:
        await self._ensure_initialized()
        kwargs["updated_at"] = time.time() * 1000
        async with self.async_session_maker.begin() as session:
            await session.execute(
                update(ConversationModel)
                .where(ConversationModel.id == conversation_id)
                .values(**kwargs)
            )

    async def delete_conversation(self, conversation_id: str) -> bool:
        await self._ensure_initialized()
        async with self.async_session_maker.begin() as session:
            # Delete messages first (cascade should handle this, but be explicit)
            await session.execute(
                delete(MessageModel).where(MessageModel.conversation_id == conversation_id)
            )
            result = await session.execute(
                delete(ConversationModel).where(ConversationModel.id == conversation_id)
            )
            return result.rowcount > 0

    async def delete_all_conversations(self, profile: str) -> int:
        await self._ensure_initialized()
        async with self.async_session_maker.begin() as session:
            # Get conversation IDs for this profile
            result = await session.execute(
                select(ConversationModel.id).where(ConversationModel.profile == profile)
            )
            conv_ids = [row[0] for row in result.all()]
            if not conv_ids:
                return 0
            # Delete messages
            await session.execute(
                delete(MessageModel).where(MessageModel.conversation_id.in_(conv_ids))
            )
            # Delete conversations
            result = await session.execute(
                delete(ConversationModel).where(ConversationModel.profile == profile)
            )
            return result.rowcount

    async def get_or_create_conversation(
        self, profile: str, context_id: str | None = None,
        task_id: str | None = None,
    ) -> dict:
        if context_id:
            conv = await self.get_conversation_by_context(profile, context_id)
            if conv:
                return conv
        return await self.create_conversation(
            profile=profile, context_id=context_id, task_id=task_id,
        )

    # ── Message CRUD ──

    async def add_message(
        self, conversation_id: str, role: str, content: str | None = None,
        parts: list | None = None, thinking_steps: list | None = None,
        token_usage: dict | None = None, metadata: dict | None = None,
        summary: str | None = None,
    ) -> dict:
        await self._ensure_initialized()
        now = time.time() * 1000

        async with self.async_session_maker.begin() as session:
            # Get next ordering number
            result = await session.execute(
                select(func.coalesce(func.max(MessageModel.ordering), -1))
                .where(MessageModel.conversation_id == conversation_id)
            )
            next_ordering = result.scalar() + 1

            # Serialize parts if they are pydantic models
            serialized_parts = None
            if parts is not None:
                serialized_parts = []
                for part in parts:
                    if hasattr(part, "model_dump"):
                        serialized_parts.append(part.model_dump(mode="json"))
                    elif hasattr(part, "root") and hasattr(part.root, "model_dump"):
                        serialized_parts.append(part.root.model_dump(mode="json"))
                    elif isinstance(part, dict):
                        serialized_parts.append(part)
                    else:
                        serialized_parts.append({"kind": "text", "text": str(part)})

            msg = MessageModel(
                id=str(uuid.uuid4()),
                conversation_id=conversation_id,
                role=role,
                content=content,
                parts=serialized_parts,
                thinking_steps=thinking_steps,
                token_usage=token_usage,
                message_metadata=metadata,
                summary=summary,
                created_at=now,
                ordering=next_ordering,
            )
            session.add(msg)

        return self._msg_to_dict(msg)

    async def get_messages(self, conversation_id: str, limit: int = 100, offset: int = 0) -> list[dict]:
        await self._ensure_initialized()
        async with self.async_session_maker() as session:
            result = await session.execute(
                select(MessageModel)
                .where(MessageModel.conversation_id == conversation_id)
                .order_by(MessageModel.ordering.asc())
                .limit(limit)
                .offset(offset)
            )
            messages = result.scalars().all()
            return [self._msg_to_dict(msg) for msg in messages]

    # ── Helpers ──

    @staticmethod
    def _profile_to_dict(profile: ProfileModel) -> dict:
        return {
            "id": profile.id,
            "name": profile.name,
            "created_at": profile.created_at,
            "updated_at": profile.updated_at,
            "skill_mode": getattr(profile, "skill_mode", "manual") or "manual",
        }

    @staticmethod
    def _conv_to_dict(conv: ConversationModel) -> dict:
        return {
            "id": conv.id,
            "profile": conv.profile,
            "context_id": conv.context_id,
            "task_id": conv.task_id,
            "title": conv.title,
            "created_at": conv.created_at,
            "updated_at": conv.updated_at,
        }

    @staticmethod
    def _msg_to_dict(msg: MessageModel) -> dict:
        return {
            "id": msg.id,
            "conversation_id": msg.conversation_id,
            "role": msg.role,
            "content": msg.content,
            "parts": msg.parts,
            "thinking_steps": msg.thinking_steps,
            "token_usage": msg.token_usage,
            "metadata": msg.message_metadata,
            "summary": msg.summary,
            "created_at": msg.created_at,
            "ordering": msg.ordering,
        }

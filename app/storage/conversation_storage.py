import re
import time
import uuid
from pathlib import Path

from sqlalchemy import Table, delete, event, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import class_mapper

from a2a.server.models import Base

from app.storage.models import (
    AuthTokenModel, AutostartProcessModel, ChannelModel, ChannelSenderModel,
    ConversationModel, LLMConfigModel, MessageModel, ProfileModel,
    ProfileToolModel, ServerConfigModel, SkillEventSubscriptionModel,
    ToolConfigModel, ToolModel, UserConfigModel,
)
from app.utils.logger import logger


# Conversation id format for user-renamed ids. Server-allocated UUIDs already
# satisfy this (lowercase hex + hyphen, leading char alphanumeric). The regex
# is the single source of truth — frontend duplicates it for UX validation
# but the server is authoritative.
_CONVERSATION_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,127}$")


def is_valid_conversation_id(s: str) -> bool:
    """True iff `s` is a syntactically valid conversation id.

    Allowed: lowercase a-z, digits 0-9, `-`, `_`. Must start with a letter or
    digit (no leading separator). Length 1..128.
    """
    return isinstance(s, str) and bool(_CONVERSATION_ID_RE.match(s))


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
        # profiles → channels → conversations → (messages, channel_senders)
        # profiles → tools → (profile_tools, tool_configs, auth_tokens)
        async with self.engine.begin() as conn:
            tables_to_create = []
            for model_class in [
                ProfileModel,
                ChannelModel,
                ConversationModel, MessageModel,
                ChannelSenderModel,
                ServerConfigModel, LLMConfigModel, UserConfigModel,
                ToolModel,
                ProfileToolModel, ToolConfigModel, AuthTokenModel,
                AutostartProcessModel,
                SkillEventSubscriptionModel,
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

            # Channels feature: add channel_id FK to existing conversations
            # tables. SQLite enforces NOT NULL on ALTER TABLE only with a
            # default, but our default is row-dependent (resolved per-profile),
            # so the column is nullable in the schema and the storage layer
            # populates it. Backfill happens lazily by ``_ensure_main_channel``.
            try:
                await conn.execute(text(
                    "ALTER TABLE conversations ADD COLUMN channel_id VARCHAR(36) "
                    "REFERENCES channels(id) ON DELETE CASCADE"
                ))
            except Exception:  # noqa: BLE001
                pass

            # Mode rename: WhatsApp's old ``normal`` mode was renamed to
            # ``userbot`` for cross-platform consistency (Telegram, Discord,
            # Messenger, Slack now also declare a ``userbot`` mode). The
            # catalog no longer accepts ``normal``, so existing rows would
            # silently fall out of the validator on the next write. Idempotent
            # — the second run no-ops because no rows match.
            try:
                await conn.execute(text(
                    "UPDATE channels SET mode='userbot' "
                    "WHERE channel_type='whatsapp' AND mode='normal'"
                ))
            except Exception:  # noqa: BLE001
                pass

            # Drop the old single-subscription unique index on dev DBs that
            # were initialized before multi-subscription support landed.
            await conn.execute(text(
                "DROP INDEX IF EXISTS uq_skill_event_subs"
            ))

        # Backfill: ensure every existing profile has a main channel and that
        # every existing conversation points at it. Idempotent.
        await self._backfill_main_channels()

        self._initialized = True
        logger.info(f"ConversationStorage initialized with database: {self.db_path}")

    async def _backfill_main_channels(self) -> None:
        """Ensure each profile has a ``main`` channel and back-fill ``channel_id``.

        Called once at the end of ``initialize()``. Safe to run on a clean DB
        (it's a no-op when there are no profiles yet).
        """
        async with self.async_session_maker.begin() as session:
            profiles = (await session.execute(
                select(ProfileModel.name)
            )).scalars().all()
            for profile_name in profiles:
                main_id = await self._ensure_main_channel(session, profile_name)
                # Back-fill conversations missing channel_id for this profile.
                await session.execute(
                    update(ConversationModel)
                    .where(
                        ConversationModel.profile == profile_name,
                        ConversationModel.channel_id.is_(None),
                    )
                    .values(channel_id=main_id)
                )

    @staticmethod
    async def _ensure_main_channel(session: AsyncSession, profile: str) -> str:
        """Return the id of the ``main`` channel for ``profile``, creating it if needed.

        Must run inside a caller-provided session/transaction so the create is
        atomic with adjacent writes.
        """
        existing = (await session.execute(
            select(ChannelModel.id).where(
                ChannelModel.profile == profile,
                ChannelModel.channel_type == "main",
            )
        )).scalar_one_or_none()
        if existing:
            return existing
        now = time.time() * 1000
        ch = ChannelModel(
            id=str(uuid.uuid4()),
            profile=profile,
            channel_type="main",
            mode="bot",
            auth_mode="none",
            response_mode="normal",
            enabled=True,
            config=None,
            state=None,
            created_at=now,
            updated_at=now,
        )
        session.add(ch)
        await session.flush()
        return ch.id

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
            await session.flush()
            # Auto-create the profile's "main" channel in the same transaction
            # so every conversation can resolve a channel_id without a special
            # case for "freshly created profile, no channel yet".
            await self._ensure_main_channel(session, name)
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
        channel_id: str | None = None,
    ) -> dict:
        """Create a conversation. If ``channel_id`` is omitted, the profile's
        ``main`` channel is used (auto-created if missing)."""
        await self._ensure_initialized()
        now = time.time() * 1000
        async with self.async_session_maker.begin() as session:
            resolved_channel_id = channel_id or await self._ensure_main_channel(session, profile)
            conv = ConversationModel(
                id=str(uuid.uuid4()),
                profile=profile,
                channel_id=resolved_channel_id,
                context_id=context_id,
                task_id=task_id,
                title=title,
                created_at=now,
                updated_at=now,
            )
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

    async def list_conversations(
        self, profile: str, limit: int = 50, offset: int = 0,
        channel_id: str | None = None, channel_type: str | None = None,
    ) -> list[dict]:
        """List conversations for a profile, optionally filtered by channel.

        If both ``channel_id`` and ``channel_type`` are provided, ``channel_id``
        wins. ``channel_type`` is resolved against the profile's channels;
        an unknown type returns an empty list.
        """
        await self._ensure_initialized()
        async with self.async_session_maker() as session:
            resolved_channel_id = channel_id
            if resolved_channel_id is None and channel_type is not None:
                resolved_channel_id = (await session.execute(
                    select(ChannelModel.id).where(
                        ChannelModel.profile == profile,
                        ChannelModel.channel_type == channel_type,
                    )
                )).scalar_one_or_none()
                if resolved_channel_id is None:
                    return []

            msg_count_subq = (
                select(
                    MessageModel.conversation_id,
                    func.count(MessageModel.id).label("message_count"),
                )
                .group_by(MessageModel.conversation_id)
                .subquery()
            )
            stmt = (
                select(ConversationModel, msg_count_subq.c.message_count)
                .outerjoin(msg_count_subq, ConversationModel.id == msg_count_subq.c.conversation_id)
                .where(ConversationModel.profile == profile)
            )
            if resolved_channel_id is not None:
                stmt = stmt.where(ConversationModel.channel_id == resolved_channel_id)
            stmt = stmt.order_by(ConversationModel.updated_at.desc()).limit(limit).offset(offset)

            result = await session.execute(stmt)
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

    async def conversation_id_exists(self, conversation_id: str) -> bool:
        await self._ensure_initialized()
        async with self.async_session_maker() as session:
            result = await session.execute(
                select(func.count())
                .select_from(ConversationModel)
                .where(ConversationModel.id == conversation_id)
            )
            return (result.scalar() or 0) > 0

    async def rename_conversation_id(
        self, old_id: str, new_id: str, *, new_title: str | None = None,
    ) -> dict | None:
        """Rename a conversation's primary id, cascading to FK children.

        Atomic across `messages.conversation_id` and
        `skill_event_subscriptions.conversation_id` via SQLite's
        ``PRAGMA defer_foreign_keys=ON`` (FKs are re-checked at COMMIT, so the
        parent row can be updated before all children).

        Returns the refreshed conversation dict on success, or ``None`` if
        ``old_id`` does not exist or ``new_id`` is already in use.

        ``new_title`` overrides the default behavior of resetting the title
        to ``new_id``. Pass an explicit string to keep a custom title; pass
        ``None`` to let the title follow the new id.
        """
        await self._ensure_initialized()

        if not is_valid_conversation_id(new_id):
            return None
        if old_id == new_id:
            return await self.get_conversation(old_id)

        async with self.async_session_maker.begin() as session:
            # Pre-flight existence checks share the transaction, so a
            # concurrent rename can't slip in between check and update.
            old_row = (await session.execute(
                select(ConversationModel).where(ConversationModel.id == old_id)
            )).scalar_one_or_none()
            if old_row is None:
                return None
            collision = (await session.execute(
                select(func.count())
                .select_from(ConversationModel)
                .where(ConversationModel.id == new_id)
            )).scalar() or 0
            if collision > 0:
                return None

            # Defer FK checks until COMMIT so we can update the parent row
            # while children still point at the old id. Pragma resets at
            # COMMIT — scoped to this connection/transaction.
            await session.execute(text("PRAGMA defer_foreign_keys=ON"))

            await session.execute(
                update(MessageModel)
                .where(MessageModel.conversation_id == old_id)
                .values(conversation_id=new_id)
            )
            await session.execute(
                update(SkillEventSubscriptionModel)
                .where(SkillEventSubscriptionModel.conversation_id == old_id)
                .values(conversation_id=new_id)
            )

            now = time.time() * 1000
            values: dict = {
                "id": new_id,
                "title": new_title if new_title is not None else new_id,
                "updated_at": now,
            }
            # context_id is back-filled to the conversation id at first stream
            # tick (see app/agent/stream_runner.py). Rewrite if it currently
            # equals the old id so tool-storage scoping follows the rename.
            if old_row.context_id == old_id:
                values["context_id"] = new_id

            await session.execute(
                update(ConversationModel)
                .where(ConversationModel.id == old_id)
                .values(**values)
            )

        return await self.get_conversation(new_id)

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

    # ── Channel CRUD ──

    async def list_channels(self, profile: str) -> list[dict]:
        await self._ensure_initialized()
        async with self.async_session_maker() as session:
            result = await session.execute(
                select(ChannelModel)
                .where(ChannelModel.profile == profile)
                .order_by(ChannelModel.created_at.asc())
            )
            return [self._channel_to_dict(c) for c in result.scalars().all()]

    async def get_channel(self, channel_id: str) -> dict | None:
        await self._ensure_initialized()
        async with self.async_session_maker() as session:
            result = await session.execute(
                select(ChannelModel).where(ChannelModel.id == channel_id)
            )
            ch = result.scalar_one_or_none()
            return self._channel_to_dict(ch) if ch else None

    async def get_channel_by_type(self, profile: str, channel_type: str) -> dict | None:
        await self._ensure_initialized()
        async with self.async_session_maker() as session:
            result = await session.execute(
                select(ChannelModel).where(
                    ChannelModel.profile == profile,
                    ChannelModel.channel_type == channel_type,
                )
            )
            ch = result.scalar_one_or_none()
            return self._channel_to_dict(ch) if ch else None

    async def create_channel(
        self, profile: str, channel_type: str, *,
        mode: str = "bot", auth_mode: str = "none",
        response_mode: str = "normal", enabled: bool = True,
        config: dict | None = None,
    ) -> dict:
        """Create a channel row. Raises if (profile, channel_type) already exists."""
        await self._ensure_initialized()
        now = time.time() * 1000
        ch = ChannelModel(
            id=str(uuid.uuid4()),
            profile=profile,
            channel_type=channel_type,
            mode=mode,
            auth_mode=auth_mode,
            response_mode=response_mode,
            enabled=enabled,
            config=config,
            state=None,
            created_at=now,
            updated_at=now,
        )
        async with self.async_session_maker.begin() as session:
            session.add(ch)
        return self._channel_to_dict(ch)

    async def update_channel(self, channel_id: str, **fields) -> dict | None:
        await self._ensure_initialized()
        if not fields:
            return await self.get_channel(channel_id)
        fields["updated_at"] = time.time() * 1000
        async with self.async_session_maker.begin() as session:
            await session.execute(
                update(ChannelModel)
                .where(ChannelModel.id == channel_id)
                .values(**fields)
            )
        return await self.get_channel(channel_id)

    async def delete_channel(self, channel_id: str) -> bool:
        """Delete a channel. ``main`` is rejected upstream (in the API layer)."""
        await self._ensure_initialized()
        async with self.async_session_maker.begin() as session:
            result = await session.execute(
                delete(ChannelModel).where(ChannelModel.id == channel_id)
            )
            return result.rowcount > 0

    async def list_enabled_external_channels(self) -> list[dict]:
        """Channels to start at server boot — enabled and not ``main``."""
        await self._ensure_initialized()
        async with self.async_session_maker() as session:
            result = await session.execute(
                select(ChannelModel).where(
                    ChannelModel.enabled.is_(True),
                    ChannelModel.channel_type != "main",
                )
            )
            return [self._channel_to_dict(c) for c in result.scalars().all()]

    # ── Channel sender CRUD ──

    async def get_or_create_sender(
        self, channel_id: str, sender_id: str, display_name: str | None = None,
    ) -> dict:
        """Return the sender row (creating it if absent). Refreshes display_name."""
        await self._ensure_initialized()
        now = time.time() * 1000
        async with self.async_session_maker.begin() as session:
            row = (await session.execute(
                select(ChannelSenderModel).where(
                    ChannelSenderModel.channel_id == channel_id,
                    ChannelSenderModel.sender_id == sender_id,
                )
            )).scalar_one_or_none()
            if row is None:
                row = ChannelSenderModel(
                    id=str(uuid.uuid4()),
                    channel_id=channel_id,
                    sender_id=sender_id,
                    display_name=display_name,
                    authenticated=False,
                    pending_otp=None,
                    pending_otp_expires_at=None,
                    conversation_id=None,
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
                await session.flush()
            elif display_name and row.display_name != display_name:
                row.display_name = display_name
                row.updated_at = now
            return self._sender_to_dict(row)

    async def list_senders(self, channel_id: str) -> list[dict]:
        await self._ensure_initialized()
        async with self.async_session_maker() as session:
            result = await session.execute(
                select(ChannelSenderModel)
                .where(ChannelSenderModel.channel_id == channel_id)
                .order_by(ChannelSenderModel.created_at.asc())
            )
            return [self._sender_to_dict(s) for s in result.scalars().all()]

    async def update_sender(self, sender_row_id: str, **fields) -> dict | None:
        await self._ensure_initialized()
        if not fields:
            return None
        fields["updated_at"] = time.time() * 1000
        async with self.async_session_maker.begin() as session:
            await session.execute(
                update(ChannelSenderModel)
                .where(ChannelSenderModel.id == sender_row_id)
                .values(**fields)
            )
            row = (await session.execute(
                select(ChannelSenderModel).where(ChannelSenderModel.id == sender_row_id)
            )).scalar_one_or_none()
            return self._sender_to_dict(row) if row else None

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
            "channel_id": conv.channel_id,
            "context_id": conv.context_id,
            "task_id": conv.task_id,
            "title": conv.title,
            "created_at": conv.created_at,
            "updated_at": conv.updated_at,
        }

    @staticmethod
    def _channel_to_dict(ch: ChannelModel) -> dict:
        return {
            "id": ch.id,
            "profile": ch.profile,
            "channel_type": ch.channel_type,
            "mode": ch.mode,
            "auth_mode": ch.auth_mode,
            "response_mode": ch.response_mode,
            "enabled": bool(ch.enabled),
            "config": ch.config or {},
            "state": ch.state or {},
            "created_at": ch.created_at,
            "updated_at": ch.updated_at,
        }

    @staticmethod
    def _sender_to_dict(s: ChannelSenderModel) -> dict:
        return {
            "id": s.id,
            "channel_id": s.channel_id,
            "sender_id": s.sender_id,
            "display_name": s.display_name,
            "authenticated": bool(s.authenticated),
            "pending_otp": s.pending_otp,
            "pending_otp_expires_at": s.pending_otp_expires_at,
            "conversation_id": s.conversation_id,
            "created_at": s.created_at,
            "updated_at": s.updated_at,
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

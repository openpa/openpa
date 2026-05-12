"""Process-wide runtime state shared between :mod:`app.server` and handlers.

The state distinguishes between **deferred-storage mode** (no ``bootstrap.toml``
yet — the Setup Wizard has not run) and **fully-booted mode** (storage
initialised, tools registered, agent built). Handlers that need post-boot
objects resolve them through :func:`get_state` so the wizard's
``POST /api/config/setup`` can trigger the deferred boot sequence and have
subsequent requests pick up the now-real storage and agent objects without a
server restart.
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Optional


class BootedState:
    """Mutable container for runtime references.

    Storage-dependent attributes are ``None`` until the deferred boot phase
    completes. ``storage_ready`` is the canonical flag — handlers must guard
    storage access on it.
    """

    storage_ready: bool = False
    config_storage: Any = None
    conversation_storage: Any = None
    registry: Any = None
    config_manager: Any = None
    model_group_mgr: Any = None
    openpa_agent: Any = None
    agent_executor: Any = None
    document_service: Any = None
    embedding: Any = None
    vector_store: Any = None
    on_first_setup: Optional[Callable[[str], Awaitable[None]]] = None
    boot_lock: Optional[asyncio.Lock] = None
    # Async callable that runs the deferred boot sequence and registers the
    # full API route set onto the live Starlette app. Set by ``main()``.
    boot_fn: Optional[Callable[[], Awaitable[None]]] = None

    def reset_storage(self) -> None:
        """Clear post-storage references so a fresh boot can repopulate."""
        self.storage_ready = False
        self.config_storage = None
        self.conversation_storage = None
        self.registry = None
        self.config_manager = None
        self.model_group_mgr = None
        self.openpa_agent = None
        self.agent_executor = None
        self.document_service = None
        self.embedding = None
        self.vector_store = None
        self.on_first_setup = None


_state = BootedState()


def get_state() -> BootedState:
    return _state

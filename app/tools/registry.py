"""Central registry for all tools (intrinsic / built-in / a2a / mcp / skill).

The registry is the single source of truth for what tools exist and what
their tool_ids are. It owns:

- An in-memory ``tool_id -> Tool`` map (used by the reasoning agent dispatch).
- The persisted ``tools`` table for the four registered types
  (intrinsic tools are not persisted -- they're discovered from code at every
  startup).
- The per-profile ``profile_tools`` rows (a2a/mcp visibility).
- Skill synchronisation: at startup and at hot-reload, ``sync_skills`` diffs
  the current on-disk skills against the persisted rows and adds/updates/
  removes as appropriate -- guarded by an ``asyncio.Lock`` so reloads can't
  interleave with the reasoning loop.

Profile-availability rules:
- intrinsic / builtin / skill : always available to every profile (no
  ``profile_tools`` row).
- a2a / mcp : visible to every profile; enabled only where
  ``profile_tools.enabled = 1``.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Dict, Iterable, List, Optional

from app.storage.tool_storage import ToolStorage
from app.tools.base import Tool, ToolType
from app.tools.config_manager import ToolConfigManager
from app.tools.ids import (
    ToolIdConflictError,
    allocate_fixed_tool_id,
    allocate_unique_tool_id,
    slugify,
)
from app.utils.logger import logger


class ToolRegistry:
    """Single source of truth for tools.

    Lifecycle
    ---------
    1. Built at server startup with a :class:`ToolStorage` and
       :class:`ToolConfigManager`.
    2. Intrinsic / built-in / skill tools are registered during startup.
    3. A2A / MCP / Skill registrations during runtime mutate the in-memory map
       *and* the database (atomically inside the registry's lock).
    """

    def __init__(self, tool_storage: ToolStorage, config_manager: ToolConfigManager):
        self._storage = tool_storage
        self._config = config_manager
        # Strong refs to live Tool instances.
        self._tools: Dict[str, Tool] = {}
        # Async lock guarding all multi-step mutations (sync_skills, register_*).
        self._lock = asyncio.Lock()
        # Optional callback fired after add/remove/sync mutations -- used by
        # the OpenPAAgent to refresh its embedding table.
        self._on_change: Optional[Callable[[], None]] = None

    # ── observability ──────────────────────────────────────────────────

    def set_change_callback(self, cb: Optional[Callable[[], None]]) -> None:
        self._on_change = cb

    def _fire_change(self) -> None:
        if self._on_change:
            try:
                self._on_change()
            except Exception:  # noqa: BLE001
                logger.exception("ToolRegistry change callback failed")

    @property
    def storage(self) -> ToolStorage:
        return self._storage

    @property
    def config(self) -> ToolConfigManager:
        return self._config

    # ── lookup ─────────────────────────────────────────────────────────

    def get(self, tool_id: str) -> Optional[Tool]:
        return self._tools.get(tool_id)

    def all_tools(self) -> List[Tool]:
        return list(self._tools.values())

    @staticmethod
    def _default_enabled(tool_type: ToolType) -> bool:
        """Default enabled state when no ``profile_tools`` row exists.

        - intrinsic       : always on (never persisted)
        - builtin / skill : on by default (always available, opt-out per profile)
        - a2a / mcp       : off by default (opt-in per profile, except for the
                            owner profile which gets enabled=1 at registration)
        """
        return tool_type in (ToolType.INTRINSIC, ToolType.BUILTIN, ToolType.SKILL)

    def is_enabled_for_profile(self, tool: Tool, profile: str) -> bool:
        """Resolve a tool's enabled state for ``profile``.

        Looks at ``profile_tools`` for an explicit value; falls back to the
        per-type default.
        """
        explicit = self._storage.get_profile_tool_enabled(profile, tool.tool_id)
        if explicit is not None:
            return explicit
        return self._default_enabled(tool.tool_type)

    def tools_for_profile(self, profile: str) -> List[Tool]:
        """Return tools the reasoning agent should expose to ``profile``.

        Intrinsic tools are always included. Every other type honors a
        per-profile ``profile_tools`` row when present, falling back to the
        type's default (built-in / skill = on; a2a / mcp = off).
        """
        enabled_per_profile = self._storage.list_profile_tools(profile)
        out: List[Tool] = []
        for tool in self._tools.values():
            if tool.tool_type is ToolType.INTRINSIC:
                out.append(tool)
                continue
            explicit = enabled_per_profile.get(tool.tool_id)
            enabled = explicit if explicit is not None else self._default_enabled(tool.tool_type)
            if enabled:
                out.append(tool)
        return out

    def visible_for_profile(self, profile: str) -> List[dict]:
        """Return UI rows for ``profile`` -- includes disabled stubs.

        Used by ``GET /api/tools``. Hidden tools (intrinsic) are excluded.
        """
        enabled_per_profile = self._storage.list_profile_tools(profile)
        rows: List[dict] = []
        for tool in self._tools.values():
            if tool.hidden:
                continue
            explicit = enabled_per_profile.get(tool.tool_id)
            enabled = explicit if explicit is not None else self._default_enabled(tool.tool_type)
            rows.append({
                "tool_id": tool.tool_id,
                "name": tool.name,
                "description": tool.description,
                "tool_type": tool.tool_type.value,
                "enabled": enabled,
                "arguments_schema": tool.arguments_schema,
            })
        return rows

    # ── id allocation ──────────────────────────────────────────────────

    def _taken(self) -> set[str]:
        return set(self._tools.keys())

    # ── intrinsic registration (in-memory only) ────────────────────────

    def register_intrinsic(self, tool: Tool) -> str:
        if tool.tool_type is not ToolType.INTRINSIC:
            raise ValueError("register_intrinsic requires a Tool with tool_type=INTRINSIC")
        tool_id = allocate_fixed_tool_id(tool.name, self._taken())
        tool.tool_id = tool_id
        self._tools[tool_id] = tool
        logger.info(f"Registered intrinsic tool '{tool.name}' (tool_id={tool_id})")
        return tool_id

    # ── built-in registration (persisted, no profile_tools row) ────────

    def register_builtin(self, tool: Tool, *, source: str | None = None) -> str:
        if tool.tool_type is not ToolType.BUILTIN:
            raise ValueError("register_builtin requires a Tool with tool_type=BUILTIN")
        tool_id = allocate_fixed_tool_id(tool.name, self._taken())
        # Built-in tools have FIXED slugs and take precedence over any
        # previously-persisted dynamic tool (skill / a2a / mcp) that happens
        # to claim the same slug. Atomically rename the displaced row so its
        # per-profile state (profile_tools + tool_configs) survives via
        # ON UPDATE CASCADE.
        self._displace_persisted_collision(tool_id, claimant_name=tool.name)
        tool.tool_id = tool_id
        self._tools[tool_id] = tool
        self._storage.upsert_tool(
            tool_id=tool_id,
            name=tool.name,
            tool_type=ToolType.BUILTIN.value,
            source=source or tool_id,
            description=tool.description,
            arguments_schema=tool.arguments_schema,
        )
        logger.info(f"Registered built-in tool '{tool.name}' (tool_id={tool_id})")
        return tool_id

    def _displace_persisted_collision(self, tool_id: str, *, claimant_name: str) -> None:
        """If ``tool_id`` is held by a non-fixed (dynamic) tool in the DB,
        rename that row so the fixed tool can claim the slug.

        - intrinsic / builtin (fixed)  : never renamed (would have been caught
          earlier by ``allocate_fixed_tool_id``, which raises on collision).
        - skill / a2a / mcp (dynamic)  : renamed with a ``_2`` / ``_3`` suffix.
          Per-profile rows follow via ``ON UPDATE CASCADE``.
        """
        existing = self._storage.get_tool(tool_id)
        if existing is None:
            return
        existing_type = existing["tool_type"]
        if existing_type == ToolType.BUILTIN.value:
            # Same fixed type re-registered (idempotent startup) -- nothing to do.
            return
        # Dynamic tool currently holds the slug; rename it.
        displaced_name = existing["name"]
        new_id = allocate_unique_tool_id(displaced_name, self._taken() | {tool_id})
        if not self._storage.rename_tool(tool_id, new_id):
            # Should not happen -- if rename fails, fall back to delete so the
            # built-in can still register. A skill row has no per-profile state
            # so this is safe; for a2a/mcp we'd lose state, which is logged.
            logger.warning(
                f"Could not rename displaced {existing_type} tool '{displaced_name}' "
                f"({tool_id} -> {new_id}). Deleting row instead; per-profile "
                f"state for that tool will be lost."
            )
            self._storage.delete_tool(tool_id)
            return
        # Mirror the rename in any in-memory entry that may have been hydrated
        # earlier in the startup sequence.
        old_in_memory = self._tools.pop(tool_id, None)
        if old_in_memory is not None:
            old_in_memory.tool_id = new_id
            self._tools[new_id] = old_in_memory
        logger.warning(
            f"Fixed-name tool '{claimant_name}' (tool_id='{tool_id}') displaced "
            f"persisted {existing_type} tool '{displaced_name}'. "
            f"Renamed displaced tool to '{new_id}' (per-profile state preserved)."
        )

    # ── a2a / mcp registration (persisted, profile_tools backfilled) ──

    async def register_a2a(self, tool: Tool, *, source: str, owner_profile: str | None) -> str:
        if tool.tool_type is not ToolType.A2A:
            raise ValueError("register_a2a requires a Tool with tool_type=A2A")
        async with self._lock:
            tool_id = self._register_dynamic(
                tool, source=source, owner_profile=owner_profile,
                tool_type_value=ToolType.A2A.value,
            )
        self._fire_change()
        return tool_id

    async def register_mcp(
        self, tool: Tool, *, source: str, owner_profile: str | None, extra: dict | None = None,
    ) -> str:
        if tool.tool_type is not ToolType.MCP:
            raise ValueError("register_mcp requires a Tool with tool_type=MCP")
        async with self._lock:
            tool_id = self._register_dynamic(
                tool, source=source, owner_profile=owner_profile,
                tool_type_value=ToolType.MCP.value, extra=extra,
            )
        self._fire_change()
        return tool_id

    def _register_dynamic(
        self,
        tool: Tool,
        *,
        source: str,
        owner_profile: str | None,
        tool_type_value: str,
        extra: dict | None = None,
    ) -> str:
        # Reuse existing tool_id if this (type, source) was registered before
        existing = self._storage.find_tool_by_source(tool_type_value, source)
        if existing:
            tool_id = existing["tool_id"]
        else:
            tool_id = allocate_unique_tool_id(tool.name, self._taken())
        tool.tool_id = tool_id
        self._tools[tool_id] = tool
        self._storage.upsert_tool(
            tool_id=tool_id,
            name=tool.name,
            tool_type=tool_type_value,
            source=source,
            description=tool.description,
            arguments_schema=tool.arguments_schema,
            extra=extra,
            owner_profile=owner_profile,
        )
        # Ensure every profile has a row: owner enabled, others disabled
        self._storage.backfill_profile_tools_for_new_tool(tool_id, owner_profile)
        logger.info(
            f"Registered {tool_type_value} tool '{tool.name}' "
            f"(tool_id={tool_id}, owner={owner_profile})"
        )
        return tool_id

    # ── skills (persisted, no profile_tools row -- always-available) ──

    def register_skill_sync(self, tool: Tool, *, source: str) -> str:
        """Synchronous skill registration helper used from inside ``sync_skills``.

        Tool-id selection follows three rules, in order:

        1. If this skill's ``source`` is already persisted AND its existing
           ``tool_id`` is still free (or owned only by this same skill),
           reuse it -- preserves identity across restarts.
        2. If the persisted ``tool_id`` is now owned by a fixed-name tool
           (intrinsic / built-in) the skill is *renamed*: a fresh unique
           suffix is allocated and the persisted row's id is rewritten.
        3. If the source is brand new, allocate a unique slug from
           ``tool.name`` (suffixed if needed).
        """
        if tool.tool_type is not ToolType.SKILL:
            raise ValueError("register_skill_sync requires a Tool with tool_type=SKILL")

        existing = self._storage.find_tool_by_source(ToolType.SKILL.value, source)
        chosen_id: str
        if existing:
            candidate = existing["tool_id"]
            owner = self._tools.get(candidate)
            owner_is_other_fixed = (
                owner is not None
                and owner is not tool
                and owner.tool_type in (ToolType.INTRINSIC, ToolType.BUILTIN)
            )
            if owner_is_other_fixed:
                # Skill's previous slug is now claimed by a fixed-name tool.
                # Re-allocate this skill's id and migrate the DB row.
                fresh = allocate_unique_tool_id(tool.name, self._taken() | {candidate})
                if not self._storage.rename_tool(candidate, fresh):
                    # Extremely unlikely (concurrent rename), fall back to insert
                    self._storage.delete_tool(candidate)
                logger.warning(
                    f"Skill '{tool.name}' previously held tool_id='{candidate}', "
                    f"which is now owned by a fixed-name tool. "
                    f"Renamed skill to '{fresh}'."
                )
                chosen_id = fresh
            else:
                chosen_id = candidate
        else:
            chosen_id = allocate_unique_tool_id(tool.name, self._taken())

        tool.tool_id = chosen_id
        self._tools[chosen_id] = tool
        self._storage.upsert_tool(
            tool_id=chosen_id,
            name=tool.name,
            tool_type=ToolType.SKILL.value,
            source=source,
            description=tool.description,
            arguments_schema=tool.arguments_schema,
        )
        logger.info(f"Registered skill '{tool.name}' (tool_id={chosen_id})")
        return chosen_id

    # ── unregister ─────────────────────────────────────────────────────

    async def unregister(self, tool_id: str) -> bool:
        async with self._lock:
            existed = self._tools.pop(tool_id, None) is not None
            self._storage.delete_tool(tool_id)
        if existed:
            self._fire_change()
        return existed

    # ── skill sync ─────────────────────────────────────────────────────

    async def sync_skills(
        self,
        current: Dict[str, Any],
        skill_factory: Callable[[Any], Tool],
    ) -> None:
        """Diff persisted skills against the on-disk set and reconcile.

        Args
        ----
        current : ``{name: SkillInfo}`` from a fresh disk scan.
        skill_factory : callable that turns a ``SkillInfo`` into a ``Tool``.

        For each entry:
        - existing source (dir_path) → reuse tool_id, refresh name/description.
        - new source                  → allocate fresh tool_id, register.
        - source vanished from disk   → remove tool (cascades configs).
        """
        async with self._lock:
            persisted = self._storage.list_tools(tool_type=ToolType.SKILL.value)
            persisted_by_source = {row["source"]: row for row in persisted}

            current_sources = {str(info.dir_path) for info in current.values()}

            # Remove vanished skills
            for source, row in persisted_by_source.items():
                if source in current_sources:
                    continue
                tool_id = row["tool_id"]
                self._tools.pop(tool_id, None)
                self._storage.delete_tool(tool_id)
                logger.info(f"Removed skill (no longer on disk): tool_id={tool_id}")

            # Drop in-memory skill entries whose source no longer matches any current skill
            stale_in_memory = [
                t.tool_id for t in list(self._tools.values())
                if t.tool_type is ToolType.SKILL
                and getattr(t, "_source", None) not in current_sources
            ]
            for tid in stale_in_memory:
                self._tools.pop(tid, None)

            # Add / update current skills
            for info in current.values():
                source = str(info.dir_path)
                tool = skill_factory(info)
                self.register_skill_sync(tool, source=source)

        self._fire_change()

    # ── profile lifecycle hooks ────────────────────────────────────────

    def on_profile_created(self, profile: str) -> int:
        """Backfill ``profile_tools`` rows for the new profile (a2a/mcp only).

        Returns the number of rows inserted.
        """
        return self._storage.backfill_profile_tools_for_new_profile(profile)

    def set_profile_tool_enabled(
        self, profile: str, tool_id: str, enabled: bool,
    ) -> None:
        """Set the per-profile enabled flag for a tool.

        Refuses intrinsic tools (they're hidden and always-on by definition).
        Every other type accepts the toggle; the row in ``profile_tools``
        overrides the per-type default returned by :meth:`_default_enabled`.
        """
        tool = self._tools.get(tool_id)
        if tool is None:
            raise KeyError(f"Tool '{tool_id}' is not registered")
        if tool.tool_type is ToolType.INTRINSIC:
            raise ValueError(
                f"Tool '{tool_id}' is intrinsic and cannot be disabled."
            )
        self._storage.set_profile_tool(profile, tool_id, enabled)


# ── singleton ──────────────────────────────────────────────────────────────

_instance: ToolRegistry | None = None


def get_tool_registry() -> ToolRegistry:
    if _instance is None:
        raise RuntimeError(
            "ToolRegistry has not been initialized yet. Call set_tool_registry() first."
        )
    return _instance


def set_tool_registry(registry: ToolRegistry) -> None:
    global _instance
    _instance = registry

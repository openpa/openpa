"""System variables registry.

Single source of truth for the env-var block injected into subprocesses
spawned by built-in tools (currently only ``exec_shell``).

Each entry in :data:`SYSTEM_VARS` pairs a canonical env-var name with a
resolver callable. Resolvers receive the active profile (may be ``None``)
and return the value as a string, or ``None`` to omit the variable from
the spawned env. Values are computed lazily on every call to
:func:`build_system_env` so that runtime changes to the underlying
config (port, working dir, profile token) are picked up immediately.

To add a new variable: append one :class:`SystemVarSpec` to
:data:`SYSTEM_VARS`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable, Dict, Optional

from app.config.settings import BaseConfig, get_user_working_directory
from app.utils.logger import logger


def _load_opa_token(profile: Optional[str]) -> Optional[str]:
    """Read the per-profile OPENPA_TOKEN from ``<OPENPA_WORKING_DIR>/tokens/<profile>.token``.

    Returns the stripped token string, or ``None`` if the profile is unset,
    the file is missing, or it cannot be read. Failure is non-fatal —
    callers should simply omit OPENPA_TOKEN from the spawned env.
    """
    if not profile:
        return None
    token_path = os.path.join(BaseConfig.OPENPA_WORKING_DIR, "tokens", f"{profile}.token")
    try:
        with open(token_path, "r", encoding="utf-8") as f:
            token = f.read().strip()
        return token or None
    except FileNotFoundError:
        logger.warning(f"OPA token file missing for profile '{profile}': {token_path}")
        return None
    except OSError as e:
        logger.warning(f"Could not read OPA token for profile '{profile}' ({token_path}): {e}")
        return None


def _resolve_skill_dir(profile: Optional[str]) -> Optional[str]:
    if not profile:
        return None
    # Lazy import: app.skills.sync pulls in the watcher / tool registry chain.
    from app.skills.sync import profile_skills_dir
    return str(profile_skills_dir(profile))


@dataclass(frozen=True)
class SystemVarSpec:
    name: str
    resolve: Callable[[Optional[str]], Optional[str]]
    description: str = ""


SYSTEM_VARS: list[SystemVarSpec] = [
    SystemVarSpec(
        name="OPENPA_SYSTEM_WORKING_DIR",
        resolve=lambda _profile: BaseConfig.OPENPA_WORKING_DIR,
        description="OpenPA internal working directory (~/.openpa).",
    ),
    SystemVarSpec(
        name="OPENPA_USER_WORKING_DIR",
        resolve=lambda _profile: get_user_working_directory(),
        description="User-facing default working directory.",
    ),
    SystemVarSpec(
        name="OPENPA_SKILL_DIR",
        resolve=_resolve_skill_dir,
        description="Per-profile skills directory; omitted when no profile.",
    ),
    SystemVarSpec(
        name="OPENPA_SERVER",
        resolve=lambda _profile: f"http://127.0.0.1:{BaseConfig.PORT}",
        description="Loopback URL of this server for the `opa` CLI.",
    ),
    SystemVarSpec(
        name="OPENPA_TOKEN",
        resolve=_load_opa_token,
        description="Per-profile OPA token; omitted when missing.",
    ),
]


def build_system_env(profile: Optional[str]) -> Dict[str, str]:
    """Resolve every entry in :data:`SYSTEM_VARS` for ``profile``.

    Returns a dict suitable for merging into a subprocess env. Variables
    whose resolver returns ``None`` are omitted (matches the historical
    "skip OPENPA_TOKEN when missing" behavior).
    """
    out: Dict[str, str] = {}
    for spec in SYSTEM_VARS:
        value = spec.resolve(profile)
        if value is None:
            continue
        out[spec.name] = str(value)
    return out

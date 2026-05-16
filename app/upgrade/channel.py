"""Release-channel helpers.

OpenPA ships three release channels: ``production`` (PyPI + GitHub
``/releases/latest``), ``test`` (Test PyPI + GitHub prereleases tagged
``v*-testN``), and ``dev`` (editable install rooted at the bind-mounted
source checkout — Docker dev mode and ``uv sync`` native dev mode). The
channel is decided at install time and written to ``~/.openpa/.env`` as
``OPENPA_UPGRADE_CHANNEL``; switching channels is a deliberate reinstall,
not a runtime toggle.

Dev exists so the feature installer can tell "we have local source"
apart from "we have a PyPI install" — pinning ``openpa==X.Y.Z`` for a
not-yet-published version only works by accident in dev (the installed
editable happens to match the pinned version), so :func:`pip_spec` drops
the pin in dev to make that explicit.

This module is the single owner of:

  - reading the channel from the environment;
  - the tag-format regex that distinguishes test prereleases;
  - the translation between the GitHub tag (``v0.1.5-test3``) and the
    PEP 440 dev version pip uses (``0.1.5.dev3``); release-test.yml
    applies the same rewrite when building, so we round-trip cleanly.
  - PEP 440 dev-version ordering, since stdlib has no parser for it
    and we don't want to take a runtime dep on ``packaging`` just for
    the upgrader.

Keeping these here means manifest.py and runner.py don't grow tag /
version-format knowledge.
"""

from __future__ import annotations

import os
import re
from typing import Literal


Channel = Literal["production", "test", "dev"]


def get_channel() -> Channel:
    """Return the active release channel.

    Defaults to ``production`` when the env var is unset, so an
    existing prod install that hasn't yet had its ``.env`` rewritten
    keeps querying ``releases/latest`` exactly as before.
    """
    value = os.environ.get("OPENPA_UPGRADE_CHANNEL", "").strip().lower()
    if value == "test":
        return "test"
    if value == "dev":
        return "dev"
    return "production"


_TEST_TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)-test(\d+)$")


def is_test_tag(tag: str) -> bool:
    """``True`` iff ``tag`` matches the ``vMAJOR.MINOR.PATCH-testN`` format."""
    return _TEST_TAG.match(tag or "") is not None


def tag_to_pep440(tag: str) -> str:
    """Convert a test tag (``v0.1.5-test3``) to its PEP 440 form (``0.1.5.dev3``).

    Mirrors the rewrite ``release-test.yml`` performs in CI. Raises
    ``ValueError`` for tags that don't match the test-tag format —
    callers should gate with :func:`is_test_tag` first.
    """
    m = _TEST_TAG.match(tag or "")
    if not m:
        raise ValueError(f"Not a test tag: {tag!r}")
    base = f"{m.group(1)}.{m.group(2)}.{m.group(3)}"
    return f"{base}.dev{int(m.group(4))}"


_PEP440 = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:\.dev(\d+))?(?:\+([\w.]+))?$")


def parse_pep440(version: str) -> tuple[int, int, int, int, int, str]:
    """Parse ``MAJOR.MINOR.PATCH[.devN][+LOCAL]`` into a sortable tuple.

    Slots: ``(major, minor, patch, is_final, dev_counter, local)``.
    ``is_final`` is ``0`` for ``.devN`` releases and ``1`` for finals so
    that ``0.1.5.devN < 0.1.5`` regardless of N. ``dev_counter`` orders
    multiple devs against each other.

    ``local`` is the optional PEP 440 *local version* suffix (the part
    after ``+``). It's compared lexically as a string — empty sorts
    lowest, so a version with a local segment is always considered
    strictly newer than the same version without one. We use this for
    the dev-channel forced-available synth: ``X.Y.Z[.devN]+devforced``
    is reliably newer than the working-copy install at ``X.Y.Z[.devN]``.
    """
    m = _PEP440.match(version or "")
    if not m:
        raise ValueError(f"Not a PEP 440 version we recognize: {version!r}")
    major, minor, patch, dev, local = m.groups()
    local_str = local or ""
    if dev is None:
        return (int(major), int(minor), int(patch), 1, 0, local_str)
    return (int(major), int(minor), int(patch), 0, int(dev), local_str)


def is_newer(latest: str, current: str) -> bool:
    """``True`` iff ``latest`` is strictly newer than ``current`` under PEP 440."""
    return parse_pep440(latest) > parse_pep440(current)


__all__ = [
    "Channel",
    "get_channel",
    "is_newer",
    "is_test_tag",
    "parse_pep440",
    "tag_to_pep440",
]

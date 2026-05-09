"""Release-manifest lookup for the upgrader.

We treat the GitHub Releases API as the source of truth: each release
ships with a tag (the SemVer) and an optional ``release.json`` asset
that pins the compatibility matrix. Reading the tag is enough for the
common case; the asset adds defense-in-depth for releases that bump
``min_supported_upgrade_from``.

Why GitHub and not a self-hosted feed: it's free, signs artifacts via
Actions, integrates with electron-updater for the UI side, and gives
users a public changelog out of the box. Self-hosted would be a strict
upgrade only if and when the team needs staged rollouts.

Network failures are non-fatal here — callers display "couldn't check
for updates" and move on. We never fail the running server because the
internet is flaky.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from app.__version__ import (
    MIN_COMPATIBLE_UI as _CURRENT_MIN_UI,
    MIN_SUPPORTED_UPGRADE_FROM as _CURRENT_MIN_FROM,
    __version__ as _CURRENT_VERSION,
)


# ``OPENPA_UPGRADE_REPO`` overrides the default for staging / forks.
DEFAULT_REPO = os.environ.get("OPENPA_UPGRADE_REPO", "openpa/openpa")
LATEST_URL_TMPL = "https://api.github.com/repos/{repo}/releases/latest"


@dataclass(frozen=True)
class ReleaseInfo:
    """Minimal manifest the upgrader needs to make a decision.

    ``version`` is the parsed SemVer (no leading ``v``). ``asset_url`` is
    the optional ``release.json`` link if the release published one;
    when present, the upgrader fetches it and overlays the compatibility
    fields on top of the defaults.
    """

    version: str
    tag_name: str
    name: str
    html_url: str
    body: str
    asset_url: str | None
    min_compatible_ui: str
    min_supported_upgrade_from: str


def fetch_latest(repo: str = DEFAULT_REPO, *, timeout: float = 10.0) -> ReleaseInfo:
    """Fetch the latest release from GitHub. Raises on network failure.

    The caller decides what "the latest release" means — for a stable
    install we want the API's ``/releases/latest`` (which excludes
    pre-releases by default). For pre-release channels we'd switch to
    ``/releases`` and filter; this is left as a follow-up since the
    plan only commits to the stable channel for Phase 5.
    """
    url = LATEST_URL_TMPL.format(repo=repo)
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            # GitHub serves the same data without a token but the
            # rate limit is 60/hour/IP. A user-agent is required.
            "User-Agent": f"openpa-upgrader/{_CURRENT_VERSION}",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return _parse_release(data)


def _parse_release(payload: dict[str, Any]) -> ReleaseInfo:
    tag = payload.get("tag_name") or ""
    version = _strip_v(tag)
    if not _looks_like_semver(version):
        raise ValueError(f"GitHub release tag is not SemVer: {tag!r}")

    # Walk the assets list for ``release.json``. The manifest is optional
    # — when missing, the upgrader uses the conservative defaults
    # described in app/__version__.py.
    asset_url: str | None = None
    for asset in payload.get("assets") or []:
        if asset.get("name") == "release.json":
            asset_url = asset.get("browser_download_url")
            break

    min_ui = _CURRENT_MIN_UI
    min_from = _CURRENT_MIN_FROM
    if asset_url:
        try:
            extra = _fetch_release_manifest(asset_url)
            if isinstance(extra.get("min_compatible_ui"), str):
                min_ui = extra["min_compatible_ui"]
            if isinstance(extra.get("min_supported_upgrade_from"), str):
                min_from = extra["min_supported_upgrade_from"]
        except Exception:  # noqa: BLE001
            # The manifest is best-effort. Fall back to defaults rather
            # than refusing to upgrade because of a single missing asset.
            pass

    return ReleaseInfo(
        version=version,
        tag_name=tag,
        name=payload.get("name") or tag,
        html_url=payload.get("html_url") or "",
        body=payload.get("body") or "",
        asset_url=asset_url,
        min_compatible_ui=min_ui,
        min_supported_upgrade_from=min_from,
    )


def _fetch_release_manifest(url: str, *, timeout: float = 10.0) -> dict[str, Any]:
    req = urllib.request.Request(
        url, headers={"User-Agent": f"openpa-upgrader/{_CURRENT_VERSION}"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ── version helpers ───────────────────────────────────────────────────────

_SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:-[\w.]+)?(?:\+[\w.]+)?$")


def _strip_v(s: str) -> str:
    return s[1:] if s.startswith("v") else s


def _looks_like_semver(s: str) -> bool:
    return bool(_SEMVER.match(s))


def parse(s: str) -> tuple[int, int, int, str]:
    """Parse a SemVer string into a sortable tuple.

    Pre-release suffix is preserved as-is so ``1.0.0-rc.1 < 1.0.0``
    sorts correctly under tuple ordering (the empty string sorts after
    any non-empty pre-release tag in Python).
    """
    s = _strip_v(s)
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)(?:-([\w.]+))?", s)
    if not m:
        raise ValueError(f"Not a SemVer: {s!r}")
    major, minor, patch, pre = m.groups()
    return (int(major), int(minor), int(patch), pre or "~")


def is_newer(latest: str, current: str) -> bool:
    """``True`` iff ``latest`` is strictly newer than ``current``."""
    return parse(latest) > parse(current)


def is_at_or_above(version: str, floor: str) -> bool:
    """``True`` iff ``version`` >= ``floor`` under SemVer ordering."""
    return parse(version) >= parse(floor)


__all__ = [
    "DEFAULT_REPO",
    "ReleaseInfo",
    "fetch_latest",
    "is_at_or_above",
    "is_newer",
    "parse",
]

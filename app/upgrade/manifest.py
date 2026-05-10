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
from app.upgrade.channel import (
    Channel,
    get_channel,
    is_test_tag,
    parse_pep440,
    tag_to_pep440,
)


# ``OPENPA_UPGRADE_REPO`` overrides the default for staging / forks.
DEFAULT_REPO = os.environ.get("OPENPA_UPGRADE_REPO", "openpa/openpa")
LATEST_URL_TMPL = "https://api.github.com/repos/{repo}/releases/latest"
LIST_URL_TMPL = "https://api.github.com/repos/{repo}/releases"


@dataclass(frozen=True)
class ReleaseInfo:
    """Minimal manifest the upgrader needs to make a decision.

    ``version`` is the PEP 440 / SemVer string callers compare against
    ``__version__`` — for a test prerelease this is the ``.devN`` form,
    not the raw tag. ``asset_url`` is the optional ``release.json`` link
    if the release published one; when present, the upgrader fetches it
    and overlays the compatibility fields on top of the defaults.
    ``channel`` records which feed produced this record so the runner
    can build the right pip args (Test PyPI vs prod PyPI).
    """

    version: str
    tag_name: str
    name: str
    html_url: str
    body: str
    asset_url: str | None
    min_compatible_ui: str
    min_supported_upgrade_from: str
    channel: Channel


def fetch_latest(
    repo: str = DEFAULT_REPO,
    *,
    timeout: float = 10.0,
    channel: Channel | None = None,
) -> ReleaseInfo:
    """Fetch the latest release on the requested channel. Raises on failure.

    ``channel="production"`` (or ``None``, resolved via :func:`get_channel`)
    hits ``/releases/latest``. GitHub's documented behavior is that this
    endpoint excludes pre-releases, so prod hosts cannot accidentally see
    test tags — the regression-safety guarantee for existing installs.

    ``channel="test"`` lists ``/releases``, filters to entries marked
    ``prerelease=true`` whose tag matches ``v*-testN``, and returns the
    highest by PEP 440 ordering. Page 1 (default 30 items) is enough in
    practice; if a test channel ever accumulates more than 30 unreleased
    entries, the cap is the right thing to hit anyway.
    """
    ch: Channel = channel if channel is not None else get_channel()
    if ch == "test":
        return _fetch_latest_test(repo=repo, timeout=timeout)
    return _fetch_latest_prod(repo=repo, timeout=timeout)


def _fetch_latest_prod(*, repo: str, timeout: float) -> ReleaseInfo:
    url = LATEST_URL_TMPL.format(repo=repo)
    data = _http_get_json(url, timeout=timeout)
    return _parse_release(data, channel="production")


def _fetch_latest_test(*, repo: str, timeout: float) -> ReleaseInfo:
    url = LIST_URL_TMPL.format(repo=repo)
    payload = _http_get_json(url, timeout=timeout)
    if not isinstance(payload, list):
        raise ValueError("GitHub /releases did not return a list payload")

    candidates: list[dict[str, Any]] = []
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        if not entry.get("prerelease"):
            continue
        tag = entry.get("tag_name") or ""
        if is_test_tag(tag):
            candidates.append(entry)

    if not candidates:
        raise LookupError("No test prereleases found on GitHub for this repo.")

    candidates.sort(key=lambda e: parse_pep440(tag_to_pep440(e["tag_name"])))
    return _parse_release(candidates[-1], channel="test")


def _http_get_json(url: str, *, timeout: float) -> Any:
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
        return json.loads(resp.read().decode("utf-8"))


def _parse_release(payload: dict[str, Any], *, channel: Channel) -> ReleaseInfo:
    tag = payload.get("tag_name") or ""
    if channel == "test":
        # Test tags carry a ``-testN`` suffix that pip can't install
        # against; the wheel on Test PyPI is named with the PEP 440
        # ``.devN`` form. ``release-test.yml`` performs the same
        # rewrite in CI so the round-trip is exact.
        if not is_test_tag(tag):
            raise ValueError(f"Test channel got non-test tag: {tag!r}")
        version = tag_to_pep440(tag)
    else:
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
        channel=channel,
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


def parse(s: str) -> tuple[int, int, int, int, int]:
    """Parse a version string into a sortable PEP 440 tuple.

    Accepts the prod form (``0.1.5``) and the test-channel PEP 440 dev
    form (``0.1.5.dev3``). A leading ``v`` is tolerated. Returns the
    same shape as :func:`channel.parse_pep440`, which orders dev
    releases before the corresponding final.
    """
    return parse_pep440(_strip_v(s))


def is_newer(latest: str, current: str) -> bool:
    """``True`` iff ``latest`` is strictly newer than ``current``."""
    return parse(latest) > parse(current)


def is_at_or_above(version: str, floor: str) -> bool:
    """``True`` iff ``version`` >= ``floor`` under PEP 440 ordering."""
    return parse(version) >= parse(floor)


__all__ = [
    "DEFAULT_REPO",
    "ReleaseInfo",
    "fetch_latest",
    "is_at_or_above",
    "is_newer",
    "parse",
]

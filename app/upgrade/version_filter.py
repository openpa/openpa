"""Channel-aware install/upgrade version filtering.

Centralizes the rule "is this version allowed for (channel, electron build)?"
so the install scripts, the manifest fetcher, and the SetupWizard all agree.

Two layers:

  - :func:`matches_channel` is the looser check used by standalone CLI
    invocations of ``install.sh``/``install.ps1``: it only verifies the
    version *shape* matches the channel (no ``.devN`` on prod, mandatory
    ``.devN`` on test). It says nothing about the Electron build line.

  - :func:`matches_electron_line` adds the line constraint the Electron
    installer needs: production must match the Electron build exactly;
    test must be a ``.devN`` of the Electron build's ``X.Y.Z``. Dev
    has no version constraint.

The Electron app always passes its build version to the install scripts
via ``--electron-version``; the scripts apply the line constraint only
when that flag is present, so curl|bash invocations stay channel-loose.
"""

from __future__ import annotations

import re

from app.upgrade.channel import Channel


_PROD_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
_TEST_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+\.dev\d+$")


def matches_channel(version: str, channel: Channel) -> bool:
    """``True`` iff ``version`` is a shape-valid release for ``channel``.

    Production accepts ``X.Y.Z`` only — a ``.devN`` is a Test PyPI artifact
    and would fail to resolve on PyPI. Test accepts ``X.Y.Z.devN`` only —
    PyPI prod releases don't carry ``.devN`` and shouldn't be installed
    via the test channel. Dev has no shape constraint (the editable
    install ignores the version string entirely).
    """
    if channel == "production":
        return bool(_PROD_VERSION_RE.match(version or ""))
    if channel == "test":
        return bool(_TEST_VERSION_RE.match(version or ""))
    # Dev channel: no version constraint (editable install).
    return True


def matches_electron_line(
    version: str,
    channel: Channel,
    electron_version: str,
) -> bool:
    """``True`` iff ``version`` is allowed for ``channel`` on this Electron build.

    Production: must equal ``electron_version`` exactly. The Electron app
    and openpa package are released together under the same tag, and the
    tray-menu / taskbar code lives in the Electron main process — letting
    the user install a different ``X.Y.Z`` would silently desync the UI
    from the backend.

    Test: must match ``<electron_version>.devN`` for some ``N ≥ 1``. The
    test channel ships prereleases of the *current* dev line; installing
    a different line's devN means the Electron shell ships features the
    backend doesn't have (or vice versa).

    Dev: no constraint — the editable install satisfies whatever is asked.
    """
    if not matches_channel(version, channel):
        return False
    if channel == "production":
        return version == electron_version
    if channel == "test":
        return version.startswith(f"{electron_version}.dev") and version != f"{electron_version}.dev"
    return True


def validate(
    version: str,
    channel: Channel,
    *,
    electron_version: str | None = None,
) -> tuple[bool, str]:
    """Return ``(ok, error)``. ``error`` is empty when ``ok`` is True.

    When ``electron_version`` is ``None`` (standalone script invocation),
    only the channel-shape constraint is applied — the script doesn't
    know which Electron build the user intends to pair this install
    with, so we don't enforce the line. The Electron-invoked install
    *always* passes ``electron_version`` and gets the strict check.
    """
    if electron_version is not None:
        if matches_electron_line(version, channel, electron_version):
            return True, ""
        if channel == "production":
            return False, (
                f"Invalid version: {version!r} is not a valid production release "
                f"for this Electron build (v{electron_version}). Production "
                f"requires an exact version match — use the in-app update "
                f"flow to install a different version."
            )
        if channel == "test":
            return False, (
                f"Invalid version: {version!r} is not a valid test release "
                f"for this Electron build (v{electron_version}). Test channel "
                f"accepts only {electron_version}.devN prereleases."
            )
        return False, f"Invalid version: {version!r} is not valid for the dev channel."

    if matches_channel(version, channel):
        return True, ""
    if channel == "production":
        return False, (
            f"Invalid version: {version!r} does not look like a production "
            f"release (expected X.Y.Z)."
        )
    if channel == "test":
        return False, (
            f"Invalid version: {version!r} does not look like a test "
            f"prerelease (expected X.Y.Z.devN)."
        )
    return False, f"Invalid version: {version!r}."


def filter_same_line(
    versions: list[str],
    channel: Channel,
    electron_version: str,
) -> list[str]:
    """Return the subset of ``versions`` allowed for this Electron build.

    Order of input is preserved (callers typically sort by PEP 440 first
    via :func:`app.upgrade.channel.parse_pep440`).
    """
    return [v for v in versions if matches_electron_line(v, channel, electron_version)]


__all__ = [
    "filter_same_line",
    "matches_channel",
    "matches_electron_line",
    "validate",
]

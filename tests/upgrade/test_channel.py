"""Unit tests for channel-aware upgrade detection."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.upgrade import channel, manifest, runner


# ── channel.py ────────────────────────────────────────────────────────────


def test_get_channel_defaults_to_production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENPA_UPGRADE_CHANNEL", raising=False)
    assert channel.get_channel() == "production"


def test_get_channel_test_when_env_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENPA_UPGRADE_CHANNEL", "test")
    assert channel.get_channel() == "test"


def test_get_channel_unknown_value_defaults_to_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENPA_UPGRADE_CHANNEL", "canary")
    assert channel.get_channel() == "production"


@pytest.mark.parametrize(
    "tag,expected",
    [
        ("v0.1.5-test1", True),
        ("v10.20.30-test99", True),
        ("v0.1.5", False),
        ("v0.1.5-rc.1", False),
        ("0.1.5-test1", False),  # missing leading v
        ("", False),
    ],
)
def test_is_test_tag(tag: str, expected: bool) -> None:
    assert channel.is_test_tag(tag) is expected


def test_tag_to_pep440_round_trip() -> None:
    assert channel.tag_to_pep440("v0.1.5-test3") == "0.1.5.dev3"
    assert channel.tag_to_pep440("v2.7.18-test1") == "2.7.18.dev1"


def test_tag_to_pep440_rejects_non_test_tag() -> None:
    with pytest.raises(ValueError):
        channel.tag_to_pep440("v0.1.5")


def test_parse_pep440_orders_dev_before_final() -> None:
    # Dev releases are PEP 440 pre-releases — they sort before the
    # corresponding final regardless of the dev counter.
    assert channel.parse_pep440("0.1.5.dev1") < channel.parse_pep440("0.1.5.dev2")
    assert channel.parse_pep440("0.1.5.dev99") < channel.parse_pep440("0.1.5")
    assert channel.parse_pep440("0.1.5") < channel.parse_pep440("0.1.6.dev1")


def test_parse_pep440_rejects_bad_input() -> None:
    with pytest.raises(ValueError):
        channel.parse_pep440("not-a-version")
    with pytest.raises(ValueError):
        channel.parse_pep440("0.1.5-test1")  # SemVer pre-release, not PEP 440


def test_channel_is_newer_handles_dev_releases() -> None:
    assert channel.is_newer("0.1.5.dev2", "0.1.5.dev1") is True
    assert channel.is_newer("0.1.5", "0.1.5.dev99") is True
    assert channel.is_newer("0.1.5.dev1", "0.1.5") is False


# ── manifest.py version helpers ───────────────────────────────────────────


def test_manifest_parse_handles_both_forms() -> None:
    assert manifest.parse("0.1.5") < manifest.parse("0.1.6")
    assert manifest.parse("0.1.5.dev3") < manifest.parse("0.1.5")
    # Leading ``v`` must be tolerated — that's what arrives from tag names.
    assert manifest.parse("v0.1.5") == manifest.parse("0.1.5")


def test_manifest_is_at_or_above_for_dev_install() -> None:
    # A test-channel install at 0.1.5.dev1 still satisfies a floor of 0.1.0.
    assert manifest.is_at_or_above("0.1.5.dev1", "0.1.0") is True
    # But not a floor newer than its base.
    assert manifest.is_at_or_above("0.1.5.dev1", "0.2.0") is False


# ── manifest._parse_release ───────────────────────────────────────────────


def _release_payload(tag: str, *, prerelease: bool = False) -> dict[str, Any]:
    return {
        "tag_name": tag,
        "name": tag,
        "html_url": f"https://example.invalid/r/{tag}",
        "body": "",
        "assets": [],
        "prerelease": prerelease,
    }


def test_parse_release_prod_strips_v() -> None:
    info = manifest._parse_release(_release_payload("v0.1.5"), channel="production")
    assert info.version == "0.1.5"
    assert info.tag_name == "v0.1.5"
    assert info.channel == "production"


def test_parse_release_test_translates_to_pep440() -> None:
    info = manifest._parse_release(
        _release_payload("v0.1.5-test3", prerelease=True), channel="test",
    )
    assert info.version == "0.1.5.dev3"
    assert info.tag_name == "v0.1.5-test3"
    assert info.channel == "test"


def test_parse_release_test_rejects_non_test_tag() -> None:
    with pytest.raises(ValueError):
        manifest._parse_release(_release_payload("v0.1.5"), channel="test")


# ── manifest._fetch_latest_test selection ────────────────────────────────


def test_fetch_latest_test_picks_highest_prerelease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = [
        _release_payload("v0.1.5", prerelease=False),
        _release_payload("v0.1.5-test1", prerelease=True),
        _release_payload("v0.1.5-test3", prerelease=True),
        _release_payload("v0.1.5-test2", prerelease=True),
        # Non-test prerelease: filtered out even though prerelease=True.
        _release_payload("v0.2.0-rc.1", prerelease=True),
    ]
    monkeypatch.setattr(manifest, "_http_get_json", lambda url, *, timeout: payload)
    info = manifest._fetch_latest_test(repo="x/y", timeout=1.0)
    assert info.tag_name == "v0.1.5-test3"
    assert info.version == "0.1.5.dev3"


def test_fetch_latest_test_raises_when_no_test_prereleases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = [_release_payload("v0.1.5", prerelease=False)]
    monkeypatch.setattr(manifest, "_http_get_json", lambda url, *, timeout: payload)
    with pytest.raises(LookupError):
        manifest._fetch_latest_test(repo="x/y", timeout=1.0)


def test_fetch_latest_dispatches_on_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_prod(*, repo, timeout):
        calls.append("prod")
        return MagicMock()

    def fake_test(*, repo, timeout):
        calls.append("test")
        return MagicMock()

    monkeypatch.setattr(manifest, "_fetch_latest_prod", fake_prod)
    monkeypatch.setattr(manifest, "_fetch_latest_test", fake_test)

    manifest.fetch_latest(channel="production")
    manifest.fetch_latest(channel="test")
    assert calls == ["prod", "test"]


# ── runner._pip_install argv composition ──────────────────────────────────


def _fake_release(
    *, channel_value: str = "production", version: str = "0.1.5", tag: str | None = None,
) -> manifest.ReleaseInfo:
    if tag is None:
        tag = f"v{version}"
    return manifest.ReleaseInfo(
        version=version,
        tag_name=tag,
        name=tag,
        html_url="",
        body="",
        asset_url=None,
        min_compatible_ui="0.1.0",
        min_supported_upgrade_from="0.1.0",
        channel=channel_value,  # type: ignore[arg-type]
    )


def test_pip_install_prod_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_run(cmd, callback, *, ignore_failure=False, prefer=None, env=None):
        captured["cmd"] = cmd
        captured["env"] = env

    monkeypatch.setattr(runner, "_run", fake_run)
    from app.config.settings import BaseConfig
    monkeypatch.setattr(BaseConfig, "OPENPA_WORKING_DIR", "/tmp/openpa-test")

    runner._pip_install("openpa==0.1.5", None, channel="production")

    cmd = captured["cmd"]
    assert cmd[1:5] == ["-m", "pip", "install", "--upgrade"]
    assert cmd[-1] == "openpa==0.1.5"
    assert "--pre" not in cmd
    assert "--index-url" not in cmd
    assert captured["env"]["PIP_CACHE_DIR"].endswith("pip-cache")


def test_pip_install_test_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_run(cmd, callback, *, ignore_failure=False, prefer=None, env=None):
        captured["cmd"] = cmd
        captured["env"] = env

    monkeypatch.setattr(runner, "_run", fake_run)
    from app.config.settings import BaseConfig
    monkeypatch.setattr(BaseConfig, "OPENPA_WORKING_DIR", "/tmp/openpa-test")
    monkeypatch.setenv("OPENPA_PIP_INDEX_URL", "https://test.pypi.org/simple/")
    monkeypatch.setenv("OPENPA_PIP_EXTRA_INDEX_URL", "https://pypi.org/simple/")

    runner._pip_install("openpa==0.1.5.dev3", None, channel="test")

    cmd = captured["cmd"]
    assert "--pre" in cmd
    assert "--index-url" in cmd
    idx = cmd.index("--index-url")
    assert cmd[idx + 1] == "https://test.pypi.org/simple/"
    assert "--extra-index-url" in cmd
    eidx = cmd.index("--extra-index-url")
    assert cmd[eidx + 1] == "https://pypi.org/simple/"
    assert cmd[-1] == "openpa==0.1.5.dev3"
    assert captured["env"]["PIP_CACHE_DIR"].endswith("pip-cache")


def test_pip_install_test_falls_back_to_default_indexes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the .env keys are absent, we still target Test PyPI."""
    captured: dict[str, Any] = {}

    def fake_run(cmd, callback, *, ignore_failure=False, prefer=None, env=None):
        captured["cmd"] = cmd

    monkeypatch.setattr(runner, "_run", fake_run)
    from app.config.settings import BaseConfig
    monkeypatch.setattr(BaseConfig, "OPENPA_WORKING_DIR", "/tmp/openpa-test")
    monkeypatch.delenv("OPENPA_PIP_INDEX_URL", raising=False)
    monkeypatch.delenv("OPENPA_PIP_EXTRA_INDEX_URL", raising=False)

    runner._pip_install("openpa==0.1.5.dev1", None, channel="test")

    cmd = captured["cmd"]
    idx = cmd.index("--index-url")
    assert cmd[idx + 1] == "https://test.pypi.org/simple/"


# ── runner lock state ────────────────────────────────────────────────────


def test_acquire_lock_or_recover_uses_persisted_channel(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the lock JSON carries ``channel: test``, recovery must pip-install
    against Test PyPI (otherwise the dev wheel won't be found on prod PyPI).
    """
    lock = tmp_path / ".upgrade.lock"
    # Intentionally point at a missing backup file so the
    # restore-from-backup branch is skipped — that import path pulls in
    # ``app.storage`` which transitively requires alembic, and we don't
    # want this unit test to drag in a DB toolchain.
    lock.write_text(
        json.dumps(
            {
                "started_at": 0.0,
                "previous_version": "0.1.5.dev1",
                "target_version": "0.1.5.dev2",
                "backup_path": str(tmp_path / "does-not-exist.bin"),
                "channel": "test",
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(runner, "_lock_path", lambda: lock)

    seen: dict[str, Any] = {}

    def fake_pip_install(spec, callback, *, channel):
        seen["spec"] = spec
        seen["channel"] = channel

    monkeypatch.setattr(runner, "_pip_install", fake_pip_install)

    runner.acquire_lock_or_recover(callback=None)

    assert seen["channel"] == "test"
    assert seen["spec"] == "openpa==0.1.5.dev1"
    assert not lock.exists()  # cleared after recovery


def test_acquire_lock_or_recover_defaults_to_production_for_legacy_lock(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lock files written before channel-aware upgrades have no ``channel`` key.
    Recovery must fall back to production rather than crashing.
    """
    lock = tmp_path / ".upgrade.lock"
    lock.write_text(
        json.dumps(
            {
                "started_at": 0.0,
                "previous_version": "0.1.4",
                "target_version": "0.1.5",
                "backup_path": str(tmp_path / "missing.bin"),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(runner, "_lock_path", lambda: lock)

    seen: dict[str, Any] = {}

    def fake_pip_install(spec, callback, *, channel):
        seen["channel"] = channel

    monkeypatch.setattr(runner, "_pip_install", fake_pip_install)

    runner.acquire_lock_or_recover(callback=None)

    assert seen["channel"] == "production"

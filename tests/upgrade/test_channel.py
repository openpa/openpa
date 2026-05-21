"""Unit tests for channel-aware upgrade detection."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.features import manifest as feature_manifest
from app.upgrade import channel, manifest, runner, version_filter


# ── channel.py ────────────────────────────────────────────────────────────


def test_get_channel_defaults_to_production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENPA_UPGRADE_CHANNEL", raising=False)
    assert channel.get_channel() == "production"


def test_get_channel_test_when_env_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENPA_UPGRADE_CHANNEL", "test")
    assert channel.get_channel() == "test"


def test_get_channel_dev_when_env_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENPA_UPGRADE_CHANNEL", "dev")
    assert channel.get_channel() == "dev"


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


# ── features.pip_spec channel handling ────────────────────────────────────


def test_pip_spec_production_pins_version() -> None:
    from app.__version__ import __version__

    spec = feature_manifest.pip_spec(["embedding.me5"], channel="production")
    assert spec == f"openpa[embeddings-me5]=={__version__}"


def test_pip_spec_test_pins_version() -> None:
    # Test channel still pins — Test PyPI has the matching prerelease.
    from app.__version__ import __version__

    spec = feature_manifest.pip_spec(["embedding.me5"], channel="test")
    assert spec == f"openpa[embeddings-me5]=={__version__}"


def test_pip_spec_dev_skips_version_pin() -> None:
    # Dev channel: editable install in /src satisfies the requirement,
    # pinning to ``==<version>`` would force pip to consult PyPI for a
    # release that may not be published yet.
    spec = feature_manifest.pip_spec(
        ["embedding.me5", "vectorstore.chroma"],
        channel="dev",
    )
    assert spec == "openpa[embeddings-me5,vectorstore-chroma]"


def test_pip_spec_dev_with_no_features() -> None:
    assert feature_manifest.pip_spec([], channel="dev") == "openpa"


def test_pip_spec_defaults_to_production() -> None:
    from app.__version__ import __version__

    spec = feature_manifest.pip_spec(["embedding.me5"])
    assert spec == f"openpa[embeddings-me5]=={__version__}"


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
        _release_payload("v0.1.5-test3", prerelease=True),
        channel="test",
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


# ── dev-channel synthesis ────────────────────────────────────────────────


def test_fetch_latest_dev_synthesizes_release_without_github(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On dev, fetch_latest must NOT hit GitHub — both _fetch_latest_prod
    and _fetch_latest_test should remain uncalled. The synth release is
    enough on its own to drive the in-app updater UI for testing.
    """
    prod_calls: list[None] = []
    test_calls: list[None] = []

    def fake_prod(*, repo, timeout):
        prod_calls.append(None)
        raise AssertionError("prod fetcher must not run on dev channel")

    def fake_test(*, repo, timeout):
        test_calls.append(None)
        raise AssertionError("test fetcher must not run on dev channel")

    monkeypatch.setattr(manifest, "_fetch_latest_prod", fake_prod)
    monkeypatch.setattr(manifest, "_fetch_latest_test", fake_test)

    info = manifest.fetch_latest(channel="dev")
    assert info.channel == "dev"
    # The synth carries the local-version suffix exactly.
    assert info.version.endswith("+devforced")
    # Compatibility floors are wide-open so existing dev installs are never blocked.
    assert info.min_compatible_ui == "0.0.0"
    assert info.min_supported_upgrade_from == "0.0.0"
    assert prod_calls == []
    assert test_calls == []


def test_synthesized_dev_release_sorts_strictly_newer() -> None:
    """The synth version must be > CURRENT_VERSION under :func:`manifest.parse`
    for every current-version shape we ship. If this regresses, the Update
    button will go dark on dev and there will be nothing to click.
    """
    synth = manifest._synthesize_dev_release()
    # Walk a representative set of current-version shapes (final, dev,
    # different majors/minors). Each must parse strictly less than synth.
    for current in ("0.1.9", "0.1.9.dev6", "0.1.10.dev1", "0.2.0", "1.0.0"):
        # Substitute a synth derived from THIS current so we test the
        # actual relationship the runtime uses ("current+devforced > current").
        forged_synth_version = f"{current}+devforced"
        assert manifest.parse(forged_synth_version) > manifest.parse(
            current
        ), f"synth {forged_synth_version!r} did not sort > {current!r}"
    # And the live synth (derived from the running __version__) must
    # itself sort newer than the running version.
    from app.__version__ import __version__ as CURRENT

    assert manifest.parse(synth.version) > manifest.parse(CURRENT)


def test_parse_pep440_accepts_local_version_suffix() -> None:
    """parse_pep440's regex was extended to accept ``+local``; verify
    populated local sorts strictly above empty, and the lexical compare
    works the way the synth relies on.
    """
    assert channel.parse_pep440("0.1.5") < channel.parse_pep440("0.1.5+a")
    assert channel.parse_pep440("0.1.5+a") < channel.parse_pep440("0.1.5+b")
    assert channel.parse_pep440("0.1.5.dev1") < channel.parse_pep440("0.1.5.dev1+x")
    # The original rejections still hold.
    with pytest.raises(ValueError):
        channel.parse_pep440("0.1.5-test1+x")  # SemVer pre-release, not PEP 440


# ── version_filter ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "version,channel_name,expected",
    [
        ("0.1.9", "production", True),
        ("0.1.10", "production", True),
        ("0.1.9.dev3", "production", False),  # devN forbidden on prod
        ("0.1", "production", False),  # not X.Y.Z
        ("0.1.9.dev3", "test", True),
        ("0.1.9", "test", False),  # missing devN
        ("0.1.9.dev", "test", False),  # missing N
        ("anything", "dev", True),  # dev imposes no shape
    ],
)
def test_version_filter_matches_channel(
    version: str, channel_name: str, expected: bool
) -> None:
    assert version_filter.matches_channel(version, channel_name) is expected  # type: ignore[arg-type]


def test_version_filter_matches_electron_line_production_strict() -> None:
    # Production: must equal the Electron build version exactly.
    assert version_filter.matches_electron_line("0.1.9", "production", "0.1.9") is True
    assert version_filter.matches_electron_line("0.1.10", "production", "0.1.9") is False
    assert version_filter.matches_electron_line("0.1.8", "production", "0.1.9") is False
    # ``.devN`` is not a valid production version regardless of line.
    assert version_filter.matches_electron_line("0.1.9.dev1", "production", "0.1.9") is False


def test_version_filter_matches_electron_line_test_same_line() -> None:
    # Test: only ``<electron>.devN`` is allowed; other lines are rejected.
    assert version_filter.matches_electron_line("0.1.9.dev1", "test", "0.1.9") is True
    assert version_filter.matches_electron_line("0.1.9.dev99", "test", "0.1.9") is True
    assert version_filter.matches_electron_line("0.1.10.dev1", "test", "0.1.9") is False
    assert version_filter.matches_electron_line("0.1.8.dev3", "test", "0.1.9") is False
    # Final releases (no .devN) are not test releases.
    assert version_filter.matches_electron_line("0.1.9", "test", "0.1.9") is False


def test_version_filter_matches_electron_line_no_prefix_overrun() -> None:
    # Regression: ``0.1.9`` must not accidentally allow ``0.1.91.devN``
    # under a naive ``startswith`` check. The regex anchors on the literal
    # dot via the shape rule, so ``0.1.91.dev1`` fails the shape too —
    # but verify both layers explicitly.
    assert version_filter.matches_channel("0.1.91.dev1", "test") is True  # shape OK
    assert version_filter.matches_electron_line("0.1.91.dev1", "test", "0.1.9") is False


def test_version_filter_validate_production_rejects_mismatch() -> None:
    ok, err = version_filter.validate("0.1.10", "production", electron_version="0.1.9")
    assert ok is False
    assert "Invalid version" in err
    assert "0.1.10" in err
    assert "0.1.9" in err  # mentions the Electron build


def test_version_filter_validate_test_rejects_cross_line() -> None:
    ok, err = version_filter.validate("0.1.10.dev1", "test", electron_version="0.1.9")
    assert ok is False
    assert "Invalid version" in err
    assert "0.1.9.devN" in err


def test_version_filter_validate_test_accepts_same_line() -> None:
    ok, err = version_filter.validate("0.1.9.dev3", "test", electron_version="0.1.9")
    assert ok is True
    assert err == ""


def test_version_filter_validate_without_electron_falls_back_to_shape() -> None:
    # Standalone install.sh invocation (no Electron context): only the
    # channel-shape rule applies — line constraint is skipped.
    ok, _ = version_filter.validate("0.1.10", "production")
    assert ok is True
    ok, _ = version_filter.validate("0.1.10.dev1", "test")
    assert ok is True
    ok, err = version_filter.validate("0.1.10.dev1", "production")
    assert ok is False
    assert "X.Y.Z" in err


def test_version_filter_filter_same_line_preserves_input_order() -> None:
    # Mixed list across two minor lines; only the 0.1.9.devN entries
    # survive the filter, and their input order is preserved (callers
    # are expected to sort by PEP 440 before / after filtering).
    versions = [
        "0.1.9.dev2",
        "0.1.10.dev1",
        "0.1.9.dev3",
        "0.1.8.dev5",
        "0.1.9.dev1",
    ]
    out = version_filter.filter_same_line(versions, "test", "0.1.9")
    assert out == ["0.1.9.dev2", "0.1.9.dev3", "0.1.9.dev1"]


# ── runner skip-pip-on-dev ───────────────────────────────────────────────


# ── CLI surface — ``openpa upgrade -y`` must parse ──────────────────────


def test_upgrade_cli_accepts_yes_flag_at_group_level(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression test for ``openpa upgrade --yes`` / ``-y``.

    The flag is declared on the ``apply`` subcommand, but the documented
    one-liner ``openpa upgrade -y`` puts the flag at the group level
    (before any subcommand name). Typer parses group-level args against
    the root callback; if the root doesn't declare the flag, the run
    fails with "No such option: --yes" before ``apply`` ever runs.

    This was a real regression at one point — the Electron updater's
    ``Apply now`` button printed exactly that error to the modal log.
    """
    from typer.testing import CliRunner
    from app.cli.commands.upgrade import upgrade_app
    from app.upgrade import runner

    # Stub the runner so this stays a CLI-parse test, not an
    # end-to-end upgrade. We assert on the *captured* yes value.
    captured: dict[str, object] = {}

    def fake_apply(target_version=None, *, callback=None, confirm=None):
        # ``confirm`` is the gate `apply` wraps the runner's confirm with.
        # When ``yes`` is True, the wrapper short-circuits to True without
        # prompting, so the safest way to verify ``yes`` reached us is to
        # check whether confirm returns True against a dummy release.
        captured["confirmed_without_prompting"] = confirm(MagicMock()) if confirm else False
        return True

    monkeypatch.setattr(runner, "apply", fake_apply)
    monkeypatch.setattr(runner, "check", lambda *a, **kw: (MagicMock(version="x"), "available"))

    runner_cli = CliRunner()

    # Form 1: group-level flag (the documented one-liner).
    result = runner_cli.invoke(upgrade_app, ["--yes"])
    assert result.exit_code == 0, result.output
    assert captured["confirmed_without_prompting"] is True

    # Form 2: subcommand-level flag (also documented; Electron uses this now).
    captured.clear()
    result = runner_cli.invoke(upgrade_app, ["apply", "--yes"])
    assert result.exit_code == 0, result.output
    assert captured["confirmed_without_prompting"] is True

    # Form 3: short flag.
    captured.clear()
    result = runner_cli.invoke(upgrade_app, ["-y"])
    assert result.exit_code == 0, result.output
    assert captured["confirmed_without_prompting"] is True


def test_apply_on_dev_skips_pip_install_but_still_migrates_and_health_checks(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Clicking Update on a dev install must walk backup → (skipped install)
    → migrate → health → done. The pip install is the one step that would
    clobber the editable install; everything else is safe to run."""
    dev_release = _fake_release(channel_value="dev", version="0.1.5+devforced")

    # Force the runner past the version-check guard by stubbing check() to
    # report "available" with our dev release.
    monkeypatch.setattr(runner, "check", lambda callback=None: (dev_release, "available"))

    pip_calls: list[str] = []
    run_calls: list[list[str]] = []
    health_calls: list[None] = []

    def fake_pip_install(spec, callback, *, channel, upgrade=True):
        pip_calls.append(spec)

    def fake_run(cmd, callback, **kwargs):
        run_calls.append(list(cmd))

    def fake_wait_for_health(*, timeout_s):
        health_calls.append(None)
        return True

    def fake_check_disk(*, min_free_mb):
        return None

    monkeypatch.setattr(runner, "_pip_install", fake_pip_install)
    monkeypatch.setattr(runner, "_run", fake_run)
    monkeypatch.setattr(runner, "_wait_for_health", fake_wait_for_health)
    monkeypatch.setattr(runner, "_check_disk_space", fake_check_disk)

    # Lock dir + backup stub.
    monkeypatch.setattr(runner, "_lock_path", lambda: tmp_path / ".upgrade.lock")
    from app.storage import backup as backup_module

    monkeypatch.setattr(backup_module, "backup", lambda: tmp_path / "fake.sqlite.gz")

    # Create the fake backup file so the runner's success-path doesn't
    # try to read a missing file in the rollback branch (it shouldn't,
    # since this test exercises the success path).
    (tmp_path / "fake.sqlite.gz").write_bytes(b"")

    events: list[runner.UpgradeEvent] = []
    ok = runner.apply(callback=events.append)
    assert ok is True, [e.message for e in events if not e.ok]

    # The critical assertion: pip was NOT called on dev.
    assert pip_calls == [], f"pip_install should be skipped on dev, got: {pip_calls!r}"

    # But migrate, health, and the rest of the flow DID run.
    assert any(
        "db" in cmd and "upgrade" in cmd for cmd in run_calls
    ), f"migrate step (openpa db upgrade) was not invoked: {run_calls!r}"
    assert health_calls == [None], "health probe was not invoked"

    # The install event was emitted with the skip message so the modal
    # log shows it to the user.
    install_events = [e for e in events if e.kind == "install"]
    assert any(
        "skipped" in e.message.lower() for e in install_events
    ), f"no 'skipped' install event found in: {[e.message for e in install_events]}"


# ── runner._pip_install argv composition ──────────────────────────────────


def _fake_release(
    *,
    channel_value: str = "production",
    version: str = "0.1.5",
    tag: str | None = None,
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

    monkeypatch.setattr(BaseConfig, "OPENPA_SYSTEM_DIR", "/tmp/openpa-test")

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

    monkeypatch.setattr(BaseConfig, "OPENPA_SYSTEM_DIR", "/tmp/openpa-test")
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

    monkeypatch.setattr(BaseConfig, "OPENPA_SYSTEM_DIR", "/tmp/openpa-test")
    monkeypatch.delenv("OPENPA_PIP_INDEX_URL", raising=False)
    monkeypatch.delenv("OPENPA_PIP_EXTRA_INDEX_URL", raising=False)

    runner._pip_install("openpa==0.1.5.dev1", None, channel="test")

    cmd = captured["cmd"]
    idx = cmd.index("--index-url")
    assert cmd[idx + 1] == "https://test.pypi.org/simple/"


# ── runner lock state ────────────────────────────────────────────────────


def test_acquire_lock_or_recover_uses_persisted_channel(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
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
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
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

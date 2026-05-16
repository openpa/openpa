"""Tests for the /api/upgrade endpoints (check / apply / status / stream).

The detached subprocess spawn itself isn't tested here — that's a
cross-platform Popen invocation we trust at the OS layer. We do test
the gating logic that wraps it: 409 when an upgrade is already
running, 401 when unauth, 202 when the spawn is mocked out.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest
from starlette.applications import Starlette
from starlette.authentication import (
    AuthCredentials,
    AuthenticationBackend,
    SimpleUser,
)
from starlette.middleware import Middleware
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.testclient import TestClient

from app.api.upgrade import get_upgrade_routes
from app.upgrade import status


# ── auth helpers ─────────────────────────────────────────────────────────


class _AlwaysAdmin(AuthenticationBackend):
    """Test backend that always authenticates as ``admin``."""

    async def authenticate(self, conn):
        return AuthCredentials(["authenticated"]), SimpleUser("admin")


class _AlwaysAnon(AuthenticationBackend):
    """Test backend that never authenticates."""

    async def authenticate(self, conn):
        return AuthCredentials([]), None


def _make_app(backend: AuthenticationBackend) -> Starlette:
    return Starlette(
        routes=get_upgrade_routes(),
        middleware=[Middleware(AuthenticationMiddleware, backend=backend)],
    )


# ── fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolate_working_dir(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config.settings import BaseConfig

    monkeypatch.setattr(BaseConfig, "OPENPA_WORKING_DIR", str(tmp_path))


# ── POST /api/upgrade/apply ──────────────────────────────────────────────


def test_apply_rejects_unauth() -> None:
    client = TestClient(_make_app(_AlwaysAnon()))
    r = client.post("/api/upgrade/apply")
    assert r.status_code == 401
    assert r.json()["error"] == "Unauthenticated"


def test_apply_returns_409_when_already_running() -> None:
    status.begin(current_version="0.1.9", target_version="0.1.10")
    status.update_phase("install", "pip install openpa==0.1.10")
    client = TestClient(_make_app(_AlwaysAdmin()))
    r = client.post("/api/upgrade/apply")
    assert r.status_code == 409
    body = r.json()
    assert "already running" in body["error"].lower()
    assert body["status_url"] == "/api/upgrade/status"


def test_apply_spawns_detached_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return MagicMock(pid=12345)

    import app.api.upgrade as upgrade_api

    monkeypatch.setattr(upgrade_api.subprocess, "Popen", fake_popen)

    client = TestClient(_make_app(_AlwaysAdmin()))
    r = client.post("/api/upgrade/apply")
    assert r.status_code == 202
    body = r.json()
    assert body["ok"] is True
    assert body["pid"] == 12345
    assert body["status_url"] == "/api/upgrade/status"
    assert body["stream_url"] == "/api/upgrade/stream"

    # Verify the spawn invokes the detached module, not just any
    # subprocess — guards against accidentally invoking the in-process
    # runner.
    cmd = captured["cmd"]
    assert cmd[1:4] == ["-m", "app.upgrade.detached", "--parent-pid"]


# ── GET /api/upgrade/status ──────────────────────────────────────────────


def test_status_rejects_unauth() -> None:
    client = TestClient(_make_app(_AlwaysAnon()))
    r = client.get("/api/upgrade/status")
    assert r.status_code == 401


def test_status_returns_idle_when_no_upgrade() -> None:
    client = TestClient(_make_app(_AlwaysAdmin()))
    r = client.get("/api/upgrade/status")
    assert r.status_code == 200
    body = r.json()
    assert body["phase"] == "idle"
    assert body["log_tail"] == []


def test_status_reflects_in_flight_state() -> None:
    status.begin(current_version="0.1.9", target_version="0.1.10")
    status.update_phase("install", "Installing wheel...")
    status.append_log("Collecting openpa==0.1.10")
    client = TestClient(_make_app(_AlwaysAdmin()))
    r = client.get("/api/upgrade/status")
    body = r.json()
    assert body["phase"] == "install"
    assert body["target_version"] == "0.1.10"
    # The phase-update header line plus our explicit append_log line.
    assert any("Installing wheel" in line for line in body["log_tail"])
    assert "Collecting openpa==0.1.10" in body["log_tail"]


# ── GET /api/upgrade/stream ──────────────────────────────────────────────


def test_stream_rejects_unauth() -> None:
    client = TestClient(_make_app(_AlwaysAnon()))
    r = client.get("/api/upgrade/stream")
    assert r.status_code == 401


def test_stream_emits_terminal_state_and_closes() -> None:
    """When the file is already in a terminal state, the stream should
    emit one event and end. Without this the renderer would hang on a
    "done" upgrade forever waiting for an event that never comes.
    """
    status.begin(current_version="0.1.9", target_version="0.1.10")
    status.finish(ok=True, exit_code=0)

    client = TestClient(_make_app(_AlwaysAdmin()))
    with client.stream("GET", "/api/upgrade/stream") as r:
        assert r.status_code == 200
        chunks: list[bytes] = []
        for chunk in r.iter_bytes():
            chunks.append(chunk)
            # The terminal-state branch emits once and returns; bail
            # immediately so the test doesn't hang if it didn't.
            if b'"phase": "done"' in b"".join(chunks):
                break

    raw = b"".join(chunks).decode("utf-8")
    assert "event: status" in raw
    # Round-trip the data payload back through JSON.
    data_lines = [line[6:] for line in raw.splitlines() if line.startswith("data: ")]
    assert data_lines, "stream emitted no data: lines"
    payload = json.loads(data_lines[-1])
    assert payload["phase"] == "done"
    assert payload["ok"] is True


# ── GET /api/upgrade/check ───────────────────────────────────────────────


def test_check_on_dev_channel_returns_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On the dev channel, /api/upgrade/check must always return
    status=available regardless of GitHub state. The Update button on
    the banner / Settings page only renders for ``available``, so
    without this the in-app updater UI is untestable on a working-copy
    install.
    """
    monkeypatch.setenv("OPENPA_UPGRADE_CHANNEL", "dev")

    # Make any GitHub call explode so we'd notice if the dev path
    # accidentally hit the network.
    import app.upgrade.manifest as manifest_module

    def explode(*args, **kwargs):
        raise AssertionError("dev /check must not hit GitHub")

    monkeypatch.setattr(manifest_module, "_fetch_latest_prod", explode)
    monkeypatch.setattr(manifest_module, "_fetch_latest_test", explode)

    client = TestClient(_make_app(_AlwaysAnon()))
    r = client.get("/api/upgrade/check")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "available"
    assert body["channel"] == "dev"
    # The synth version carries the local-version suffix.
    assert body["latest"].endswith("+devforced"), body
    # min_compatible_ui is wide-open so the dev path never trips the
    # "incompatible UI" blocked banner.
    assert body["min_compatible_ui"] == "0.0.0"


def test_check_is_public_no_apply_command_field() -> None:
    """``/check`` used to expose ``apply_command`` for the copy-the-CLI
    UX. The unified-banner work removes that field; the renderer no
    longer shows a command to copy. Anyone running an older renderer
    will simply not see the command, which is fine.
    """
    # Stub fetch_latest to a known-newer release so we hit the
    # ``available`` branch without an HTTP call.
    from app.upgrade import manifest

    fake = manifest.ReleaseInfo(
        version="99.0.0",
        tag_name="v99.0.0",
        name="v99.0.0",
        html_url="https://example.invalid/r/v99.0.0",
        body="release body",
        asset_url=None,
        min_compatible_ui="0.0.0",
        min_supported_upgrade_from="0.0.0",
        channel="production",
    )

    import app.api.upgrade as upgrade_api

    # patch the inside-the-handler import via the source module
    monkeypatched = MagicMock(return_value=fake)
    import app.upgrade.manifest as manifest_module

    original = manifest_module.fetch_latest
    manifest_module.fetch_latest = monkeypatched  # type: ignore[assignment]
    try:
        # Anonymous: /check is intentionally still public.
        client = TestClient(_make_app(_AlwaysAnon()))
        r = client.get("/api/upgrade/check")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "available"
        assert body["latest"] == "99.0.0"
        # The old ``apply_command`` field is gone — the renderer drives
        # the upgrade through POST /apply now.
        assert "apply_command" not in body
    finally:
        manifest_module.fetch_latest = original  # type: ignore[assignment]
    _ = upgrade_api  # silence unused-import lint

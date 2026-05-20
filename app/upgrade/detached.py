"""Detached upgrade runner — the body of an upgrade triggered via
``POST /api/upgrade/apply``.

Invoked as a sibling subprocess of the running HTTP server:

    python -m app.upgrade.detached --parent-pid <PID>

The HTTP server can't run :func:`app.upgrade.runner.apply` in-process
because the runner replaces its own wheel files on disk mid-flight. So
the API endpoint spawns *this* module detached, returns 202 immediately,
and lets the new process drive the install / migrate / restart cycle
on its own. Progress lands in the status file (see
:mod:`app.upgrade.status`) which the API endpoints poll.

After a successful upgrade the runner kills ``--parent-pid``. What
happens next depends on how the install is supervised:

- Under Docker: the entrypoint's ``wait -n`` exits when ``openpa serve``
  dies, Docker's restart policy brings the container back up, the
  entrypoint runs migrations again (idempotent) and re-launches the
  backend on the new wheel.
- Under Electron: the main process's ``runBackendUpgrade`` already
  handles the restart through its own IPC handler — for those installs
  the renderer uses the IPC bridge, not this detached path, so this
  module's parent-kill is a no-op (parent pid never gets passed).
- Under a bare ``openpa serve`` with no supervisor: the backend exits
  and the user must relaunch it. The status file remains so the new
  process surfaces the terminal result when it boots. Documented as a
  known limitation in UPGRADING.md.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import time

from app.__version__ import __version__ as CURRENT_VERSION
from app.upgrade import runner, status
from app.upgrade.runner import UpgradeEvent


def _make_callback():
    """Build the ``UpgradeEvent`` → status-file callback.

    Each event becomes a header line ``[<phase>] <message>`` plus a
    phase update so the renderer can render summary text without
    parsing log lines.
    """

    def _cb(event: UpgradeEvent) -> None:
        # Map runner kinds onto the broader status phase vocabulary.
        # ``check`` / ``backup`` / ``install`` / ``migrate`` / ``health``
        # / ``rollback`` are passed through; ``done`` and ``error`` are
        # handled by the caller via :func:`status.finish`.
        if event.kind in ("done", "error"):
            # finish() is called after apply() returns; don't double-mark.
            status.append_log(f"[{event.kind}] {event.message}")
            return
        status.update_phase(event.kind, event.message, ok=event.ok)

    return _cb


def _kill_parent(pid: int) -> None:
    """Best-effort kill of the supervised backend process.

    Used to trigger a Docker entrypoint restart (which re-runs
    migrations idempotently and serves the new wheel). On a host with
    no supervisor, this leaves the backend down — see module docstring.
    """
    if pid <= 0:
        return
    try:
        if sys.platform == "win32":
            # Windows: no SIGTERM semantics; the entrypoint shell will
            # exit when the openpa.exe process dies. ``taskkill /F``
            # would be cleaner but adds a subprocess dependency; the
            # Python-native path below works for both PIDs.
            os.kill(pid, signal.SIGTERM)
        else:
            os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        # Parent already gone (e.g., user killed openpa serve between
        # apply call and restart). Status file already reflects success,
        # so the new boot will pick it up regardless.
        pass
    except PermissionError:
        # Should only happen if the API runs as a different user than
        # the backend, which is not a supported configuration. Log it
        # for the upgrade.log; the upgrade itself already succeeded.
        status.append_log(
            f"[restart] could not signal pid {pid}: permission denied; "
            "restart the backend manually to load the new version."
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="openpa-upgrade-detached")
    parser.add_argument(
        "--parent-pid",
        type=int,
        default=0,
        help=(
            "PID of the HTTP server that should be signalled after a "
            "successful upgrade so its supervisor relaunches it on the "
            "new wheel. 0 = don't kill anything."
        ),
    )
    args = parser.parse_args(argv)

    # Resolve the target version from the runner's check pass; we
    # need it for the begin() banner. ``check`` is cheap and we want
    # the failure mode "GitHub unreachable" to land in the status file
    # too, not be swallowed by a startup crash.
    callback = _make_callback()
    release, check_status = runner.check(callback=callback)
    target = release.version if release else "unknown"
    status.begin(current_version=CURRENT_VERSION, target_version=target)

    if check_status != "available":
        # Nothing to do — record the reason and exit cleanly. The API
        # surfaces this as ``phase=done, ok=true`` (up_to_date) or
        # ``phase=failed`` (unreachable / too_old).
        ok = check_status == "up_to_date"
        status.finish(
            ok=ok,
            exit_code=0 if ok else 1,
            error=None if ok else f"upgrade not applicable: {check_status}",
        )
        return 0 if ok else 1

    try:
        success = runner.apply(callback=callback)
    except Exception as e:  # noqa: BLE001
        status.finish(ok=False, exit_code=1, error=str(e))
        return 1

    if not success:
        status.finish(ok=False, exit_code=1, error="upgrade rolled back")
        return 1

    # Tell the operator what's about to happen before we yank the
    # parent out from under the API request that triggered us. The
    # client may still be polling /status when the SIGTERM lands.
    status.update_phase("restart", "Restarting backend on the new version...")
    print(f"[detached] T={time.time():.3f} restart phase set", flush=True)

    # Small delay so the API's most-recent /status response reaches the
    # client before the listener dies — without this, the SSE/poll just
    # sees ECONNREFUSED with no warning, and the renderer has to guess
    # what happened.
    print(f"[detached] T={time.time():.3f} sleeping 2s pre-finish", flush=True)
    time.sleep(2)

    # Mark done BEFORE killing the parent. Two reasons:
    #   1. Under Docker, the parent kill triggers a container restart;
    #      Docker SIGKILLs the detached process when the container exits,
    #      so a finish() that ran *after* the kill could be cut short and
    #      leave the file at phase="restart" forever.
    #   2. The new boot's clear_if_terminal preserves recently-finished
    #      states (see status.TERMINAL_GRACE_S), so the renderer's first
    #      poll after reconnect still sees "done" and transitions the UI.
    print(f"[detached] T={time.time():.3f} calling status.finish(ok=True)", flush=True)
    status.finish(ok=True, exit_code=0)
    print(
        f"[detached] T={time.time():.3f} sending SIGTERM to pid {args.parent_pid}",
        flush=True,
    )
    _kill_parent(args.parent_pid)
    print(f"[detached] T={time.time():.3f} kill returned", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

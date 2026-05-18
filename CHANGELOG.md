# Changelog

All notable changes to OpenPA are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); version numbers
follow [SemVer](https://semver.org/) within the `0.x` line (breaking
changes can land between minors until `1.0`).

The source of truth for the current version is
[`app/__version__.py`](app/__version__.py). The release process is
documented in [RELEASING.md](RELEASING.md); upgrade guidance for
operators is in [UPGRADING.md](UPGRADING.md).

## [Unreleased]

<!--
Add bullets under one of:
  Added / Changed / Deprecated / Removed / Fixed / Security
  Schema       — for Alembic revisions and any DB shape change
  Compatibility — when MIN_COMPATIBLE_UI or MIN_SUPPORTED_UPGRADE_FROM
                  changes, or when an Electron shell built on this
                  version requires a backend ≥ X. See UPGRADING.md
                  for why this matters for Electron users.
Move to a dated section on release.
-->

## [0.1.9] — TBD

### Added
- Tray icon menu, Windows taskbar jumplist, and macOS dock menu now
  surface direct shortcuts to **Process Manager**, **Events**, and
  **Channels**. Clicking an entry opens the page in a new window, or
  focuses the existing window if one is already on that route — unlike
  the always-new-window behavior of "Open Main Page" / "Open Settings".
- Capability-gating for the tray / jumplist / dock entries above. The
  backend now advertises an ``ui_features`` list in
  ``/api/services/capabilities``; the Electron shell only surfaces an
  entry whose name appears in that list. When the field is absent
  (older pinned wheel predating this protocol) the gated entries are
  hidden, since pre-protocol backends also lack the matching SPA
  routes — clicking would otherwise land on the fallback page.
- Dev-channel forced-available upgrade: on `OPENPA_UPGRADE_CHANNEL=dev`,
  `/api/upgrade/check` synthesises a "newer" release without hitting
  GitHub, and the runner skips `pip install` so the in-app updater UI
  can be tested end-to-end against a working-copy install without
  modifying the editable install. See [UPGRADING.md](UPGRADING.md).
- Unified in-app upgrade UX: one "Update available" card on the banner
  and in Settings → Updates, one Update button, with a live-streaming
  progress modal. Works in both the Electron desktop app and the web
  UI. No more manual `openpa upgrade -y` step.
- `POST /api/upgrade/apply`, `GET /api/upgrade/status`, and
  `GET /api/upgrade/stream` for web-UI users: the server spawns a
  detached upgrade runner that writes progress to
  `~/.openpa/.upgrade.status.json`, kills the parent process on
  success, and relies on the supervisor (Docker / Electron / systemd)
  to relaunch the new wheel. Endpoints are auth-gated; `/check`
  remains public.
- Install catalog: structured registry of deployment/install/service modes
  consumed by the Setup Wizard. Source: [`install/catalog.toml`](install/catalog.toml),
  generated copies in [`install/_catalog.{json,ps1,sh}`](install/),
  Python loader at [`app/config/install_catalog.py`](app/config/install_catalog.py).
- Setup progress stream so the Setup Wizard can show live step status
  instead of polling.
- Provisioner refactor: setup logic moved out of the installer scripts
  into the running server so installer scripts stay thin.
- Pre-commit hook configuration.
- `--channel` / `-Channel` flag unified across `install.sh` and
  `install.ps1` for selecting production vs. test release feeds.
- Per-channel Docker image strategy: test installs pull
  `openpa/openpa-desktop:<version>.dev1`, production pulls `:latest`.

### Fixed
- Electron jumplist click no longer kills the backend. Clicking any
  Windows taskbar jumplist entry ("Open Main Page", "Process Manager",
  …) re-invokes `OpenPA App.exe` with `--open=<target>`; the second
  instance fails the single-instance lock and calls `app.quit()`. The
  top-level `before-quit` handler then ran inside that doomed
  secondary process, where `killTrackedProcessesSync()` read
  `~/.openpa/install.pid` (written by the primary) and force-killed
  the primary's `openpa serve` tree with `taskkill /T /F`. Empty
  `~/.openpa/server.err.log` after each kill was the external-SIGKILL
  fingerprint. The lock is now acquired before any quit-time
  handlers register; lifecycle wiring (`window-all-closed`,
  `before-quit`, `second-instance`, `whenReady`) lives only inside
  the primary-instance branch, so a losing secondary quits cleanly
  with no kill handlers attached.
- Tray / jumplist / dock entries for Process Manager, Events, and
  Channels were hidden on every post-setup launch. The Electron main
  process called the admin-gated `/api/services/capabilities` for
  feature gating, but it can't share the renderer's session cookies,
  so once `is_setup_complete()` flipped to true the endpoint returned
  401 and `uiFeatures` stayed `null`. Added a deliberately-public
  sibling, `/api/services/tray-capabilities`, that returns only
  `install_mode` + `ui_features` (route-name metadata with no
  security value); the richer capabilities endpoint stays
  admin-gated for the Setup Wizard's deeper payload. Fresh installs
  appeared to work only because setup hadn't completed yet — they
  would have broken the gate as soon as an admin password got set.
- Upgrade flow on Windows: `_backup_sqlite` no longer leaks SQLite
  connections, so the `*.sqlite.gz.tmp` snapshot file can be unlinked
  after gzipping. The previous code wrapped `sqlite3.connect(...)` in a
  `with` block that only manages the transaction — the connection (and
  its OS file handle) stayed open until GC, and NTFS refused the
  `finally`-block `unlink()` with `WinError 32`, aborting every in-app
  upgrade at the `[backup]` step. The connections are now wrapped in
  `contextlib.closing(...)` so `.close()` runs on scope exit. Linux and
  macOS were unaffected (they happily unlink open files).
- Settings → Updates → "Release channel" now shows the actual install
  channel (`production` / `test` / `dev`) baked in at build time. It
  previously read a stale ``runtimeConfig.channel`` field that was
  never connected to ``INSTALL_CHANNEL`` and always displayed
  ``stable`` regardless of how the Electron app was built. The
  config field is force-overwritten from ``INSTALL_CHANNEL`` on each
  launch so a re-installed test app never inherits a previous build's
  persisted value.
- Notifications popover in the sidebar no longer opens in the
  top-left corner of the viewport. The `<ElPopover>`'s `#reference`
  slot nested an `<ElTooltip>` around the trigger row, which broke
  ElPopover's Popper.js reference resolution and pinned the popover
  to the document origin (0,0). Restructured so the tooltip wraps
  the row directly and the popover anchors to it via `:virtual-ref`
  + `virtual-triggering`, matching the sibling-pattern already used
  by Settings / Process Manager / Events / Channels rows.
- In-app upgrade on the Windows `test` channel: pip install no
  longer fails at the `[install]` step with `WinError 32` renaming
  `openpa.exe` → `openpa.exe.deleteme`. The Electron shell was
  spawning both the long-running backend (`openpa.exe serve`) and
  the upgrade subprocess itself (`openpa.exe upgrade apply --yes`)
  through the venv's console-script wrapper, so Windows held the
  executable open for pip's lifetime — even pip's own ``.deleteme``
  rename fallback was refused (the handle was held without
  ``FILE_SHARE_DELETE``). Both spawns now go through
  ``<venv>/Scripts/python.exe -m app.cli.main <args>``;
  ``python.exe`` is untouched by ``pip install --upgrade openpa``,
  so the lock disappears structurally. The web-UI upgrade path
  (`POST /api/upgrade/apply`) is unblocked as a side effect, since
  the only running `openpa.exe` consumer (the backend) is gone.
  Complements the ``v0.1.9-test15`` fix, which addressed a different
  Windows file-lock (the SQLite backup) on the same upgrade flow.

### Changed
- The Settings → Updates page and UpdateBanner no longer distinguish
  "backend" from "desktop app" updates; both surface as a single
  "OpenPA vX → vY available" with one Update button. The
  copy-the-command UX (`openpa upgrade -y`) is removed from the UI;
  the CLI remains documented in [UPGRADING.md](UPGRADING.md) as a
  fallback for headless / SSH-only operators.
- Docker bundle is always regenerated on install (previously reused
  stale config in some paths).
- `release-test.yml` polls the PyPI simple index rather than the JSON
  API for wheel readiness, and pins the openpa wheel by URL to avoid
  Test PyPI pollution.

### Schema
- No schema changes. Alembic baseline (`20260509_baseline`) remains the
  only migration; existing 0.1.7 installs upgrade in place with no
  data-migration steps required.

## [0.1.7] — 2026-05-11

### Added
- `CONTRIBUTING.md`.
- UI source merged into this repo under [`ui/`](ui/); a single version
  number now drives backend, frontend, and Electron app.
- Release-discipline CI: prod tag must match `app/__version__.py` and
  must promote a commit that already carries a `v*-test*` tag.

### Changed
- `install.ps1` writes its log as UTF-8 (was UTF-16 LE) and adds a
  UTF-8 BOM for Windows PowerShell 5.1 console output.
- `install.ps1` runs the `uv` installer in a child `powershell.exe` to
  sidestep PS 5.1 native-stderr abort behavior.

### Schema
- No schema changes.

## [0.1.4] — 2026-05-10

### Added
- `container` deployment option for running inside Docker / Podman.
- `openpa` CLI rename (was previously a different entry point); `uv`
  bootstrap auto-installs Python and puts `openpa` on `PATH`.
- `~/.openpa/activate.sh` generated on install; activation hint
  shortened to a one-liner.
- Pip cache scoped to `~/.openpa/pip-cache`.

### Changed
- Migrate errors are surfaced loudly instead of being swallowed.
- `app.storage` shipped in the wheel (previously gitignored at the
  package layer).

### Schema
- No schema changes (pre-Alembic; install was still bootstrap-only).

## [0.1.1] — 2026-05-09

### Added
- Initial public release with backend (`openpa serve`), CLI, SQLite
  storage, Setup Wizard, and a single-file `install.sh` / `install.ps1`
  bootstrap.

[Unreleased]: https://github.com/openpa/openpa/compare/v0.1.9...HEAD
[0.1.9]: https://github.com/openpa/openpa/compare/v0.1.7...v0.1.9
[0.1.7]: https://github.com/openpa/openpa/compare/v0.1.4...v0.1.7
[0.1.4]: https://github.com/openpa/openpa/compare/v0.1.1...v0.1.4
[0.1.1]: https://github.com/openpa/openpa/releases/tag/v0.1.1

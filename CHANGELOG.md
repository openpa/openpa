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
Add bullets under one of: Added / Changed / Deprecated / Removed /
Fixed / Security / Schema. Move to a dated section on release.
-->

## [0.1.9] — TBD

### Added
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

### Changed
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

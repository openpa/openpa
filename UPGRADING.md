# Upgrading OpenPA

This file tells operators what they need to do to move an existing
install from one OpenPA version to the next. For the changelog of what
shipped in each version, see [CHANGELOG.md](CHANGELOG.md). For how
maintainers cut a release, see [RELEASING.md](RELEASING.md).

## The short version

For most releases you do not need to do anything special:

```powershell
openpa upgrade            # interactive: confirms, then runs the flow
openpa upgrade --yes      # non-interactive
openpa upgrade check      # what's available, no changes
```

`openpa upgrade apply` (the default) runs this sequence:

1. **Backup.** SQLite installs get a gzipped file copy under
   `~/.openpa/backups/`. Postgres installs get a `pg_dump`.
2. **Install.** The new wheel is installed into `~/.openpa/venv`
   (native) or pulled as a new image tag (Docker).
3. **Migrate.** Alembic applies any new revisions in
   [`app/alembic/versions/`](app/alembic/versions/) to the live
   database.
4. **Health-check.** The new build is started briefly and probed.
5. **Commit or roll back.** On any failure the package is downgraded
   and the database restored from the backup taken in step 1. The lock
   file at `~/.openpa/.upgrade.lock` lets a subsequent boot finish a
   recovery that was interrupted mid-flight.

The Docker desktop image runs `openpa db upgrade` from its entrypoint
on every container start, so `docker pull openpa/openpa-desktop:latest`
followed by `docker compose up` is sufficient for Docker installs.

## Upgrading the Electron desktop app

An Electron install is **two artifacts on different upgrade tracks**:

- The **Electron shell** (window, tray, auto-launch, bundled web UI)
  updates itself through `electron-updater`. At app launch it checks
  the `latest*.yml` manifests attached to the GitHub Release; if a
  newer version is available, it downloads in the background and
  installs on the next app restart. No user action.
- The **Python backend** (`openpa serve`, spawned as a subprocess by
  the shell — see [`ui/electron/main.ts`](ui/electron/main.ts)) is the
  same `~/.openpa/venv` install any non-Electron user has. It is
  **not** updated by `electron-updater`. It updates only when the user
  runs `openpa upgrade` in a terminal.

The in-app banner from `/api/upgrade/check` surfaces both: it tells the
user when a newer backend is available and prints the exact command to
run. Until that command runs, the new UI is talking to the old backend.

### What to do on each release

The Electron shell ships an in-app **Apply now** button on the
"backend update available" banner. Clicking it opens a modal that
runs `openpa upgrade --yes` under the shell, streams the output, and
restarts the backend on success — no terminal needed.

1. **After Electron auto-updates the shell, restart the app once.**
   electron-updater applies the update at quit; until you quit and
   relaunch, you're still running the old shell — and on a brand-new
   shell version, the *previous* shell does not yet carry the in-app
   apply button. The first upgrade after adopting this build still
   needs the manual `openpa upgrade -y` step; every subsequent one is
   one click.
2. **Click "Apply now" in the banner.** Leave the modal open until it
   reports "Upgrade complete." Do not close OpenPA while the upgrade
   is in progress — quitting mid-flight leaves a lock file behind and
   the next launch will roll back from the captured backup.
3. **Manual fallback.** If the modal reports an error, or you prefer
   the CLI, open a terminal and run `openpa upgrade -y` followed by
   restarting the Electron app.

### When shell and backend versions disagree

The two artifacts can drift because their upgrade triggers are
independent. The combinations:

| Shell | Backend | Behaviour |
|---|---|---|
| new | new | Normal. |
| old | old | Normal — no upgrade attempted yet. |
| old | new | Usually fine. The backend's `MIN_COMPATIBLE_UI` floor (see below) will reject genuinely stale shells. |
| new | old | The risky case. New UI may call endpoints the old backend doesn't have. If the release bumped `MIN_COMPATIBLE_UI`, the banner blocks the UI with "upgrade backend"; otherwise individual features can silently 404 until the user runs `openpa upgrade`. |

The per-version notes below call out releases where this drift matters.

## Version floor

This build refuses to upgrade an install older than
`MIN_SUPPORTED_UPGRADE_FROM` in [`app/__version__.py`](app/__version__.py)
(currently `0.1.0`). If your install is older than the floor, upgrade
first to the floor version, then to current.

The UI has a parallel floor (`MIN_COMPATIBLE_UI`). A web UI or Electron
client older than that shows an "upgrade required" banner against this
backend.

## Rolling back

The upgrader rolls back automatically on failure, but if you want to
roll back a *successful* upgrade:

```powershell
# 1. Stop the service.
# 2. Reinstall the previous wheel.
pip install --force-reinstall openpa==<previous-version>
# 3. Restore the most recent pre-upgrade snapshot.
openpa db restore ~/.openpa/backups/<timestamp>.sqlite.gz
# 4. Start the service.
```

Same flow on Docker, with `docker pull openpa/openpa-desktop:<previous>`
in place of `pip install`.

The forward migration's `downgrade()` is **not** used here — `db
restore` reverts the database wholesale to its pre-upgrade state, which
is the only safe option once the new build has been writing to it.

## Per-version notes

Sections below cover only the upgrade-relevant changes for each release
— behaviour changes operators need to know about, manual steps, and
schema work. Full release notes are in [CHANGELOG.md](CHANGELOG.md).

### Upgrading to 0.1.9 (from 0.1.7)

- **No manual steps required.** No schema changes; no breaking config
  changes; data layout under `~/.openpa/` is unchanged.
- **Install catalog refresh.** The Setup Wizard reads a new catalog
  format on next launch; existing installs do not need to re-run setup.
- **Docker users.** Image pulls now follow a per-channel naming scheme
  (`:latest` for prod, `:<version>.dev1` for test). If you previously
  pinned to a `dev` tag, switch to `:latest` or to an explicit
  `:<version>` pin before upgrading.

### Upgrading to 0.1.7 (from 0.1.4)

- **No manual steps required.**
- **Windows installer logging.** `install.log` is now UTF-8. If you
  have tooling that reads it as UTF-16, update the encoding.
- **UI is now in this repo.** Has no effect on operators of installed
  builds; only relevant to contributors.

### Upgrading to 0.1.4 (from 0.1.1)

- **CLI rename.** The entry point is now `openpa`. If you have scripts
  calling the old name, update them.
- **Activation.** After install, the activation hint is a one-liner
  printed at the end of `install.sh` / `install.ps1`. Existing
  `~/.openpa/activate.sh` from earlier installs continues to work; new
  installs generate it.
- **Pre-Alembic database.** This install path predates Alembic. After
  upgrading to a build that ships Alembic (0.1.5+), the first boot
  stamps the existing DB at the baseline revision and then runs any
  pending revisions. There is no manual `db stamp` step.

## When a release has breaking changes

A release with manual steps will say so in CHANGELOG.md under a
**Breaking** subsection and will have a dedicated subsection in this
file. The release notes on the GitHub release page will link here.

For schema changes that need data backfill, the work is done inside the
Alembic revision (`op.execute(...)`), so `openpa db upgrade` is still
the only operator-side action.

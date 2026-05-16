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

## Upgrading via the in-app Update button

Both the Electron desktop app and the Web UI now ship a one-click
**Update now** button — on the top banner when a new version is
detected, and in **Settings → Updates** at any time. Clicking it
opens a progress modal that:

1. Backs up your database (SQLite copy or `pg_dump`).
2. Installs the new wheel and runs Alembic migrations.
3. Streams live progress to the modal so you can see what's happening.
4. Restarts the backend on the new version (via Docker's restart
   policy, the Electron shell's supervisor, or systemd).
5. Reports "Update complete" — or rolls back to the previous version
   automatically on any failure.

No terminal needed. No commands to copy. **Don't close OpenPA while
the upgrade is running** — quitting mid-flight leaves the lock file
behind and the next launch will roll back from the captured backup.

### Architecture: one version, two delivery channels

A single release tag (`v0.1.9`) ships:

- A **Python wheel** (PyPI / `~/.openpa/venv`) — the backend.
- An **Electron installer** (GitHub Releases) — the desktop shell,
  with the SPA bundled inside.

The two arrive on independent triggers:

| Channel | Delivers | Trigger |
|---|---|---|
| `electron-updater` | Electron shell + bundled SPA | App launch; downloads in background; installs on next quit |
| `openpa upgrade` / `POST /api/upgrade/apply` | Python wheel + DB migrations | User clicks Update Now, or runs the CLI |

When you click **Update now**, both tracks run in parallel: the
Electron shell starts downloading, the backend upgrade runs in a
detached subprocess, and the modal reconciles them. If the shell
update finished but the backend hadn't yet, the modal prompts you to
restart once both are settled.

### Shell-only vs backend-only updates

The unified UI hides which component changed: you see "OpenPA vX → vY
available — Update now." Whether the diff is shell, backend, or
both, you take the same action.

### Testing the in-app updater on the `dev` channel

For a working-copy / `uv sync` dev install, the channel is set in the
repo-root [`.env`](.env) (`OPENPA_UPGRADE_CHANNEL=dev`) — the file
[`app/config/settings.py`](app/config/settings.py) loads it at startup
via `load_dotenv("app/../.env")`. That `.env` is the dev-environment
config; `~/.openpa/.env` is only relevant for installed (non-dev)
hosts and should not be edited to switch channels.

On the `dev` channel, the `/api/upgrade/check` endpoint always reports
an update available, and clicking **Update now** exercises the full
flow (backup → migrate → restart) **without running `pip install`**.
The synthetic target version is the running version plus a `+devforced`
PEP 440 local suffix.

This exists so a contributor can test the in-app updater UI against a
working-copy install — there is no real "newer wheel" to publish for a
dev install, so without the synthesis the Update button would never
appear. The banner reappears after every restart on dev; that is the
expected behaviour, not a bug.

Production and test installs are unaffected: only `dev` channel
short-circuits the GitHub lookup.

### Manual CLI fallback (headless / SSH-only)

Operators running OpenPA on a remote host without a graphical
session can still upgrade from the command line:

```powershell
openpa upgrade            # interactive: confirms, then runs the flow
openpa upgrade --yes      # non-interactive
openpa upgrade check      # what's available, no changes
```

The CLI runs the same backup → install → migrate → health flow the
in-app button uses. After it completes, restart the backend manually
(`systemctl restart openpa`, `docker compose restart`, etc.) so the
new wheel is loaded.

### When shell and backend versions disagree

Even with the unified UI, the two artifacts can drift in time because
their upgrade triggers are independent. The combinations:

| Shell | Backend | Behaviour |
|---|---|---|
| new | new | Normal. |
| old | old | Normal — no upgrade attempted yet. |
| old | new | Usually fine. The backend's `MIN_COMPATIBLE_UI` floor (see below) will reject genuinely stale shells. |
| new | old | The risky case. New UI may call endpoints the old backend doesn't have. If the release bumped `MIN_COMPATIBLE_UI`, the banner blocks the UI with "upgrade backend"; otherwise individual features can silently 404 until the user clicks Update Now. |

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
- **Unified update UI.** The Settings → Updates page and the top
  banner now show one "OpenPA update available" card with a single
  **Update now** button, instead of separate "Backend update" / "Desktop
  app update" sections. The button does the right thing regardless of
  whether the change is the shell, the backend, or both. Web UI users
  also get the button — they used to see only a copy-the-command block.
- **No more manual `openpa upgrade -y`.** The command still exists for
  headless / SSH-only operators (see the section above), but it's no
  longer surfaced in the UI.
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

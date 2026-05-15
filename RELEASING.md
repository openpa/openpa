# Releasing OpenPA

OpenPA's Python wheel (PyPI), Electron app (GitHub Releases installers
+ auto-update manifests), and Docker image (`openpa/openpa-desktop` on
Docker Hub) all ship from this single repo. Every production release is
the promotion of a tested commit — there's no separate "test" and "prod"
pipeline. CI enforces this.

## Version source

[`app/__version__.py`](app/__version__.py)'s `__version__` field is the
sole source. Every other version reference derives from it:

- `pyproject.toml` reads it via `[tool.hatch.version] path = "app/__version__.py"`.
- `ui/package.json` is regenerated from it by
  [`scripts/sync_ui_version.py`](scripts/sync_ui_version.py), which runs
  as the `predev`/`prebuild`/`preweb:dev`/`preweb:build` npm hook. You
  never edit `ui/package.json`'s version manually. When you bump
  `app/__version__.py`, run the sync script once and commit both files
  together so the committed state is internally consistent (CI re-syncs
  on every build either way, so this is hygiene, not a functional
  requirement).

## Day-to-day: working on a feature

One branch, one PR — backend, frontend, or both.

```powershell
git checkout -b my-feature
# edit anywhere under app/ or ui/src
git push -u origin my-feature
```

Open a pull request. [`.github/workflows/pr.yml`](.github/workflows/pr.yml)
runs two jobs in parallel:

- `backend` — pytest under Python 3.13
- `ui` — `vue-tsc --noEmit` + `npm run web:build` smoke

Merge when green.

### Running locally

| Goal | Commands |
|---|---|
| Backend only | `pip install -e .` then `openpa serve`. UI listener silently disabled because `app/static/ui/` is empty. |
| UI only | `cd ui ; npm run web:dev`. Talks to whatever backend URL is in the wizard or `VITE_AGENT_URL`. |
| Full local | Terminal A: `openpa serve` on `:1112`. Terminal B: `cd ui ; npm run web:dev` on `:1515`. The port-swap heuristic in `ui/src/services/runtimeConfig.ts` resolves the API URL automatically. |
| Bundled smoke | `bash scripts/build_ui.sh ; openpa serve` — SPA served from `app/static/ui/` on `:1515`. |
| Electron dev | `cd ui ; npm run dev` |

## Preparing a feature for release

Before you bump the version, walk through this checklist. Which steps
apply depends on what the feature touches — UI, backend, or database.
Operator-facing guidance for these same scenarios lives in
[UPGRADING.md](UPGRADING.md); the two files should always agree.

### Always

1. **Update [CHANGELOG.md](CHANGELOG.md).** Move bullets from `[Unreleased]`
   into a new `## [X.Y.Z] — TBD` section, or add bullets there if the
   section already exists. Use the existing category headers (Added /
   Changed / Fixed / Schema / Compatibility).
2. **Update [UPGRADING.md](UPGRADING.md).** Add a per-version subsection
   under "Per-version notes" — even if the body is just "No manual
   steps required." Operators value the explicit confirmation; an
   empty section reads as "I forgot to write this," not "nothing
   changed."

### UI-only change (no new or changed backend API)

Nothing extra. The Electron shell auto-updates via electron-updater;
the bundled SPA ships inside the wheel and via the desktop installer
in lockstep with the rest of the release.

### UI change that calls a new or changed backend API

The risk is an Electron user whose shell auto-updated but whose backend
hasn't been upgraded yet. The new UI hits a route the old backend
doesn't have. To handle it:

1. **Bump `MIN_COMPATIBLE_UI`** in [`app/__version__.py`](app/__version__.py)
   to this release's version. The old backend will then refuse the new
   UI and the UpdateBanner shows "upgrade backend" instead of letting
   individual features silently 404.
2. **Add a `Compatibility` bullet to CHANGELOG.md** for this release
   documenting the bumped floor.
3. **No reverse check is needed** for the common case — the in-app
   "Apply now" flow in [`ui/src/components/UpdateBanner.vue`](ui/src/components/UpdateBanner.vue)
   prompts the user to run the backend upgrade as soon as the
   blocking banner appears. Web-UI / non-Electron users still need to
   run `openpa upgrade -y` manually; the banner spells it out.

### Schema change

1. **Edit the ORM model** in [`app/storage/models.py`](app/storage/models.py).
   Never write raw `CREATE TABLE` outside of an Alembic revision.
2. **Generate the migration:**
   ```
   openpa db revision --autogenerate -m "short_description"
   ```
   (or whatever the project's wrapper command is — check
   [`app/cli/commands/db.py`](app/cli/commands/db.py).) The new
   revision lands in [`app/alembic/versions/`](app/alembic/versions/).
3. **Review the generated revision by hand.** Alembic autogenerate
   misses or mis-handles:
   - **Column renames** — sees them as drop + add, which is data loss.
     Fix to `op.alter_column(..., new_column_name=...)`.
   - **`server_default` changes**.
   - **Check constraints** and **enum value additions**.
   - **Index renames**.
4. **Prefer additive shapes within one release.** Add nullable columns
   or new tables rather than dropping or renaming. If a destructive
   change is unavoidable, split it across two releases (release N
   adds the new shape and dual-writes; release N+1 drops the old
   shape) so a rollback from N+1 → N still works.
5. **Backfill inside the migration**, not in application code. Use
   `op.execute(...)` or a batched UPDATE. Application-level backfill
   races with rolling restarts that haven't yet picked up the new
   wheel.
6. **Test the upgrade from a real older install**, not just a fresh
   DB. Boot a `0.1.x` SQLite install (and a Postgres install if you
   support it), then `openpa upgrade -y` to your candidate; the
   first-time path through `compat_preflight` only fires on real
   pre-Alembic data.
7. **Add a `Schema` bullet to CHANGELOG.md** describing the migration
   in one line.
8. **Add an UPGRADING.md note** if the migration takes meaningful
   downtime, requires action, or has a non-obvious failure mode.

### When in doubt

If a change might trip operators, ask: "Will a user with a fresh
install at the previous version succeed at `openpa upgrade -y`
without reading anything?" If the answer isn't a confident yes, the
feature needs an UPGRADING.md subsection.

## Release cycle

A release cycle covers ONE version. It starts when you decide to ship
0.1.8 and ends when `v0.1.8` is on PyPI and GitHub Releases. There is no
"skip testing" path — see [CI enforcement](#ci-enforcement) below.

### Cycle steps (shipping 0.1.8)

1. **Bump the version once, at the start.** On `main`:

   ```powershell
   git checkout main ; git pull
   # edit app/__version__.py:  __version__ = "0.1.8"
   python scripts/sync_ui_version.py        # mirrors into ui/package.json
   git add app/__version__.py ui/package.json
   git commit -m "Bump version to 0.1.8"
   git push
   ```

2. **Tag the first test release.**

   ```powershell
   git tag v0.1.8-test1
   git push origin v0.1.8-test1
   ```

   [`release-test.yml`](.github/workflows/release-test.yml) builds the
   wheel as `0.1.8.dev1`, publishes to Test PyPI, attaches a GitHub
   prerelease.

3. **Verify.** Install from Test PyPI, run, exercise the change. Two
   options depending on how much of the install path you want to
   exercise:

   - **Direct pip** (quickest, skips the installer scripts):

     ```powershell
     pip install --index-url https://test.pypi.org/simple/ --pre openpa==0.1.8.dev1
     ```

   - **Through the installer scripts** (covers the path end users will
     take when the wheel is promoted to prod):

     ```bash
     bash install/install.sh --channel test --deployment local
     ```

     ```powershell
     .\install\install.ps1 -Channel test -Deployment local
     ```

     The installer locates the latest `.devN` wheel on Test PyPI,
     installs it into `~/.openpa/venv` (or builds the Docker bundle
     with Test-PyPI indexes if you pass `--mode docker`), and stamps
     `OPENPA_UPGRADE_CHANNEL=test` into `~/.openpa/.env` so the
     upgrader queries the right channel. By default the test install
     shares `~/.openpa` with prod — set
     `OPENPA_WORKING_DIR=~/.openpa-test` to keep them side-by-side.

4. **Iterate.** Bugs found? Fix on `main`, push fixes, then tag the next
   iteration on the new `HEAD`:

   ```powershell
   git tag v0.1.8-test2
   git push origin v0.1.8-test2
   ```

   `app/__version__.py` stays at `0.1.8` throughout the cycle — you only
   change code, never the version.

5. **Promote.** When the final test build is good, tag the **same
   commit** as production and push:

   ```powershell
   git tag v0.1.8                          # at the green commit
   git push origin v0.1.8
   ```

   [`release.yml`](.github/workflows/release.yml) fires.

### What ships on `v0.1.8`

The release workflow runs six jobs:

1. **`verify`** — CI gates described below.
2. **`prepare-draft`** — creates a draft GitHub Release with
   auto-generated notes (PR titles since the previous tag).
3. **`wheel`** (ubuntu) — builds the SPA via `scripts/build_ui.sh`, runs
   `hatch build`, smoke-tests, publishes to PyPI, uploads `.whl` +
   `.tar.gz` to the draft.
4. **`electron`** matrix (windows / macos / ubuntu) — `npm run build` in
   `ui/`. electron-builder publishes installers + `latest*.yml` update
   manifests to the same draft.
5. **`docker`** (ubuntu) — builds `Dockerfile.desktop` with
   `OPENPA_PIP_SPEC=openpa==0.1.8` and pushes
   `openpa/openpa-desktop:0.1.8` and `openpa/openpa-desktop:latest` to
   Docker Hub. Heavy base layers (Ubuntu + XFCE + Chrome + Python +
   Docker CLI) come from BuildKit GHA cache; only the small per-version
   pip layer is built and uploaded.
6. **`publish`** — promotes the draft to public, but **only** after all
   of the above succeed.

The promoted release contains:

- `openpa-0.1.8-py3-none-any.whl` + `openpa-0.1.8.tar.gz` (PyPI + GH)
- `OpenPA App-Windows-<version>-Setup.exe` + `latest.yml`
- `OpenPA App-Mac-<version>-Installer.dmg` + `latest-mac.yml`
- `OpenPA App-Linux-<version>.AppImage` + `latest-linux.yml`
- `openpa/openpa-desktop:0.1.8` and `openpa/openpa-desktop:latest` on
  Docker Hub

The test workflow (`release-test.yml`) emits a parallel set:
`openpa==0.1.8.dev1` on Test PyPI plus `openpa/openpa-desktop:0.1.8.dev1`
on Docker Hub (no `:latest`).

## CI enforcement

The `verify` job in [`release.yml`](.github/workflows/release.yml) runs
first and refuses to proceed unless **both** of these hold:

1. **Tag matches `app/__version__.py`.** Tag `v0.1.8` requires
   `__version__ = "0.1.8"`. Mismatch ⇒ fail with the exact diff.
2. **A `v0.1.8-test*` tag exists at the same commit.** The prod tag
   must promote a tested commit. If it points to a fresh commit with
   no matching test tag, ⇒ fail.

Failure happens before any artifact is built or the draft is created,
so a misconfigured release leaves no debris.

[`release-test.yml`](.github/workflows/release-test.yml) enforces only
the first check — test releases don't need to be promoted from anywhere,
they just need their version string to agree with `app/__version__.py`.

## Hotfixes

A hotfix uses the same cycle with a four-segment version. For a hotfix
to `0.1.8`:

```powershell
# bump app/__version__.py:  __version__ = "0.1.8.1"
git add app/__version__.py
git commit -m "Hotfix: <what you fixed>"
git push

git tag v0.1.8.1-test1
git push origin v0.1.8.1-test1
# verify, iterate, promote with v0.1.8.1
```

PEP 440 accepts the four-segment form; hatchling and the upgrade
manifest both handle it.

## When a release fails

The draft stays hidden until every job succeeds, so partial failure has
no user impact. To retry:

```powershell
gh release delete v0.1.8 --cleanup-tag --yes
# fix the underlying issue, push to main
# re-tag a test cycle if the fix could change behavior:
git tag v0.1.8-testN+1 ; git push origin v0.1.8-testN+1
# once green again:
git tag v0.1.8 ; git push origin v0.1.8
```

The PyPI wheel is the exception — once uploaded to PyPI it stays there.
If the wheel job succeeded but Electron failed, either retag at a higher
patch version, or `pip uninstall openpa==0.1.8` on the clients you
control.

## Code-signing

Electron installers are signed only when these repo Actions secrets are
present. Missing secrets produce **unsigned** binaries — build still
succeeds, but users see SmartScreen / Gatekeeper warnings.

- `CSC_LINK` — base64-encoded code-signing cert (Windows + macOS)
- `CSC_KEY_PASSWORD`
- `APPLE_ID`, `APPLE_APP_SPECIFIC_PASSWORD`, `APPLE_TEAM_ID` — macOS
  notarization

The per-job gating logic is in
[`.github/workflows/release.yml`](.github/workflows/release.yml)'s
"Configure code-signing env" step.

## Docker Hub credentials

The `docker` jobs in both `release.yml` and `release-test.yml` push
to `openpa/openpa-desktop` on Docker Hub. Unlike code-signing, these
secrets are **required** — a missing secret fails the job, which fails
the release.

One-time setup:

1. **Create the repo on Docker Hub.** At
   [hub.docker.com](https://hub.docker.com) → *Create Repository* →
   name `openpa-desktop`, namespace `openpa`, visibility **Public**.
   (If the first push creates it private by accident, unauthenticated
   `docker compose pull` from the installer will fall back to local
   build, which works but takes ~25 minutes.)

2. **Create a Personal Access Token.** Docker Hub → *Account Settings →
   Security → New Access Token*. Scope **Read & Write** (Delete is not
   needed). Name it `openpa-gha-release`. Copy the token string
   immediately — Docker Hub shows it once.

3. **Add the GitHub repo secrets.** Repo → *Settings → Secrets and
   variables → Actions → New repository secret*:

   - `DOCKERHUB_USERNAME` — the Docker Hub account or org username.
     Stored as a secret for log-redaction consistency, even though
     usernames aren't sensitive.
   - `DOCKERHUB_TOKEN` — the PAT from step 2.

Once these are in place, every `v*-testN` and `v*` tag will publish a
matching image. The base layers are cached in GitHub Actions cache under
`scope=desktop`, shared between the prod and test workflows, so each
release rebuilds only the small (~50 MB) final `pip install` layer.

## Cheat sheet

```powershell
# Feature
git checkout -b my-feature
git push -u origin my-feature
# open PR; merge when pr.yml is green

# Release cycle for X.Y.Z (replace 0.1.8 below)
git checkout main ; git pull
# bump app/__version__.py to "0.1.8"
python scripts/sync_ui_version.py
git add app/__version__.py ui/package.json
git commit -m "Bump version to 0.1.8"
git push

git tag v0.1.8-test1 ; git push origin v0.1.8-test1
# verify; iterate as needed:
git tag v0.1.8-test2 ; git push origin v0.1.8-test2
# ...

# Promote: same commit as the green test tag
git tag v0.1.8 ; git push origin v0.1.8

# Hotfix (X.Y.Z.N)
# bump app/__version__.py to "0.1.8.1"
python scripts/sync_ui_version.py
git add app/__version__.py ui/package.json
git commit -m "Hotfix: ..."
git push
git tag v0.1.8.1-test1 ; git push origin v0.1.8.1-test1
# verify, then:
git tag v0.1.8.1 ; git push origin v0.1.8.1
```

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
- `OpenPA Web UI-Windows-0.1.8-Setup.exe` + `latest.yml`
- `OpenPA Web UI-Mac-0.1.8-Installer.dmg` + `latest-mac.yml`
- `OpenPA Web UI-Linux-0.1.8.AppImage` + `latest-linux.yml`
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

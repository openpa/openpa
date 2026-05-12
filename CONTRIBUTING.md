# Contributing to OpenPA

OpenPA's backend (Python) and UI (Vue 3 + Electron) live together in
this repo. A feature that touches both ships from one branch and one
PR. This document covers everyday development; for the release pipeline
see [RELEASING.md](RELEASING.md).

## Repo layout

| Path | What |
|---|---|
| `app/` | Python backend — Starlette server, CLI, agent, tools, skills, channels. |
| `app/__version__.py` | The single source of truth for the project version. |
| `app/static/ui/` | Built SPA artifact (gitignored). Populated by `scripts/build_ui.sh`. |
| `ui/` | Vue 3 + Vite + Electron source. |
| `ui/src/` | Vue components, stores, services. |
| `ui/electron/` | Electron main process + preload. |
| `scripts/build_ui.sh` | Builds the SPA into `app/static/ui/` so the wheel ships with it. |
| `scripts/sync_ui_version.py` | Mirrors `app/__version__.py` → `ui/package.json` (also runs as the `prebuild`/`preweb:build` npm hook). |
| `.github/workflows/` | `pr.yml`, `release.yml`, `release-test.yml`. |
| `pyproject.toml` | Python deps + hatch config. |
| `RELEASING.md` | How to ship a new version. |

## First-time setup

```powershell
# Python deps
uv sync                                  # reads pyproject.toml + uv.lock

# Node deps
cd ui
npm install
cd ..

# Initial config (profile, LLM keys, ports, etc.)
uv run openpa setup
```

`uv run openpa setup` is interactive and writes `~/.openpa/` with your
profile and provider credentials. You only need to run it once unless
you blow away `~/.openpa/`.

## Daily dev — two terminals

The everyday loop: backend on `:1112`, Vite on `:1515`, each side
hot-reloads its own half.

**Terminal A — backend:**

```powershell
uv run openpa serve
```

The API listens on `:1112`. The bundled-SPA listener on `:1515` stays
silent in a dev checkout because `app/static/ui/` is empty — Vite
serves the SPA in Terminal B instead.

**Terminal B — Vite dev server:**

```powershell
cd ui
npm run web:dev
```

Open <http://localhost:1515>. `ui/src/services/runtimeConfig.ts`'s
port-swap heuristic detects the `:1515` host and auto-resolves the
backend at `:1112` — you don't need to set `VITE_AGENT_URL`.

### What hot-reloads

| Change | Reload |
|---|---|
| `ui/src/**` (Vue, TS, CSS) | Vite HMR — instant in the browser. |
| `app/**` (Python) | Restart Terminal A (`Ctrl+C`, then re-run `uv run openpa serve`). Check `uv run openpa serve --help` for a `--reload` flag if one is exposed. |

## Variations

| You want… | Run |
|---|---|
| Backend only, no UI | `uv run openpa serve`. UI listener on `:1515` is silent. |
| UI pointed at a remote backend | `cd ui ; npm run web:dev`. Configure the agent URL via the setup wizard, or `$env:VITE_AGENT_URL = "https://..."` before `npm run web:dev`. |
| Single-port end-to-end smoke (no HMR) | `bash scripts/build_ui.sh ; uv run openpa serve`. The SPA gets bundled into `app/static/ui/`, served on `:1515` by the backend. Use for pre-release verification, not active dev. |
| Electron desktop dev | `cd ui ; npm run dev`. Wraps the SPA in an Electron window. Talks to the backend URL from `~/.openpa-ui/openpa-config.json`. |

## Installing your checkout via the installer scripts

The two-terminal flow above is the fastest dev loop. If you want to
exercise the actual installer scripts users will run — to test changes
to `install/install.sh` or `install/install.ps1`, or to bring up the
full Docker bundle with Postgres + Qdrant against your checkout — pass
`--channel dev` (Linux/macOS) or `-Channel dev` (Windows):

```bash
bash install/install.sh --channel dev --deployment local
```

```powershell
.\install\install.ps1 -Channel dev -Deployment local
```

Dev channel skips PyPI and runs `pip install -e .` against your
checkout. Templates (`local.env`, `docker-compose.yml.tmpl`, …) come
from the checkout's `install/templates/`, not GitHub.

Caveats:

- The wizard at `http://localhost:1515` needs `app/static/ui/`
  populated. Run `bash scripts/build_ui.sh` once before `--channel dev`
  so the SPA listener comes up.
- `--channel dev` requires running the script *from* a checkout —
  piping it via `curl | bash` (no file on disk) is rejected.
- `--channel dev --mode docker` works: the installer emits a
  `docker-compose.override.yml` that points the build context at the
  checkout, swaps the pip install for `-e /src`, and bind-mounts the
  checkout into the container. Host edits to `app/` show up after
  `docker compose restart openpa`.
- `OPENPA_UPGRADE_CHANNEL` is intentionally not written to `.env` in
  dev channel. Don't run `openpa upgrade` from a dev install — pull
  from git instead.

## Gotchas

- **First page load after `setup`**: the SPA on `:1515` may show the
  setup wizard until the agent URL is configured. After that it sticks.
- **Backend not picking up code changes**: kill + restart Terminal A.
  Don't restart Vite — it's stateful for HMR.
- **The port-swap heuristic only kicks in when the SPA is served at
  `:1515`**. Running Vite on a different port (e.g. via `$env:PORT`)
  skips it; you need `VITE_AGENT_URL`.
- **`ui/node_modules` is large** — ~600 MB. Stay patient on first
  install.
- **Don't edit `ui/package.json`'s `version` field** — it's regenerated
  from `app/__version__.py` by `scripts/sync_ui_version.py` (runs as
  the `prebuild`/`preweb:build` npm hook).

## Tests

| What | Command |
|---|---|
| Backend pytest | `uv run pytest` |
| UI type-check | `cd ui ; npx vue-tsc --noEmit` |
| UI web smoke build | `cd ui ; npm run web:build` |

The same commands run on every PR via
[`.github/workflows/pr.yml`](.github/workflows/pr.yml). A failing job
blocks merge.

## Branch, commit, PR

```powershell
git checkout main ; git pull
git checkout -b my-feature
# edit anywhere under app/ or ui/src
git push -u origin my-feature
```

Open a pull request against `main`. `pr.yml` runs the `backend` and
`ui` jobs in parallel. Merge when green.

## Releasing

See [RELEASING.md](RELEASING.md). Short version: bump
`app/__version__.py`, push a `v<X.Y.Z>-test1` tag, iterate, then promote
with a `v<X.Y.Z>` tag on the green commit. CI enforces that a prod tag
always promotes a tested commit — there's no "skip testing" path.

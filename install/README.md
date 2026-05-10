# OpenPA installers

One-line install scripts for Linux, macOS, and Windows. Both end with
the setup wizard open in your browser; the rest of this file is the
reference for what they do, the flags they accept, and how to manage
the install once it's running.

## Quickstart

**Linux / macOS:**

```bash
curl -fsSL https://openpa.ai/install.sh | bash
```

**Windows (PowerShell):**

```powershell
iwr -useb https://openpa.ai/install.ps1 | iex
```

## Pre-release / test builds

To validate a release candidate end-to-end before it lands on production
PyPI, use the test installers. They install from
[Test PyPI](https://test.pypi.org) and are wired up to the
`release-test` GitHub workflow (which fires on tags matching `v*-test*`,
e.g. `v0.1.5-test1`).

**Linux / macOS:**

```bash
curl -fsSL https://openpa.ai/install-test.sh | bash
```

**Windows (PowerShell):**

```powershell
iwr -useb https://openpa.ai/install-test.ps1 | iex
```

The test installers behave identically to the production ones (same
flags, same prompts, both modes supported) but with two differences:

- **Pip index** — native installs use
  `--index-url https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple/ --pre openpa`.
  Docker installs forward the same URLs as build args via
  `~/.openpa/docker/.env`.
- **Working directory** — test installs share `~/.openpa` with the
  production installer. Running `install-test.sh` on a host that
  already has prod `openpa` installed **will upgrade or downgrade the
  existing install to the test version**. To keep them side-by-side,
  set `OPENPA_WORKING_DIR=~/.openpa-test` before running.

Test versions live only on Test PyPI — they are **not** announced on
the GitHub Releases page.

## Two install modes

The scripts detect Docker and ask which mode you want:

| Mode | What you get | When to pick it |
|---|---|---|
| **docker** *(recommended)* | The agent runs inside a sandboxed VNC-accessible XFCE desktop, alongside Postgres and Qdrant. You can observe what the agent is doing at `http://<host>:6080/vnc.html`. | Anytime Docker is available — the agent is isolated from your host, and the bundle includes everything in one shot. |
| **native** | A Python venv at `~/.openpa/venv` with `openpa` and SQLite. The agent shares your desktop and home directory. | Docker isn't available, or you want a minimal install without containers. |

## What docker mode does

1. Detect Docker, ask deployment type (local / server) and host.
2. Generate random VNC and Postgres passwords.
3. Render [`docker-compose.yml`](templates/docker-compose.yml.tmpl) and an `.env` file (passwords, ports, app URL, CORS) into `~/.openpa/docker/`.
4. `docker compose pull` (best-effort), then `docker compose up -d --build`.
5. Wait for `http://<host>:1112/health` to return 200.
6. Open `http://<host>:1515/#/setup` in your browser.

The bundle includes three services:

- **openpa** — XFCE + TigerVNC + noVNC + Python 3.13 + the OpenPA agent + the SPA static server. Built from [`Dockerfile.desktop`](../Dockerfile.desktop).
- **postgres** — Application DB. Schema is managed by Alembic from inside the openpa container.
- **qdrant** — Vector store for embeddings.

Stored at `~/.openpa/docker/`. Manage with:

```
cd ~/.openpa/docker
docker compose ps                  # status
docker compose logs -f openpa      # follow logs
docker compose restart openpa      # restart just the agent
docker compose down                # stop everything
docker compose down -v             # stop and delete all data
```

## What native mode does

1. Detect Python 3.13+ on PATH.
2. Ask deployment type (local / server) and host.
3. Create venv at `~/.openpa/venv` and `pip install openpa`. The
   wheel ships the prebuilt openpa-ui SPA inside it (see
   [`scripts/build_ui.sh`](../scripts/build_ui.sh)), so no separate UI
   install is needed.
4. Generate `~/.openpa/.env` from [`templates/local.env`](templates/local.env) or [`templates/server.env.tmpl`](templates/server.env.tmpl) (with `__APP_HOST__` substituted).
5. Generate `~/.openpa/bootstrap.toml` selecting SQLite.
6. Run `openpa db upgrade` to apply Alembic migrations.
7. Start `openpa serve` in the background. The same process opens two
   listeners: the API on `:1112` and the SPA on `:1515`. Wait for
   `/health`.
8. Open `http://<host>:1515/#/setup` in your browser.

Both modes are idempotent: re-running upgrades in place and keeps your
existing config + database. Use `--reinstall` (sh) or `-Reinstall` (ps1)
to wipe and start fresh.

## Flags (sh)

| Flag | Description |
|---|---|
| `--deployment local\|server` | Skip the deployment-type prompt. |
| `--host HOST`                | Public IP/domain (server only). |
| `--mode docker\|native`      | Skip the mode prompt. `--docker` and `--native` are aliases. |
| `--no-launch`                | Don't open the wizard at the end. |
| `--unattended`               | Use defaults; never prompt. Implies `--mode docker` if Docker is present. |
| `--reinstall`                | Wipe the existing venv (native) or regenerate compose+.env (docker). |

## Flags (ps1)

| Flag | Description |
|---|---|
| `-Deployment local\|server` | Skip the deployment-type prompt. |
| `-AppHost HOST`             | Public IP/domain (server only). |
| `-Mode docker\|native`      | Skip the mode prompt. |
| `-NoLaunch`                 | Don't open the wizard at the end. |
| `-Unattended`               | Use defaults; never prompt. |
| `-Reinstall`                | Wipe the existing venv or regenerate compose+.env. |

## Files written

### Docker mode

| Path | Purpose |
|---|---|
| `~/.openpa/docker/docker-compose.yml` | Compose orchestration for openpa, postgres, qdrant. |
| `~/.openpa/docker/.env`               | Secrets and config that Compose substitutes (chmod 600). |
| `~/.openpa/install.log`               | Output of `compose pull` / `compose up`. |
| Docker named volumes                  | `openpa-data`, `pg-data`, `qdrant-data`. Persisted across restarts. |

### Native mode

| Path | Purpose |
|---|---|
| `~/.openpa/venv/`                     | Python virtualenv with `openpa` (SPA included). |
| `~/.openpa/.env`                      | Backend env vars. |
| `~/.openpa/bootstrap.toml`            | DB-provider selection. |
| `~/.openpa/install.log`               | Output of `pip install` and `openpa db upgrade`. |
| `~/.openpa/install.pid`               | PID of the install-session server. |
| `~/.openpa/server.log`                | `openpa serve` stdout/stderr. |

`OPENPA_WORKING_DIR` overrides `~/.openpa` for side-by-side staging
installs. `OPENPA_TEMPLATE_BASE` overrides the URL the scripts fetch
templates from (useful for offline installs and CI).

## Stopping things

**Docker:**

```
cd ~/.openpa/docker
docker compose down
```

**Native (install-session server):**

```bash
kill $(cat ~/.openpa/install.pid)
```

```powershell
Stop-Process -Id (Get-Content ~/.openpa/install.pid)
```

A real service unit (systemd / launchd / Scheduled Task) for native
installs is a follow-up; for now the install-session server runs only
until you log out or run `kill` explicitly. Docker mode is supervised
by the daemon and survives logout.

## Building the SPA into the wheel

The `openpa` wheel ships the prebuilt openpa-ui SPA at
`app/static/ui/` so `openpa serve` can serve it on `:1515` without a
separate UI install. CI runs [`scripts/build_ui.sh`](../scripts/build_ui.sh)
before `hatch build`; you can run it locally too:

```bash
# Default: clone openpa-ui main and build it.
./scripts/build_ui.sh

# Use a local checkout instead of cloning.
OPENPA_UI_LOCAL=../openpa-ui ./scripts/build_ui.sh

# Pin a release tag.
OPENPA_UI_REF=v0.4.2 ./scripts/build_ui.sh
```

The script writes to `app/static/ui/` (gitignored). When `openpa serve`
boots it auto-detects this directory and starts the SPA listener.
`OPENPA_UI_PORT=0` disables the listener (useful when an external
nginx is already serving the SPA); `OPENPA_UI_DIR=/elsewhere` points
at a different built location, which is exactly how the Docker image
serves the stage-1 SPA build.

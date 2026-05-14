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

## Two install modes

The scripts detect Docker and ask which mode you want:

| Mode | What you get | When to pick it |
|---|---|---|
| **docker** *(recommended)* | The agent runs inside a sandboxed VNC-accessible XFCE desktop. The Setup Wizard activates Postgres, Qdrant, or ChromaDB as sibling containers on demand. You can observe what the agent is doing at `http://<host>:6080/vnc.html`. | Anytime Docker is available — the agent is isolated from your host, and per-service deployment choices are made later in the wizard. |
| **native** | A Python venv at `~/.openpa/venv` with `openpa` and SQLite. The agent shares your desktop and home directory. | Docker isn't available, or you want a minimal install without containers. |

The installer no longer asks which database or vector store to use —
those choices are now per-service, made in the Setup Wizard. Each
service that supports multiple deployments (Postgres, Qdrant, ChromaDB)
gets a Docker / Native / External radio in its wizard step. SQLite
shows no radio (it's local-only).

## What docker mode does

1. Detect Docker, ask deployment type (local / server / custom) and host or advanced fields.
2. Generate a random VNC password.
3. Render [`docker-compose.yml`](templates/docker-compose.yml.tmpl) and an `.env` file (VNC password, ports, app URL, CORS) into `~/.openpa/docker/`. The Setup Wizard later appends to `COMPOSE_PROFILES` as the user activates Docker-mode services; the bundle starts with only the `openpa` container running.
4. `docker compose pull` (best-effort), then `docker compose up -d --build`.
5. Wait for `http://<host>:1112/health` to return 200.
6. Open `http://<host>:1515/#/setup` in your browser.

The bundle defines four services. Only `openpa` is started at install
time; the others are activated by the wizard:

- **openpa** — XFCE + TigerVNC + noVNC + Python 3.13 + the OpenPA agent + the SPA static server. Always started. Built from [`Dockerfile.desktop`](../Dockerfile.desktop). Mounts `/var/run/docker.sock` so the in-container wizard can `docker compose up -d` the sibling services on demand.
- **postgres** — Application DB (`postgres:16`). Activated when the wizard's Database step picks **Postgres + Docker**.
- **qdrant** — Vector store (`qdrant/qdrant:latest`). Activated when the wizard's Embedding step picks **Qdrant + Docker**.
- **chroma** — Vector store (`chromadb/chroma:latest`). Activated when the wizard's Embedding step picks **ChromaDB + Docker**.

For each of those sidecars you can instead pick **Native** (run a local
binary or in-process library) or **External** (point at a host/port you
already operate) from the wizard. SQLite is always local-only.

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
2. Ask deployment type (local / server / custom) and host or advanced fields.
3. Create venv at `~/.openpa/venv` and `pip install openpa`. The
   wheel ships the prebuilt SPA inside it (see
   [`scripts/build_ui.sh`](../scripts/build_ui.sh)), so no separate UI
   install is needed.
4. Generate `~/.openpa/.env` from [`templates/local.env`](templates/local.env), [`templates/server.env.tmpl`](templates/server.env.tmpl) (with `__APP_HOST__` substituted), or [`templates/custom.env.tmpl`](templates/custom.env.tmpl) (with the four advanced-field placeholders substituted). The installer also appends `INSTALL_MODE=docker|native` so the backend's Setup Wizard can filter per-service deployment modes.
5. Generate `~/.openpa/bootstrap.toml` selecting SQLite.
6. Run `openpa db upgrade` to apply Alembic migrations.
7. Start `openpa serve` in the background. The same process opens two
   listeners: the API on `:1112` and the SPA on `:1515`. Wait for
   `/health`.
8. Open `http://<host>:1515/#/setup` in your browser.

Both modes are idempotent: re-running upgrades in place and keeps your
existing config + database. Use `--reinstall` (sh) or `-Reinstall` (ps1)
to wipe and start fresh.

## Deployment types

Both installers ask **how** OpenPA will be reached:

| Deployment | What it sets | When to pick it |
|---|---|---|
| **local**  | `HOST=127.0.0.1`. Only this machine can reach the backend. | Single-user desktop install. |
| **server** | `HOST=0.0.0.0`, `APP_URL=http://<your-host>:1112`, CORS allows your host. | Multi-machine setup; pass `--host`/`-AppHost` with your public IP or domain. |
| **custom** *(advanced)* | You pick `HOST`, `APP_URL`, `CORS_ALLOWED_ORIGINS`, and the Setup Wizard preset yourself. | Inside containers, behind reverse proxies, or anywhere the local/server presets don't fit. |

`container` is accepted as a deprecated alias for `custom` (with
container-friendly defaults) for one release; new scripts should use
`custom` directly.

## Flags (sh)

| Flag | Description |
|---|---|
| `--deployment local\|server\|custom` | Skip the deployment-type prompt. |
| `--host HOST`                        | Public IP/domain (server only). |
| `--listen-host HOST`                 | (custom) Override `HOST` in the rendered `.env`. |
| `--public-url URL`                   | (custom) Override `APP_URL`. |
| `--allowed-origins LIST`             | (custom) Override `CORS_ALLOWED_ORIGINS`. |
| `--wizard-preset ID`                 | (custom) Override `SETUP_WIZARD_ENV`. |
| `--mode docker\|native`              | Skip the mode prompt. `--docker` and `--native` are aliases. |
| `--no-launch`                        | Don't open the wizard at the end. |
| `--unattended`                       | Use defaults; never prompt. Implies `--mode docker` if Docker is present. |
| `--reinstall`                        | Wipe the existing venv (native) or regenerate compose+.env (docker). |

## Flags (ps1)

| Flag | Description |
|---|---|
| `-Deployment local\|server\|custom` | Skip the deployment-type prompt. |
| `-AppHost HOST`                     | Public IP/domain (server only). |
| `-ListenHost HOST`                  | (custom) Override `HOST`. |
| `-PublicUrl URL`                    | (custom) Override `APP_URL`. |
| `-AllowedOrigins LIST`              | (custom) Override `CORS_ALLOWED_ORIGINS`. |
| `-WizardPreset ID`                  | (custom) Override `SETUP_WIZARD_ENV`. |
| `-Mode docker\|native`              | Skip the mode prompt. |
| `-NoLaunch`                         | Don't open the wizard at the end. |
| `-Unattended`                       | Use defaults; never prompt. |
| `-Reinstall`                        | Wipe the existing venv or regenerate compose+.env. |

## Shared catalog

Deployment labels, install-mode descriptions, and the per-install-mode
service-mode visibility rules live in [`catalog.toml`](catalog.toml).
Both install scripts and the Setup Wizard read from it:

- `install.sh` and `install.ps1` source the generated bash and
  PowerShell includes ([`_catalog.sh`](_catalog.sh) and
  [`_catalog.ps1`](_catalog.ps1)).
- The backend ships a copy at `app/config/install_catalog.toml` and
  serves it to the wizard via `GET /api/config/install-catalog`.

Re-generate the includes after editing `catalog.toml`:

```bash
python install/scripts/build_catalog.py        # write the includes
python install/scripts/build_catalog.py --check  # CI: exit 1 on drift
```

When re-running on an existing Docker install (without `--reinstall` /
`-Reinstall`), the installer reuses the existing
`~/.openpa/docker/.env`. Service-deployment choices are persisted by
the wizard, not by the installer, so they survive re-runs as well.

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

The `openpa` wheel ships the prebuilt SPA at `app/static/ui/` so
`openpa serve` can serve it on `:1515` without a separate UI install.
CI runs [`scripts/build_ui.sh`](../scripts/build_ui.sh) before
`hatch build`; you can run it locally too:

```bash
./scripts/build_ui.sh
```

The script builds the in-tree `ui/` source and writes to
`app/static/ui/` (gitignored). When `openpa serve` boots it auto-detects
this directory and starts the SPA listener. `OPENPA_UI_PORT=0` disables
the listener (useful when an external nginx is already serving the SPA);
`OPENPA_UI_DIR=/elsewhere` points at a different built location, which
is exactly how the Docker image serves the stage-1 SPA build.

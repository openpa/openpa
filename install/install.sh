#!/usr/bin/env bash
# OpenPA installer — Linux / macOS.
#
# Usage:
#   curl -fsSL https://openpa.ai/install.sh | bash
#   curl -fsSL https://openpa.ai/install.sh | bash -s -- [flags]
#
# Flags:
#   --deployment local|server|container
#                               Skip the deployment-type prompt.
#                               container = run inside Docker/Podman; bind to
#                               0.0.0.0 so the docker host can reach the
#                               wizard via published ports.
#   --host HOST                 Public IP/domain (server deployment only).
#   --no-launch                 Skip opening the setup wizard at the end.
#   --unattended                Use defaults; never prompt. Implies --no-launch
#                               unless deployment+host are also provided.
#   --reinstall                 Wipe any existing ~/.openpa/venv before installing.
#   --auto-install-python       Auto-install isolated Python 3.13 if missing
#                               (default: prompt; --unattended installs silently).
#   --no-auto-install-python    Never auto-install; print manual hints and exit.
#   --no-modify-path            Don't modify shell rc; print the export line instead.
#   --dev                       Install from the local checkout (developer mode).
#                               Requires running this script from a clone of the
#                               repo; rejected when piped via curl. Works with
#                               both --mode native and --mode docker (the
#                               compose override bind-mounts the checkout
#                               at /src for an editable install).
#   --help                      Show this message.
#
# This is the Phase 2 installer: native install only. Docker mode is
# detected and recommended, but the actual containerized bundle ships in
# Phase 3 — for now we explain that and fall back to native.

set -euo pipefail

# ── colors / logging ──────────────────────────────────────────────────────

if [ -t 1 ]; then
    BOLD=$(printf '\033[1m')
    DIM=$(printf '\033[2m')
    RED=$(printf '\033[31m')
    GREEN=$(printf '\033[32m')
    YELLOW=$(printf '\033[33m')
    BLUE=$(printf '\033[34m')
    RESET=$(printf '\033[0m')
else
    BOLD= DIM= RED= GREEN= YELLOW= BLUE= RESET=
fi

info()  { printf '%s==>%s %s\n' "$BLUE$BOLD" "$RESET" "$1"; }
warn()  { printf '%s!!!%s %s\n' "$YELLOW$BOLD" "$RESET" "$1" >&2; }
err()   { printf '%sERR%s %s\n' "$RED$BOLD" "$RESET" "$1" >&2; }
ok()    { printf '%s ✓%s  %s\n' "$GREEN$BOLD" "$RESET" "$1"; }
step()  { printf '\n%s── %s ──%s\n' "$BOLD" "$1" "$RESET"; }

# ── channel (hidden) ──────────────────────────────────────────────────────
#
# OPENPA_INSTALL_CHANNEL / --channel is intentionally absent from --help.
# It controls the install source:
#   production (default) — pip install openpa from PyPI
#   test                 — install latest dev wheel from Test PyPI
#   dev                  — pip install -e <repo_root> (local checkout)
# End users never set this; CI/maintainers pass --channel test; developers
# pass --dev, which is just a visible alias that sets CHANNEL=dev.
CHANNEL="${OPENPA_INSTALL_CHANNEL:-production}"

# REPO_ROOT is the repo containing this script. Required for dev mode (we
# install from there and read templates from there). Empty when piped via
# curl — the dev-mode check below uses that to reject `curl | bash -s -- --dev`.
REPO_ROOT=""
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
    REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." 2>/dev/null && pwd)"
fi

# ── flags ─────────────────────────────────────────────────────────────────

DEPLOYMENT=""
APP_HOST=""
MODE=""           # docker | native (default: prompt if Docker available)
NO_LAUNCH=0
UNATTENDED=0
REINSTALL=0
AUTO_INSTALL_PYTHON=""   # "" = ask interactively; "1" = yes; "0" = no
MODIFY_PATH=""           # "" = auto (1 for canonical home, 0 otherwise); 0/1 explicit

while [ $# -gt 0 ]; do
    case "$1" in
        --deployment)             DEPLOYMENT="$2"; shift 2 ;;
        --deployment=*)           DEPLOYMENT="${1#*=}"; shift ;;
        --host)                   APP_HOST="$2"; shift 2 ;;
        --host=*)                 APP_HOST="${1#*=}"; shift ;;
        --mode)                   MODE="$2"; shift 2 ;;
        --mode=*)                 MODE="${1#*=}"; shift ;;
        --docker)                 MODE="docker"; shift ;;
        --native)                 MODE="native"; shift ;;
        --no-launch)              NO_LAUNCH=1; shift ;;
        --unattended)             UNATTENDED=1; shift ;;
        --reinstall)              REINSTALL=1; shift ;;
        --auto-install-python)    AUTO_INSTALL_PYTHON=1; shift ;;
        --no-auto-install-python) AUTO_INSTALL_PYTHON=0; shift ;;
        --modify-path)            MODIFY_PATH=1; shift ;;
        --no-modify-path)         MODIFY_PATH=0; shift ;;
        --channel)                CHANNEL="$2"; shift 2 ;;
        --channel=*)              CHANNEL="${1#*=}"; shift ;;
        --dev)                    CHANNEL="dev"; shift ;;
        --help|-h)
            sed -n '1,/^set -e/p' "$0" | sed -e 's/^# \{0,1\}//' -e '/^set -e/d'
            exit 0
            ;;
        *)
            err "Unknown flag: $1"
            exit 2
            ;;
    esac
done

# ── channel validation / dev-mode guards ──────────────────────────────────

case "$CHANNEL" in
    production|test|dev) ;;
    *) err "Invalid --channel: $CHANNEL (must be production, test, or dev)"; exit 2 ;;
esac

if [ "$CHANNEL" = "dev" ]; then
    if [ -z "$REPO_ROOT" ] || [ ! -f "$REPO_ROOT/pyproject.toml" ]; then
        err "--dev requires running install.sh from a checkout (not piped via curl)."
        err "Usage: bash <repo>/install/install.sh --dev"
        exit 2
    fi
fi

# Detect a containerized host (Docker / Podman / k8s pod). When the
# installer runs inside a container, ``local`` deployment binds to
# 127.0.0.1 inside the container — unreachable from the docker host's
# browser even with ``-p 1515:1515``. We use this to default unattended
# installs and prompt-default to ``container`` instead.
IN_CONTAINER=0
if [ -f /.dockerenv ] || [ -f /run/.containerenv ] \
        || (grep -qE '(docker|containerd|kubepods)' /proc/1/cgroup 2>/dev/null); then
    IN_CONTAINER=1
fi

if [ "$UNATTENDED" -eq 1 ] && [ -z "$DEPLOYMENT" ]; then
    if [ "$IN_CONTAINER" -eq 1 ]; then
        DEPLOYMENT="container"
    else
        DEPLOYMENT="local"
    fi
fi
if [ "$UNATTENDED" -eq 1 ] && [ -z "$APP_HOST" ] && [ "$DEPLOYMENT" = "server" ]; then
    err "--unattended with --deployment=server requires --host"
    exit 2
fi
# --unattended implies "yes" for the auto-install prompt unless the
# operator explicitly said otherwise.
if [ "$UNATTENDED" -eq 1 ] && [ -z "$AUTO_INSTALL_PYTHON" ]; then
    AUTO_INSTALL_PYTHON=1
fi

# ── paths ─────────────────────────────────────────────────────────────────

# Honour OPENPA_WORKING_DIR so power users can install side-by-side
# (e.g., a staging copy under ~/.openpa-staging).
OPENPA_HOME="${OPENPA_WORKING_DIR:-$HOME/.openpa}"
VENV_DIR="$OPENPA_HOME/venv"
ENV_FILE="$OPENPA_HOME/.env"
BOOTSTRAP_FILE="$OPENPA_HOME/bootstrap.toml"
LOG_FILE="$OPENPA_HOME/install.log"
BIN_DIR="$OPENPA_HOME/bin"
UV_BIN="$BIN_DIR/uv"

# Default MODIFY_PATH: only modify shell rc for the canonical install dir,
# so a staging/test install at OPENPA_WORKING_DIR=~/.openpa-test doesn't
# clobber the prod PATH entry. --modify-path / --no-modify-path override.
if [ -z "$MODIFY_PATH" ]; then
    if [ "$OPENPA_HOME" = "$HOME/.openpa" ]; then
        MODIFY_PATH=1
    else
        MODIFY_PATH=0
    fi
fi

# Scope pip's HTTP + wheel cache under our install dir so a user-driven
# `rm -rf ~/.openpa` (or --reinstall) can't leave behind a stale index
# response that makes pip pin an old version. Without this, pip uses
# ~/.cache/pip/, which persists across openpa reinstalls and has bitten
# us when re-resolving the latest pre-release.
export PIP_CACHE_DIR="$OPENPA_HOME/pip-cache"

mkdir -p "$OPENPA_HOME"

# Templates are fetched at install time so we don't need to ship them
# alongside the script. Production/test fetch from GitHub; dev reads from
# the checkout's install/templates/ directly (treat them as files, not
# URLs). OPENPA_TEMPLATE_BASE overrides the remote URL for testing.
if [ "$CHANNEL" = "dev" ]; then
    TEMPLATE_BASE="file://$REPO_ROOT/install/templates"
else
    TEMPLATE_BASE="${OPENPA_TEMPLATE_BASE:-https://raw.githubusercontent.com/openpa/openpa/main/install/templates}"
fi

# Helper: emit a template to stdout, branching on file:// vs http(s)://.
# Centralizes the curl-vs-cat plumbing so every caller reads from one verb.
fetch_template() {
    local name="$1"
    case "$TEMPLATE_BASE" in
        file://*) cat "${TEMPLATE_BASE#file://}/$name" ;;
        *)        curl -fsSL "$TEMPLATE_BASE/$name" ;;
    esac
}

# ── banner ────────────────────────────────────────────────────────────────

cat <<EOF
${BOLD}OpenPA installer${RESET}
${DIM}Logs: $LOG_FILE${RESET}

EOF

# Channel stamp: visible ONLY when non-production. End users never see it;
# CI / maintainers / devs do, and it tells them at a glance which path the
# installer is taking.
if [ "$CHANNEL" != "production" ]; then
    if [ "$CHANNEL" = "dev" ]; then
        printf '%s==>%s %schannel: dev (source: %s)%s\n\n' \
            "$BLUE$BOLD" "$RESET" "$DIM" "$REPO_ROOT" "$RESET"
    else
        printf '%s==>%s %schannel: %s%s\n\n' \
            "$BLUE$BOLD" "$RESET" "$DIM" "$CHANNEL" "$RESET"
    fi
fi

# ── detection ─────────────────────────────────────────────────────────────

step "Environment"

OS_NAME="$(uname -s)"
case "$OS_NAME" in
    Linux*)  OS=linux ;;
    Darwin*) OS=macos ;;
    *)
        err "Unsupported OS: $OS_NAME (this script handles Linux and macOS; use install.ps1 on Windows)"
        exit 1
        ;;
esac
ARCH="$(uname -m)"
ok "OS:   $OS ($ARCH)"

# curl is used for fetching templates, downloading uv, and the post-install
# health check. Fail fast with a clear hint if it's missing — the alternative
# is a confusing error several screens later.
if ! command -v curl >/dev/null 2>&1; then
    err "curl is required but was not found on PATH."
    cat <<EOF >&2

Install curl, then re-run this script:
  ${BOLD}macOS${RESET}: brew install curl   (usually preinstalled)
  ${BOLD}Ubuntu/Debian${RESET}: sudo apt install curl
  ${BOLD}Fedora/RHEL${RESET}: sudo dnf install curl
  ${BOLD}Alpine${RESET}: apk add curl

EOF
    exit 1
fi

# Find a Python 3.13+ interpreter. Try the most-specific name first so we
# don't accidentally pick up a system 3.10 named just `python3`.
find_system_python() {
    for candidate in python3.13 python3.14 python3 python; do
        if command -v "$candidate" >/dev/null 2>&1; then
            ver="$("$candidate" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo "")"
            case "$ver" in
                3.13|3.14|3.15|3.16|3.17|3.18|3.19)
                    command -v "$candidate"
                    return 0
                    ;;
            esac
        fi
    done
    return 1
}
PYTHON="$(find_system_python || true)"

if [ -n "$PYTHON" ]; then
    ok "Python: $("$PYTHON" --version) at $PYTHON"
else
    info "Python: 3.13+ not found (will auto-install in native mode)"
fi

# Docker is detected so we can RECOMMEND it. The actual container bundle
# ships in Phase 3 — for now we explain and fall back.
HAS_DOCKER=0
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    HAS_DOCKER=1
    ok "Docker: detected (recommended in a future release)"
else
    info "Docker: not detected (or not running)"
fi

# ── deployment type ───────────────────────────────────────────────────────

step "Deployment"

if [ -z "$DEPLOYMENT" ]; then
    if [ "$IN_CONTAINER" -eq 1 ]; then
        default_choice=3
        cat <<EOF
${YELLOW}${BOLD}Detected: this installer is running inside a container.${RESET}
${DIM}Pick option 3 — local would bind to 127.0.0.1 inside the container,
which is unreachable from the docker host's browser even with -p forwarding.${RESET}

How will you run OpenPA?
  ${BOLD}1)${RESET} ${BOLD}local${RESET}      — bind to 127.0.0.1, only this machine can reach it
  ${BOLD}2)${RESET} ${BOLD}server${RESET}     — bind to all interfaces, reachable from other devices
  ${BOLD}3)${RESET} ${BOLD}container${RESET}  — bind to 0.0.0.0; URLs use localhost (recommended here)
EOF
    else
        default_choice=1
        cat <<EOF
How will you run OpenPA?
  ${BOLD}1)${RESET} ${BOLD}local${RESET}      — bind to 127.0.0.1, only this machine can reach it
  ${BOLD}2)${RESET} ${BOLD}server${RESET}     — bind to all interfaces, reachable from other devices
  ${BOLD}3)${RESET} ${BOLD}container${RESET}  — bind to 0.0.0.0; URLs use localhost
                  ${DIM}(pick this if you're running this script inside a container
                   and will browse from the docker host)${RESET}
EOF
    fi
    while :; do
        read -r -p "Choice [$default_choice]: " choice </dev/tty || choice=""
        case "${choice:-$default_choice}" in
            1|local)     DEPLOYMENT=local;     break ;;
            2|server)    DEPLOYMENT=server;    break ;;
            3|container) DEPLOYMENT=container; break ;;
            *) warn "Pick 1, 2, or 3." ;;
        esac
    done
fi
ok "Deployment: $DEPLOYMENT"

if [ "$DEPLOYMENT" = "server" ] && [ -z "$APP_HOST" ]; then
    while :; do
        read -r -p "Public IP or domain (e.g. 100.120.175.90 or openpa.example.com): " APP_HOST </dev/tty || APP_HOST=""
        case "$APP_HOST" in
            "" ) warn "Required for server deployment." ;;
            *[![:alnum:].:-]* ) warn "Invalid characters; use letters, digits, dot, colon, hyphen." ;;
            *) break ;;
        esac
    done
fi
[ -n "$APP_HOST" ] && ok "Host: $APP_HOST"

# ── mode (docker vs native) ──────────────────────────────────────────────

# Default: docker if available, native otherwise. We sandbox the agent in
# a desktop container by default — the user opts out explicitly if they
# want a native install.
if [ -z "$MODE" ]; then
    if [ "$HAS_DOCKER" -eq 1 ]; then
        if [ "$UNATTENDED" -eq 1 ]; then
            MODE="docker"
        else
            cat <<EOF

${BOLD}How do you want to run OpenPA?${RESET}
  ${BOLD}1)${RESET} ${BOLD}docker${RESET}  — sandboxed VNC desktop with bundled Postgres + Qdrant
                ${DIM}recommended; the agent gets its own GUI environment${RESET}
  ${BOLD}2)${RESET} ${BOLD}native${RESET}  — Python venv at ~/.openpa/venv with SQLite
                ${DIM}simpler, but the agent shares your desktop${RESET}
EOF
            while :; do
                read -r -p "Choice [1]: " choice </dev/tty || choice=""
                case "${choice:-1}" in
                    1|docker) MODE=docker; break ;;
                    2|native) MODE=native; break ;;
                    *) warn "Pick 1 or 2." ;;
                esac
            done
        fi
    else
        MODE="native"
    fi
fi
ok "Mode: $MODE"

if [ "$MODE" = "docker" ] && [ "$HAS_DOCKER" -eq 0 ]; then
    err "Docker mode requested but Docker is not available."
    exit 1
fi

# ── docker install ────────────────────────────────────────────────────────

# Random secret generator. /dev/urandom + tr keeps us within the printable
# alnum set so passwords paste cleanly through web forms and shells.
gen_secret() {
    LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 24 || true
    echo
}

# Read the version pinned in the installed package, falling back to
# ``main`` when we don't have a Python install yet (Docker mode is
# allowed without Python on the host).
resolve_version() {
    local v=""
    if [ -n "$PYTHON" ]; then
        v="$("$PYTHON" -c 'from importlib.metadata import version, PackageNotFoundError
try:
    print(version("openpa"))
except PackageNotFoundError:
    pass' 2>/dev/null || true)"
    fi
    if [ -z "$v" ]; then v="main"; fi
    echo "$v"
}

if [ "$MODE" = "docker" ]; then
    step "Docker install"

    DOCKER_DIR="$OPENPA_HOME/docker"
    mkdir -p "$DOCKER_DIR"

    # Idempotency: if the bundle is already running, just bring it up
    # again (which no-ops if everything is healthy) and skip generation.
    if [ -f "$DOCKER_DIR/.env" ] && [ -f "$DOCKER_DIR/docker-compose.yml" ] && [ "$REINSTALL" -ne 1 ]; then
        info "Existing Docker bundle detected at $DOCKER_DIR — reusing config."
    else
        VNC_PASSWORD="$(gen_secret)"
        PG_PASSWORD="$(gen_secret)"
        OPENPA_VERSION="$(resolve_version)"
        OPENPA_UI_REF="${OPENPA_UI_REF:-main}"

        if [ "$DEPLOYMENT" = "local" ]; then
            DOCKER_APP_URL="http://localhost:1112"
            DOCKER_CORS="http://localhost:1515,http://127.0.0.1:1515"
            DOCKER_WIZARD_ENV="local"
        else
            DOCKER_APP_URL="http://$APP_HOST:1112"
            DOCKER_CORS="http://$APP_HOST:1515,http://localhost:1515"
            DOCKER_WIZARD_ENV="server"
        fi

        info "Fetching docker-compose template"
        fetch_template "docker-compose.yml.tmpl" > "$DOCKER_DIR/docker-compose.yml"

        info "Writing $DOCKER_DIR/.env (secrets, do not commit)"
        fetch_template "docker.env.tmpl" \
            | sed \
                -e "s|__OPENPA_VERSION__|$OPENPA_VERSION|g" \
                -e "s|__OPENPA_UI_REF__|$OPENPA_UI_REF|g" \
                -e "s|__APP_URL__|$DOCKER_APP_URL|g" \
                -e "s|__CORS_ALLOWED_ORIGINS__|$DOCKER_CORS|g" \
                -e "s|__SETUP_WIZARD_ENV__|$DOCKER_WIZARD_ENV|g" \
                -e "s|__PG_PASSWORD__|$PG_PASSWORD|g" \
                -e "s|__VNC_PASSWORD__|$VNC_PASSWORD|g" \
            > "$DOCKER_DIR/.env"

        # Test channel: forward Test PyPI indices into the Dockerfile build
        # via the compose .env. Prod leaves both unset (Dockerfile treats
        # empty as default PyPI). Dev installs use ``-e /src`` via the
        # override file below, so they don't need pip index overrides.
        if [ "$CHANNEL" = "test" ]; then
            cat >>"$DOCKER_DIR/.env" <<EOF
OPENPA_PIP_INDEX_URL=https://test.pypi.org/simple/
OPENPA_PIP_EXTRA_INDEX_URL=https://pypi.org/simple/
EOF
        fi
        chmod 600 "$DOCKER_DIR/.env"

        # Dev channel: emit a docker-compose.override.yml that points the
        # build context at the local checkout, switches the pip install
        # to ``-e /src``, and bind-mounts the checkout for runtime
        # imports. Compose auto-merges this when running from $DOCKER_DIR.
        if [ "$CHANNEL" = "dev" ]; then
            info "Writing $DOCKER_DIR/docker-compose.override.yml (bind-mounts $REPO_ROOT at /src)"
            fetch_template "docker-compose.override.yml.tmpl" \
                | sed -e "s|__REPO_ROOT__|$REPO_ROOT|g" \
                > "$DOCKER_DIR/docker-compose.override.yml"
        fi

        ok "Wrote $DOCKER_DIR/docker-compose.yml + .env"
    fi

    # ``compose pull`` is best-effort — if the published image isn't
    # available yet (dev release), Compose's ``build:`` directive picks
    # up the slack on ``up -d``.
    info "Pulling images (this may take a few minutes the first time)"
    (cd "$DOCKER_DIR" && docker compose pull --ignore-pull-failures \
        >>"$LOG_FILE" 2>&1) || warn "Some images couldn't be pulled; will build locally."

    info "Starting bundle"
    (cd "$DOCKER_DIR" && docker compose up -d --build >>"$LOG_FILE" 2>&1)

    # Health gate: don't open the wizard until the backend is reachable.
    if [ "$DEPLOYMENT" = "local" ]; then
        HEALTH_HOST="localhost"
    else
        HEALTH_HOST="$APP_HOST"
    fi
    HEALTH_URL="http://${HEALTH_HOST}:1112/health"
    info "Waiting for backend at $HEALTH_URL ..."
    for i in $(seq 1 60); do
        if curl -fsS "$HEALTH_URL" >/dev/null 2>&1; then
            ok "Backend is up"
            break
        fi
        sleep 2
    done

    # The Electron app drives navigation to the wizard inside its own
    # window — printing a "Wizard URL: http://localhost:1515/#/setup"
    # block in that context misleads the user into thinking they need
    # to open a browser. Suppress the whole human-handoff section when
    # invoked with OPENPA_INSTALLER_FRONTEND=electron.
    WIZARD_URL="http://${HEALTH_HOST}:1515/#/setup"
    if [ "${OPENPA_INSTALLER_FRONTEND:-}" != "electron" ]; then
        step "Setup wizard"

        NOVNC_URL="http://${HEALTH_HOST}:6080/vnc.html"
        cat <<EOF
The setup wizard is the next step. It collects your LLM API keys,
profile name, and tool preferences, then activates the server.

  ${BOLD}Wizard URL${RESET}: $WIZARD_URL
  ${BOLD}Backend${RESET}:    http://${HEALTH_HOST}:1112
  ${BOLD}Desktop${RESET}:    $NOVNC_URL
  ${BOLD}VNC password${RESET} (saved to $DOCKER_DIR/.env):
    $(grep '^VNC_PASSWORD=' "$DOCKER_DIR/.env" | cut -d= -f2-)

  Stop:    cd $DOCKER_DIR && docker compose down
  Logs:    cd $DOCKER_DIR && docker compose logs -f openpa
  Restart: cd $DOCKER_DIR && docker compose restart openpa

EOF

        if [ "$NO_LAUNCH" -eq 0 ] && [ "$UNATTENDED" -eq 0 ]; then
            if command -v xdg-open >/dev/null 2>&1; then
                xdg-open "$WIZARD_URL" >/dev/null 2>&1 || true
            elif command -v open >/dev/null 2>&1; then
                open "$WIZARD_URL" >/dev/null 2>&1 || true
            fi
        fi
    fi

    case "$CHANNEL" in
        test) ok "Done. Welcome to OpenPA (test build)." ;;
        *)    ok "Done. Welcome to OpenPA." ;;
    esac
    exit 0
fi

# ── native install ────────────────────────────────────────────────────────

# (Reaching here implies MODE=native. The Docker path exited above.)

# Print the manual-install hint shown when auto-install is declined or fails.
print_python_manual_hint() {
    cat <<EOF >&2

Install options:
  ${BOLD}macOS${RESET}: brew install python@3.13
  ${BOLD}Ubuntu/Debian${RESET}: sudo apt install python3.13 python3.13-venv
  ${BOLD}Fedora/RHEL${RESET}: sudo dnf install python3.13
  ${BOLD}Any${RESET}: https://www.python.org/downloads/

Re-run this script after Python is on your PATH (or pass --mode docker).
EOF
}

# Decide whether to auto-install Python. Honors --auto-install-python /
# --no-auto-install-python / --unattended; otherwise prompts (default Yes).
prompt_for_python_install() {
    if [ "$AUTO_INSTALL_PYTHON" = "0" ]; then
        err "Python 3.13 or newer is required for native mode but was not found."
        print_python_manual_hint
        exit 1
    fi
    if [ "$AUTO_INSTALL_PYTHON" = "1" ]; then
        return 0
    fi
    cat <<EOF

OpenPA can install an isolated Python 3.13 just for itself
(~70 MB downloaded into $OPENPA_HOME/python, no admin needed; system
Python is left untouched).

EOF
    while :; do
        read -r -p "Install isolated Python 3.13 now? [Y/n]: " choice </dev/tty || choice=""
        case "${choice:-y}" in
            y|Y|yes|YES) return 0 ;;
            n|N|no|NO)
                err "Aborted: Python 3.13 is required for native mode."
                print_python_manual_hint
                exit 1
                ;;
            *) warn "Please answer y or n." ;;
        esac
    done
}

# Download a private copy of `uv` into $OPENPA_HOME/bin so we can manage
# Python and venv installs without touching system tools. Pinned to a
# tested major.minor.
install_uv_locally() {
    if [ -x "$UV_BIN" ]; then
        info "uv already installed at $UV_BIN"
        return 0
    fi
    info "Installing uv into $BIN_DIR"
    mkdir -p "$BIN_DIR"
    if ! curl -LsSf https://astral.sh/uv/install.sh \
            | env UV_INSTALL_DIR="$BIN_DIR" UV_UNMANAGED_INSTALL="$BIN_DIR" \
                  INSTALLER_NO_MODIFY_PATH=1 sh \
            >>"$LOG_FILE" 2>&1; then
        err "Failed to download uv (the Python installer)."
        cat <<EOF >&2

Possible causes: no internet, corporate TLS interception, or astral.sh
is blocked. Set HTTPS_PROXY / SSL_CERT_FILE if you're behind a proxy,
or install Python manually (see hints below).
EOF
        print_python_manual_hint
        exit 1
    fi
    if [ ! -x "$UV_BIN" ]; then
        err "uv installer ran but $UV_BIN is missing — see $LOG_FILE."
        exit 1
    fi
    ok "Installed uv at $UV_BIN"
}

# Use uv to download an isolated Python 3.13 into $OPENPA_HOME/python and
# return its absolute path via $PYTHON. The cache and install dir are
# scoped to OPENPA_HOME so `rm -rf ~/.openpa` removes everything.
install_python_via_uv() {
    export UV_PYTHON_INSTALL_DIR="$OPENPA_HOME/python"
    export UV_CACHE_DIR="$OPENPA_HOME/uv-cache"
    info "Downloading isolated Python 3.13 (this may take a minute)"
    if ! "$UV_BIN" python install 3.13 >>"$LOG_FILE" 2>&1; then
        err "uv failed to install Python 3.13 — see $LOG_FILE."
        exit 1
    fi
    PYTHON="$("$UV_BIN" python find 3.13 2>/dev/null | tr -d '[:space:]')"
    if [ -z "$PYTHON" ] || [ ! -x "$PYTHON" ]; then
        err "Python install completed but the interpreter could not be located."
        exit 1
    fi
    ok "Python: $("$PYTHON" --version) at $PYTHON (isolated)"
}

# Dev channel reuses the developer's local .venv (managed by uv) for the
# install, so we don't need a separate Python here. Skip the prompt and
# the isolated-Python bootstrap entirely.
if [ -z "$PYTHON" ] && [ "$CHANNEL" != "dev" ]; then
    prompt_for_python_install
    install_uv_locally
    install_python_via_uv
fi

# ── existing install detection ────────────────────────────────────────────

step "Install"

if [ "$CHANNEL" = "dev" ]; then
    # Dev channel: reuse the developer's local .venv (managed by ``uv
    # sync`` from <repo>/pyproject.toml) instead of building a parallel
    # venv at $OPENPA_HOME/venv. The dev already has openpa + every
    # transitive dep installed in editable mode; reinstalling them into
    # a separate venv takes minutes for no benefit.
    if [ "$REINSTALL" -eq 1 ]; then
        warn "--reinstall has no effect in dev mode (the dev .venv is shared; refusing to wipe)."
    fi
    if [ ! -d "$REPO_ROOT/app/static/ui" ]; then
        warn "Dev: $REPO_ROOT/app/static/ui is empty."
        warn "Run scripts/build_ui.sh once so the SPA listener can start."
    fi
    VENV_DIR="$REPO_ROOT/.venv"
    if [ ! -x "$VENV_DIR/bin/openpa" ]; then
        err "Dev mode expects $VENV_DIR/bin/openpa to exist."
        err "Run 'uv sync' from $REPO_ROOT first, then re-run this installer."
        exit 1
    fi
    info "Reusing dev .venv at $VENV_DIR (no pip install)"
    INSTALLED_VERSION="$("$VENV_DIR/bin/openpa" version 2>/dev/null | awk '{print $2}' || echo "?")"
    ok "Using openpa $INSTALLED_VERSION from dev .venv"
else
    if [ "$REINSTALL" -eq 1 ] && [ -d "$VENV_DIR" ]; then
        info "Removing existing venv (--reinstall): $VENV_DIR"
        rm -rf "$VENV_DIR"
    fi

    # Resolve the spec passed to ``pip install``. Test channel pins a
    # direct wheel URL (see Test PyPI rationale below); production uses
    # the bare package name.
    INSTALL_SPEC=""
    INSTALL_SOURCE_LABEL=""
    case "$CHANNEL" in
        production)
            INSTALL_SPEC="openpa"
            INSTALL_SOURCE_LABEL="PyPI"
            ;;
        test)
            # Resolve the latest openpa test wheel directly from Test
            # PyPI's simple index, then install that URL. Production
            # PyPI is the only resolver for transitive deps.
            #
            # We deliberately do NOT use ``--index-url <test_pypi>
            # --extra-index-url <prod_pypi> --pre`` to install openpa,
            # because Test PyPI is a public free-for-all polluted with
            # broken stubs and stale pre-releases that match openpa's
            # transitive constraints. Two examples we hit on
            # v0.1.6.dev4:
            #
            #   - ``FASTAPI-1.0.tar.gz`` (uppercase, 2.5 KB stub with a
            #     missing DESCRIPTION.txt) satisfies ``fastapi>=0.115.2``
            #     and crashes the setuptools build with FileNotFoundError.
            #   - ``httpx-1.0.dev3`` outranks prod PyPI's stable
            #     ``httpx 0.28`` once ``--pre`` is on, even though we
            #     only wanted pre-release semantics for openpa itself.
            #
            # Pinning openpa to a direct wheel URL sidesteps both: pip
            # never asks Test PyPI for transitive deps, so the pollution
            # can't reach us.
            info "Locating latest openpa test wheel"
            INSTALL_SPEC="$(curl -fsSL https://test.pypi.org/simple/openpa/ \
                | grep -oE 'https://[^"]*openpa-[^"]*-py3-none-any\.whl' \
                | awk -F/ '{print $NF, $0}' \
                | sort -V \
                | awk 'END {print $2}')"
            if [ -z "$INSTALL_SPEC" ]; then
                err "No openpa wheel found at https://test.pypi.org/simple/openpa/"
                exit 1
            fi
            ok "Test wheel: $(basename "$INSTALL_SPEC")"
            INSTALL_SOURCE_LABEL="Test PyPI"
            ;;
    esac

    if [ -d "$VENV_DIR" ]; then
        info "Existing install detected at $VENV_DIR — upgrading in place."
        "$VENV_DIR/bin/pip" install --upgrade pip >>"$LOG_FILE" 2>&1
        "$VENV_DIR/bin/pip" install --upgrade "$INSTALL_SPEC" >>"$LOG_FILE" 2>&1
    else
        info "Creating venv at $VENV_DIR"
        "$PYTHON" -m venv "$VENV_DIR" >>"$LOG_FILE" 2>&1
        info "Installing openpa from $INSTALL_SOURCE_LABEL (this may take a few minutes)"
        "$VENV_DIR/bin/pip" install --upgrade pip >>"$LOG_FILE" 2>&1
        "$VENV_DIR/bin/pip" install "$INSTALL_SPEC" >>"$LOG_FILE" 2>&1
    fi

    INSTALLED_VERSION="$("$VENV_DIR/bin/openpa" version 2>/dev/null | awk '{print $2}' || echo "?")"
    ok "Installed openpa $INSTALLED_VERSION"
fi

# ── shim & PATH ───────────────────────────────────────────────────────────

# Create a symlink in $BIN_DIR so a single, stable path on PATH points at
# the venv's `openpa`. This means re-installs (which can rebuild the venv)
# don't change what the user has on PATH.
mkdir -p "$BIN_DIR"
ln -sfn "$VENV_DIR/bin/openpa" "$BIN_DIR/openpa"
ok "Linked $BIN_DIR/openpa -> $VENV_DIR/bin/openpa"

# Append a marker block to the user's shell rc so $BIN_DIR is on PATH for
# every new shell. The block is idempotent (re-runs are no-ops) and uses a
# runtime guard so $PATH doesn't accumulate duplicates if the rc gets
# sourced more than once.
PATH_MARKER_BEGIN="# >>> openpa installer >>>"
PATH_MARKER_END="# <<< openpa installer <<<"

write_path_block_posix() {
    local rcfile="$1"
    if [ -f "$rcfile" ] && grep -q "^${PATH_MARKER_BEGIN}\$" "$rcfile" 2>/dev/null; then
        return 0   # already present
    fi
    {
        printf '\n%s\n' "$PATH_MARKER_BEGIN"
        printf 'case ":$PATH:" in\n'
        printf '    *":$HOME/.openpa/bin:"*) ;;\n'
        printf '    *) export PATH="$HOME/.openpa/bin:$PATH" ;;\n'
        printf 'esac\n'
        printf '%s\n' "$PATH_MARKER_END"
    } >> "$rcfile" 2>/dev/null
}

write_path_block_fish() {
    local rcfile="$1"
    if [ -f "$rcfile" ] && grep -q "^${PATH_MARKER_BEGIN}\$" "$rcfile" 2>/dev/null; then
        return 0
    fi
    mkdir -p "$(dirname "$rcfile")"
    {
        printf '\n%s\n' "$PATH_MARKER_BEGIN"
        printf 'if not contains $HOME/.openpa/bin $fish_user_paths\n'
        printf '    set -Ux fish_user_paths $HOME/.openpa/bin $fish_user_paths\n'
        printf 'end\n'
        printf '%s\n' "$PATH_MARKER_END"
    } >> "$rcfile" 2>/dev/null
}

install_path_entry() {
    local shellname rcfile written=""
    shellname="$(basename "${SHELL:-}")"
    case "$shellname" in
        zsh)
            rcfile="$HOME/.zshrc"
            ;;
        fish)
            rcfile="$HOME/.config/fish/config.fish"
            write_path_block_fish "$rcfile" && written="$rcfile"
            ;;
        bash)
            if [ -f "$HOME/.bashrc" ] || [ ! -f "$HOME/.bash_profile" ]; then
                rcfile="$HOME/.bashrc"
            else
                rcfile="$HOME/.bash_profile"
            fi
            ;;
        *)
            rcfile="$HOME/.profile"
            ;;
    esac
    if [ -z "$written" ] && [ -n "$rcfile" ]; then
        write_path_block_posix "$rcfile"
        written="$rcfile"
    fi
    # Also drop a copy into ~/.profile if it exists and we haven't already
    # touched it — covers GUI-launched terminals and `sh` subshells.
    if [ -f "$HOME/.profile" ] && [ "$written" != "$HOME/.profile" ]; then
        write_path_block_posix "$HOME/.profile"
    fi
    if [ -n "$written" ] && [ -f "$written" ] \
            && grep -q "^${PATH_MARKER_BEGIN}\$" "$written" 2>/dev/null; then
        ok "Added $BIN_DIR to PATH via $written"
    else
        warn "Couldn't write PATH entry (permission denied?). Add manually:"
        printf '    export PATH="%s:$PATH"\n' "$BIN_DIR" >&2
    fi
}

if [ "$MODIFY_PATH" -eq 1 ]; then
    install_path_entry
else
    info "Skipping PATH modification (--no-modify-path). Add manually:"
    printf '    export PATH="%s:$PATH"\n' "$BIN_DIR"
fi

# ── env file ──────────────────────────────────────────────────────────────

if [ ! -f "$ENV_FILE" ]; then
    info "Generating $ENV_FILE"
    case "$DEPLOYMENT" in
        local)
            fetch_template "local.env" > "$ENV_FILE"
            ;;
        container)
            fetch_template "container.env" > "$ENV_FILE"
            ;;
        server)
            # __APP_HOST__ is the only placeholder; the user-provided host
            # gets substituted as-is (validated against [a-zA-Z0-9.:-]+ above).
            fetch_template "server.env.tmpl" | sed "s|__APP_HOST__|$APP_HOST|g" > "$ENV_FILE"
            ;;
    esac
    ok "Wrote $ENV_FILE"
else
    info ".env already exists — keeping it. Edit $ENV_FILE if you need to."
fi

# Stamp the channel-specific keys into .env so the running app's upgrader
# reads them via the .env loader. Each write is idempotent (skipped if the
# key is already present) so re-runs don't accumulate duplicates and a
# user-customized value is preserved.
case "$CHANNEL" in
    production)
        if ! grep -q '^OPENPA_UPGRADE_CHANNEL=' "$ENV_FILE" 2>/dev/null; then
            printf '\nOPENPA_UPGRADE_CHANNEL=production\n' >> "$ENV_FILE"
        fi
        ;;
    test)
        if ! grep -q '^OPENPA_UPGRADE_CHANNEL=' "$ENV_FILE" 2>/dev/null; then
            printf '\nOPENPA_UPGRADE_CHANNEL=test\n' >> "$ENV_FILE"
        fi
        if ! grep -q '^OPENPA_PIP_INDEX_URL=' "$ENV_FILE" 2>/dev/null; then
            printf 'OPENPA_PIP_INDEX_URL=https://test.pypi.org/simple/\n' >> "$ENV_FILE"
        fi
        if ! grep -q '^OPENPA_PIP_EXTRA_INDEX_URL=' "$ENV_FILE" 2>/dev/null; then
            printf 'OPENPA_PIP_EXTRA_INDEX_URL=https://pypi.org/simple/\n' >> "$ENV_FILE"
        fi
        ;;
    dev)
        # Deliberately leave OPENPA_UPGRADE_CHANNEL unset. get_channel()
        # defaults to "production" when missing (app/upgrade/channel.py),
        # which is harmless here: ``openpa upgrade`` from a dev editable
        # install is a footgun anyway and the dev path is to ``git pull``.
        :
        ;;
esac

# ── bootstrap.toml (DB selection) ─────────────────────────────────────────

# Skip the default-SQLite bootstrap.toml when the Electron app is driving
# — the Setup Wizard will write the file once the user picks a backend,
# and the backend boots in deferred-storage mode until then so no DB is
# materialised under ~/.openpa/storage before the user has chosen.
# Native installs (curl | sh, no Electron) get the SQLite default here,
# matching the legacy behavior; the wizard can still flip them to
# Postgres on first setup.
if [ "${OPENPA_INSTALLER_FRONTEND:-}" != "electron" ]; then
    if [ ! -f "$BOOTSTRAP_FILE" ]; then
        info "Generating $BOOTSTRAP_FILE (SQLite, the recommended default)"
        cat > "$BOOTSTRAP_FILE" <<'EOF'
# Database selection. SQLite is the recommended default for native
# installs; switch to "postgres" via the setup wizard if you want a
# multi-process or networked DB.
db_provider = "sqlite"
EOF
        ok "Wrote $BOOTSTRAP_FILE"
    fi
fi

# ── migrate ───────────────────────────────────────────────────────────────

# Skip Alembic's ``upgrade head`` (and therefore creating the SQLite DB
# file) when the Electron app is driving — the app starts the backend
# only after the user clicks "Continue to Setup Wizard", and the backend
# is what eventually creates the DB. Keeping this here means a stray
# ~/.openpa/storage/openpa.db never shows up between the installer
# finishing and the user choosing to continue.
if [ "${OPENPA_INSTALLER_FRONTEND:-}" != "electron" ]; then
    info "Migrating database to current schema"
    "$VENV_DIR/bin/openpa" db upgrade >>"$LOG_FILE" 2>&1
    REVISION="$("$VENV_DIR/bin/openpa" db current 2>/dev/null || echo "?")"
    ok "Database at revision $REVISION"
fi

# ── start the server (foreground-detached for the install session) ───────

# Skip starting ``openpa serve`` when the Electron app is driving —
# the app spawns the backend itself once the user clicks "Continue to
# Setup Wizard", and that's also the point at which the SQLite DB is
# created (via the backend's own initialize() call).
SERVER_PID_FILE="$OPENPA_HOME/install.pid"
if [ "${OPENPA_INSTALLER_FRONTEND:-}" != "electron" ]; then
    step "Starting OpenPA"

    # We start the server in the background so the wizard URL works as
    # soon as we open the browser. The PID is recorded so the user can
    # stop it with `kill $(cat ~/.openpa/install.pid)`. Future Phase:
    # install a real service unit (systemd / launchd) so it survives
    # logout.

    # Source the .env early so HOST/PORT are honored both by the health
    # probe below and by the spawned server.
    set -a
    # shellcheck disable=SC1090
    . "$ENV_FILE"
    set +a

    SERVER_RUNNING=0
    if [ -f "$SERVER_PID_FILE" ] && kill -0 "$(cat "$SERVER_PID_FILE")" 2>/dev/null; then
        info "OpenPA is already running (pid $(cat "$SERVER_PID_FILE"))."
        SERVER_RUNNING=1
    fi

    # Detect a server bound to the port without going through this
    # installer — typical dev case: ``uv run openpa serve`` in a separate
    # terminal. Starting a second openpa would collide on the bind, so
    # treat it as already-running and skip the spawn.
    if [ "$SERVER_RUNNING" -eq 0 ] && curl -fsS "http://${HOST:-127.0.0.1}:${PORT:-1112}/health" >/dev/null 2>&1; then
        info "OpenPA is already responding at http://${HOST:-127.0.0.1}:${PORT:-1112} — skipping server start."
        SERVER_RUNNING=1
    fi

    if [ "$SERVER_RUNNING" -eq 0 ]; then
        nohup "$VENV_DIR/bin/openpa" serve >>"$OPENPA_HOME/server.log" 2>&1 &
        echo $! > "$SERVER_PID_FILE"
        ok "OpenPA started (pid $(cat "$SERVER_PID_FILE"), logs: $OPENPA_HOME/server.log)"
    fi
fi

# Wait briefly for the HTTP listener — if it doesn't come up the wizard
# will be a dead link, which is the worst end-user experience. Skipped
# under the Electron front-end since the app starts the backend later
# (on the Continue click) and waits for its own health probe there.
if [ "${OPENPA_INSTALLER_FRONTEND:-}" != "electron" ]; then
    HEALTH_URL="http://${HOST:-127.0.0.1}:${PORT:-1112}/health"
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        if curl -fsS "$HEALTH_URL" >/dev/null 2>&1; then
            break
        fi
        sleep 1
    done
fi

# ── wizard handoff ────────────────────────────────────────────────────────

# ``container`` mode binds to 0.0.0.0 inside the container, but the user
# browses from the docker host where the published port surfaces as
# localhost — so the wizard URL is the same as ``local``.
if [ "$DEPLOYMENT" = "server" ]; then
    WIZARD_URL="http://$APP_HOST:1515/#/setup"
else
    WIZARD_URL="http://localhost:1515/#/setup"
fi

# Suppress the human-handoff block when the Electron app is driving —
# the app navigates to the in-window wizard via vue-router and showing
# a "Wizard URL: ..." instruction in that context misleads the user.
if [ "${OPENPA_INSTALLER_FRONTEND:-}" != "electron" ]; then
    step "Setup wizard"

    cat <<EOF
The setup wizard is the next step. It collects your LLM API keys,
profile name, and tool preferences, then activates the server.

  Wizard URL: ${BOLD}$WIZARD_URL${RESET}
  Backend:    http://${HOST:-127.0.0.1}:${PORT:-1112}
  Stop:       kill \$(cat $SERVER_PID_FILE)

EOF

    # Activation guidance — front-and-center because bash can't update
    # the parent shell's PATH for us, and "command not found" right
    # after install is the most common user pitfall.
    if [ "$MODIFY_PATH" -eq 1 ]; then
        cat <<EOF
${BOLD}One last step — activate \`openpa\` in this shell:${RESET}

    ${BOLD}export PATH="$BIN_DIR:\$PATH"${RESET}

(New terminals pick this up automatically from your shell rc.)
Then run: ${BOLD}openpa --help${RESET}

EOF
    else
        cat <<EOF
${BOLD}One last step — put \`openpa\` on your PATH:${RESET}

    ${BOLD}export PATH="$BIN_DIR:\$PATH"${RESET}

To make this permanent, add the line above to your shell rc
(e.g. ~/.bashrc, ~/.zshrc).

EOF
    fi

    if [ "$NO_LAUNCH" -eq 0 ] && [ "$UNATTENDED" -eq 0 ]; then
        if command -v xdg-open >/dev/null 2>&1; then
            xdg-open "$WIZARD_URL" >/dev/null 2>&1 || true
        elif command -v open >/dev/null 2>&1; then
            open "$WIZARD_URL" >/dev/null 2>&1 || true
        fi
    fi
fi

case "$CHANNEL" in
    test) ok "Done. Welcome to OpenPA (test build)." ;;
    dev)  ok "Done. Welcome to OpenPA (dev install from $REPO_ROOT)." ;;
    *)    ok "Done. Welcome to OpenPA." ;;
esac

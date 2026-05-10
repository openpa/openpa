#!/usr/bin/env bash
# OpenPA TEST installer — Linux / macOS.
#
# Identical to install.sh except it pulls pre-release builds from
# **Test PyPI** (https://test.pypi.org) instead of production PyPI. Use
# this to validate a release candidate end-to-end before cutting a real
# tag.
#
# Usage:
#   curl -fsSL https://openpa.ai/install-test.sh | bash
#   curl -fsSL https://openpa.ai/install-test.sh | bash -s -- [flags]
#
# Flags: same as install.sh.
#
# Heads up: this installer shares ~/.openpa with the production
# installer. Running it on a host that already has prod openpa installed
# WILL upgrade/downgrade that install to the test version. Use
# OPENPA_WORKING_DIR=~/.openpa-test to keep them separate.

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

# ── flags ─────────────────────────────────────────────────────────────────

DEPLOYMENT=""
APP_HOST=""
MODE=""           # docker | native (default: prompt if Docker available)
NO_LAUNCH=0
UNATTENDED=0
REINSTALL=0

while [ $# -gt 0 ]; do
    case "$1" in
        --deployment)       DEPLOYMENT="$2"; shift 2 ;;
        --deployment=*)     DEPLOYMENT="${1#*=}"; shift ;;
        --host)             APP_HOST="$2"; shift 2 ;;
        --host=*)           APP_HOST="${1#*=}"; shift ;;
        --mode)             MODE="$2"; shift 2 ;;
        --mode=*)           MODE="${1#*=}"; shift ;;
        --docker)           MODE="docker"; shift ;;
        --native)           MODE="native"; shift ;;
        --no-launch)        NO_LAUNCH=1; shift ;;
        --unattended)       UNATTENDED=1; shift ;;
        --reinstall)        REINSTALL=1; shift ;;
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

if [ "$UNATTENDED" -eq 1 ] && [ -z "$DEPLOYMENT" ]; then
    DEPLOYMENT="local"
fi
if [ "$UNATTENDED" -eq 1 ] && [ -z "$APP_HOST" ] && [ "$DEPLOYMENT" = "server" ]; then
    err "--unattended with --deployment=server requires --host"
    exit 2
fi

# ── test-pypi config ──────────────────────────────────────────────────────

# Pip index URLs used for the native install and forwarded to the docker
# build via the docker-compose .env file. Test PyPI is the primary index
# (so ``pip install openpa`` resolves the test wheel); production PyPI is
# the fallback for transitive deps that don't live on Test PyPI.
TEST_PYPI_INDEX_URL="https://test.pypi.org/simple/"
PROD_PYPI_EXTRA_INDEX_URL="https://pypi.org/simple/"

# ── paths ─────────────────────────────────────────────────────────────────

# Same default as the prod installer. Use OPENPA_WORKING_DIR to install
# side-by-side (e.g., ~/.openpa-test) without clobbering a real install.
OPENPA_HOME="${OPENPA_WORKING_DIR:-$HOME/.openpa}"
VENV_DIR="$OPENPA_HOME/venv"
ENV_FILE="$OPENPA_HOME/.env"
BOOTSTRAP_FILE="$OPENPA_HOME/bootstrap.toml"
LOG_FILE="$OPENPA_HOME/install.log"

mkdir -p "$OPENPA_HOME"

TEMPLATE_BASE="${OPENPA_TEMPLATE_BASE:-https://raw.githubusercontent.com/openpa/openpa/main/install/templates}"

# ── banner ────────────────────────────────────────────────────────────────

cat <<EOF
${BOLD}${YELLOW}OpenPA TEST installer${RESET}
${DIM}Installs from $TEST_PYPI_INDEX_URL${RESET}
${DIM}Targets $OPENPA_HOME (will overwrite an existing install in this directory)${RESET}
${DIM}Logs: $LOG_FILE${RESET}

EOF

# ── detection ─────────────────────────────────────────────────────────────

step "Environment"

OS_NAME="$(uname -s)"
case "$OS_NAME" in
    Linux*)  OS=linux ;;
    Darwin*) OS=macos ;;
    *)
        err "Unsupported OS: $OS_NAME (this script handles Linux and macOS; use install-test.ps1 on Windows)"
        exit 1
        ;;
esac
ARCH="$(uname -m)"
ok "OS:   $OS ($ARCH)"

PYTHON=""
for candidate in python3.13 python3.14 python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
        ver="$("$candidate" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo "")"
        case "$ver" in
            3.13|3.14|3.15|3.16|3.17|3.18|3.19)
                PYTHON="$(command -v "$candidate")"
                break
                ;;
        esac
    fi
done

if [ -n "$PYTHON" ]; then
    ok "Python: $("$PYTHON" --version) at $PYTHON"
else
    info "Python: 3.13+ not found (only required for native mode)"
fi

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
    cat <<EOF
How will you run OpenPA?
  ${BOLD}1)${RESET} ${BOLD}local${RESET}   — bind to 127.0.0.1, only this machine can reach it
  ${BOLD}2)${RESET} ${BOLD}server${RESET}  — bind to all interfaces, reachable from other devices
EOF
    while :; do
        read -r -p "Choice [1]: " choice </dev/tty || choice=""
        case "${choice:-1}" in
            1|local)  DEPLOYMENT=local;  break ;;
            2|server) DEPLOYMENT=server; break ;;
            *) warn "Pick 1 or 2." ;;
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

if [ -z "$MODE" ]; then
    if [ "$HAS_DOCKER" -eq 1 ]; then
        if [ "$UNATTENDED" -eq 1 ]; then
            MODE="docker"
        else
            cat <<EOF

${BOLD}How do you want to run OpenPA?${RESET}
  ${BOLD}1)${RESET} ${BOLD}docker${RESET}  — sandboxed VNC desktop with bundled Postgres + Qdrant
                ${DIM}recommended; the agent gets its own GUI environment${RESET}
  ${BOLD}2)${RESET} ${BOLD}native${RESET}  — Python venv at $OPENPA_HOME/venv with SQLite
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

gen_secret() {
    LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 24 || true
    echo
}

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
        curl -fsSL "$TEMPLATE_BASE/docker-compose.yml.tmpl" \
            -o "$DOCKER_DIR/docker-compose.yml"

        info "Writing $DOCKER_DIR/.env (secrets, do not commit)"
        tmpl="$(mktemp)"
        curl -fsSL "$TEMPLATE_BASE/docker.env.tmpl" -o "$tmpl"
        sed \
            -e "s|__OPENPA_VERSION__|$OPENPA_VERSION|g" \
            -e "s|__OPENPA_UI_REF__|$OPENPA_UI_REF|g" \
            -e "s|__APP_URL__|$DOCKER_APP_URL|g" \
            -e "s|__CORS_ALLOWED_ORIGINS__|$DOCKER_CORS|g" \
            -e "s|__SETUP_WIZARD_ENV__|$DOCKER_WIZARD_ENV|g" \
            -e "s|__PG_PASSWORD__|$PG_PASSWORD|g" \
            -e "s|__VNC_PASSWORD__|$VNC_PASSWORD|g" \
            "$tmpl" > "$DOCKER_DIR/.env"
        # Append the Test PyPI index URLs so docker-compose forwards them
        # to the Dockerfile's pip install step. These keys are only
        # written by the test installer; the prod installer leaves them
        # unset (which the Dockerfile treats as "use default PyPI").
        cat >>"$DOCKER_DIR/.env" <<EOF
OPENPA_PIP_INDEX_URL=$TEST_PYPI_INDEX_URL
OPENPA_PIP_EXTRA_INDEX_URL=$PROD_PYPI_EXTRA_INDEX_URL
EOF
        chmod 600 "$DOCKER_DIR/.env"
        rm -f "$tmpl"

        ok "Wrote $DOCKER_DIR/docker-compose.yml + .env"
    fi

    info "Pulling images (this may take a few minutes the first time)"
    (cd "$DOCKER_DIR" && docker compose pull --ignore-pull-failures \
        >>"$LOG_FILE" 2>&1) || warn "Some images couldn't be pulled; will build locally."

    info "Starting bundle"
    (cd "$DOCKER_DIR" && docker compose up -d --build >>"$LOG_FILE" 2>&1)

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

    step "Setup wizard"

    WIZARD_URL="http://${HEALTH_HOST}:1515/#/setup"
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

    ok "Done. Welcome to OpenPA (test build)."
    exit 0
fi

# ── native install ────────────────────────────────────────────────────────

if [ -z "$PYTHON" ]; then
    err "Python 3.13 or newer is required for native mode but was not found."
    cat <<EOF >&2

Install options:
  ${BOLD}macOS${RESET}: brew install python@3.13
  ${BOLD}Ubuntu/Debian${RESET}: sudo apt install python3.13 python3.13-venv
  ${BOLD}Fedora/RHEL${RESET}: sudo dnf install python3.13
  ${BOLD}Any${RESET}: https://www.python.org/downloads/

Re-run this script after Python is on your PATH (or pass --mode docker).
EOF
    exit 1
fi

# ── existing install detection ────────────────────────────────────────────

step "Install"

if [ "$REINSTALL" -eq 1 ] && [ -d "$VENV_DIR" ]; then
    info "Removing existing venv (--reinstall): $VENV_DIR"
    rm -rf "$VENV_DIR"
fi

# Test installs use Test PyPI as the primary index and prod PyPI as a
# fallback (transitive deps like anthropic / openai / pandas only live on
# prod PyPI). ``--pre`` is required because the test wheel is a PEP 440
# pre-release (e.g. 0.1.5.dev1).
PIP_TEST_FLAGS=(
    --index-url "$TEST_PYPI_INDEX_URL"
    --extra-index-url "$PROD_PYPI_EXTRA_INDEX_URL"
    --pre
)

if [ -d "$VENV_DIR" ]; then
    info "Existing install detected at $VENV_DIR — upgrading in place."
    "$VENV_DIR/bin/pip" install --upgrade pip >>"$LOG_FILE" 2>&1
    "$VENV_DIR/bin/pip" install "${PIP_TEST_FLAGS[@]}" --upgrade openpa >>"$LOG_FILE" 2>&1
else
    info "Creating venv at $VENV_DIR"
    "$PYTHON" -m venv "$VENV_DIR" >>"$LOG_FILE" 2>&1
    info "Installing openpa from Test PyPI (this may take a few minutes)"
    "$VENV_DIR/bin/pip" install --upgrade pip >>"$LOG_FILE" 2>&1
    "$VENV_DIR/bin/pip" install "${PIP_TEST_FLAGS[@]}" openpa >>"$LOG_FILE" 2>&1
fi

INSTALLED_VERSION="$("$VENV_DIR/bin/opa" version 2>/dev/null | awk '{print $2}' || echo "?")"
ok "Installed openpa $INSTALLED_VERSION (test build)"

# ── env file ──────────────────────────────────────────────────────────────

if [ ! -f "$ENV_FILE" ]; then
    info "Generating $ENV_FILE"
    if [ "$DEPLOYMENT" = "local" ]; then
        curl -fsSL "$TEMPLATE_BASE/local.env" -o "$ENV_FILE"
    else
        tmpl="$(mktemp)"
        curl -fsSL "$TEMPLATE_BASE/server.env.tmpl" -o "$tmpl"
        sed "s|__APP_HOST__|$APP_HOST|g" "$tmpl" > "$ENV_FILE"
        rm -f "$tmpl"
    fi
    ok "Wrote $ENV_FILE"
else
    info ".env already exists — keeping it. Edit $ENV_FILE if you need to."
fi

# ── bootstrap.toml (DB selection) ─────────────────────────────────────────

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

# ── migrate ───────────────────────────────────────────────────────────────

info "Migrating database to current schema"
"$VENV_DIR/bin/opa" db upgrade >>"$LOG_FILE" 2>&1
REVISION="$("$VENV_DIR/bin/opa" db current 2>/dev/null || echo "?")"
ok "Database at revision $REVISION"

# ── start the server ──────────────────────────────────────────────────────

step "Starting OpenPA"

SERVER_PID_FILE="$OPENPA_HOME/install.pid"
if [ -f "$SERVER_PID_FILE" ] && kill -0 "$(cat "$SERVER_PID_FILE")" 2>/dev/null; then
    info "OpenPA is already running (pid $(cat "$SERVER_PID_FILE"))."
else
    set -a
    # shellcheck disable=SC1090
    . "$ENV_FILE"
    set +a
    nohup "$VENV_DIR/bin/opa" serve >>"$OPENPA_HOME/server.log" 2>&1 &
    echo $! > "$SERVER_PID_FILE"
    ok "OpenPA started (pid $(cat "$SERVER_PID_FILE"), logs: $OPENPA_HOME/server.log)"
fi

HEALTH_URL="http://${HOST:-127.0.0.1}:${PORT:-1112}/health"
for _ in 1 2 3 4 5 6 7 8 9 10; do
    if curl -fsS "$HEALTH_URL" >/dev/null 2>&1; then
        break
    fi
    sleep 1
done

# ── wizard handoff ────────────────────────────────────────────────────────

step "Setup wizard"

if [ "$DEPLOYMENT" = "local" ]; then
    WIZARD_URL="http://localhost:1515/#/setup"
else
    WIZARD_URL="http://$APP_HOST:1515/#/setup"
fi

cat <<EOF
The setup wizard is the next step. It collects your LLM API keys,
profile name, and tool preferences, then activates the server.

  Wizard URL: ${BOLD}$WIZARD_URL${RESET}
  Backend:    http://${HOST:-127.0.0.1}:${PORT:-1112}
  Stop:       kill \$(cat $SERVER_PID_FILE)
  Re-open:    "$VENV_DIR/bin/opa" serve

EOF

if [ "$NO_LAUNCH" -eq 0 ] && [ "$UNATTENDED" -eq 0 ]; then
    if command -v xdg-open >/dev/null 2>&1; then
        xdg-open "$WIZARD_URL" >/dev/null 2>&1 || true
    elif command -v open >/dev/null 2>&1; then
        open "$WIZARD_URL" >/dev/null 2>&1 || true
    fi
fi

ok "Done. Welcome to OpenPA (test build)."

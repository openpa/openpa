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
# Flags: same as install.sh, including --auto-install-python /
# --no-auto-install-python and --no-modify-path. Note: this installer
# defaults to --no-modify-path when OPENPA_WORKING_DIR points outside
# the canonical $HOME/.openpa, so a staging install never clobbers the
# prod PATH entry.
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
AUTO_INSTALL_PYTHON=""   # "" = ask interactively; "1" = yes; "0" = no
MODIFY_PATH=""           # set later: 1 if OPENPA_HOME is canonical; 0 otherwise

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
if [ "$UNATTENDED" -eq 1 ] && [ -z "$AUTO_INSTALL_PYTHON" ]; then
    AUTO_INSTALL_PYTHON=1
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
BIN_DIR="$OPENPA_HOME/bin"
UV_BIN="$BIN_DIR/uv"

# Scope pip's HTTP + wheel cache under our install dir. Critical for the
# test installer: rapid test-wheel iteration trips over Test PyPI's
# index-page caching at ~/.cache/pip/, which can keep pip pinned to an
# older test build even after `rm -rf ~/.openpa`.
export PIP_CACHE_DIR="$OPENPA_HOME/pip-cache"

# Default MODIFY_PATH: only modify PATH for the canonical install dir, so
# a staging install at ~/.openpa-test doesn't clobber prod's PATH entry.
# --modify-path / --no-modify-path override this.
if [ -z "$MODIFY_PATH" ]; then
    if [ "$OPENPA_HOME" = "$HOME/.openpa" ]; then
        MODIFY_PATH=1
    else
        MODIFY_PATH=0
    fi
fi

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

if [ -z "$PYTHON" ]; then
    prompt_for_python_install
    install_uv_locally
    install_python_via_uv
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

INSTALLED_VERSION="$("$VENV_DIR/bin/openpa" version 2>/dev/null | awk '{print $2}' || echo "?")"
ok "Installed openpa $INSTALLED_VERSION (test build)"

# ── shim & PATH ───────────────────────────────────────────────────────────

mkdir -p "$BIN_DIR"
ln -sfn "$VENV_DIR/bin/openpa" "$BIN_DIR/openpa"
ok "Linked $BIN_DIR/openpa -> $VENV_DIR/bin/openpa"

PATH_MARKER_BEGIN="# >>> openpa installer >>>"
PATH_MARKER_END="# <<< openpa installer <<<"

write_path_block_posix() {
    local rcfile="$1"
    if [ -f "$rcfile" ] && grep -q "^${PATH_MARKER_BEGIN}\$" "$rcfile" 2>/dev/null; then
        return 0
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
    info "Skipping PATH modification (test installer / --no-modify-path). Add manually if needed:"
    printf '    export PATH="%s:$PATH"\n' "$BIN_DIR"
fi

# Activation guidance — printed NOW (not at end-of-script) because bash
# can't update the parent shell's PATH for us, and a later step crashing
# would otherwise swallow this message before the user sees it.
if [ "$MODIFY_PATH" -eq 1 ]; then
    cat <<EOF

${BOLD}To use \`openpa\` in this shell, run:${RESET}

    ${BOLD}export PATH="$BIN_DIR:\$PATH"${RESET}

(New terminals pick this up automatically from your shell rc.)

EOF
else
    cat <<EOF

${BOLD}To put \`openpa\` on your PATH, run:${RESET}

    ${BOLD}export PATH="$BIN_DIR:\$PATH"${RESET}

Add that line to your shell rc (e.g. ~/.bashrc, ~/.zshrc) to make it
permanent.

EOF
fi

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

# Source the .env file so any HOST/PORT/OPENPA_* overrides are honored by
# both `openpa db upgrade` (some subcommands read settings during import)
# and the subsequent `openpa serve`.
set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a

info "Migrating database to current schema"
if ! "$VENV_DIR/bin/openpa" db upgrade >>"$LOG_FILE" 2>&1; then
    err "Database migration failed."
    printf '\n%sLast 20 lines of %s:%s\n' "$DIM" "$LOG_FILE" "$RESET" >&2
    tail -n 20 "$LOG_FILE" >&2
    printf '\n%sFull log:%s %s\n' "$BOLD" "$RESET" "$LOG_FILE" >&2
    printf '%sRetry with:%s %s\n\n' "$BOLD" "$RESET" "$VENV_DIR/bin/openpa db upgrade" >&2
    exit 1
fi
REVISION="$("$VENV_DIR/bin/openpa" db current 2>/dev/null || echo "?")"
ok "Database at revision $REVISION"

# ── start the server ──────────────────────────────────────────────────────

step "Starting OpenPA"

SERVER_PID_FILE="$OPENPA_HOME/install.pid"
if [ -f "$SERVER_PID_FILE" ] && kill -0 "$(cat "$SERVER_PID_FILE")" 2>/dev/null; then
    info "OpenPA is already running (pid $(cat "$SERVER_PID_FILE"))."
else
    # .env was sourced above (before migrate); HOST/PORT are already in env.
    nohup "$VENV_DIR/bin/openpa" serve >>"$OPENPA_HOME/server.log" 2>&1 &
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

EOF

if [ "$NO_LAUNCH" -eq 0 ] && [ "$UNATTENDED" -eq 0 ]; then
    if command -v xdg-open >/dev/null 2>&1; then
        xdg-open "$WIZARD_URL" >/dev/null 2>&1 || true
    elif command -v open >/dev/null 2>&1; then
        open "$WIZARD_URL" >/dev/null 2>&1 || true
    fi
fi

ok "Done. Welcome to OpenPA (test build)."

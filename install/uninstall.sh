#!/usr/bin/env bash
# OpenPA uninstaller — Linux / macOS.
#
# Removes OpenPA from this machine. Operates on two directories:
#
#   System Dir  ($OPENPA_SYSTEM_DIR / default ~/.openpa)
#       Runtime + user content: .env, bootstrap.toml, storage/, tokens/,
#       venv/, bin/, per-profile dirs (PERSONA, skills, documents,
#       browser-profile), server.log, upgrade artifacts, backups/.
#
#   Install Dir ($OPENPA_INSTALL_DIR / platform-conventional)
#       Install-time scratch: install.log, install.pid, docker/
#       (compose bundle), pip-cache/, uv-cache/, python/.
#
# Usage:
#   uninstall.sh                # interactive; prompts for keep vs purge
#   uninstall.sh --keep         # remove install scratch + binaries;
#                                 preserve .env, storage, tokens, etc.
#   uninstall.sh --purge        # remove EVERYTHING in System Dir + Install
#                                 Dir. For Docker: docker compose down -v.
#   uninstall.sh --system-dir DIR / --install-dir DIR
#                               # override env vars for one run.
#
# Detection: $INSTALL_DIR/docker/docker-compose.yml -> Docker mode;
#            $SYSTEM_DIR/venv -> Native mode.
#
# Behavior matrix:
#   --keep + native:  remove venv/, bin/, $INSTALL_DIR contents.
#                     Preserve $SYSTEM_DIR/.env, bootstrap.toml, storage/,
#                     tokens/, profile dirs.
#   --keep + docker:  docker compose down (volumes preserved); remove
#                     $INSTALL_DIR/docker and other scratch. Preserve
#                     $SYSTEM_DIR (the bind-mounted host data).
#   --purge + native: rm -rf both $SYSTEM_DIR and $INSTALL_DIR.
#   --purge + docker: docker compose down -v; rm -rf both dirs.
#
# The User Working Directory (server_config.user_working_dir, picked in
# the Setup Wizard) is NEVER touched.

set -e

MODE=""           # keep | purge
SYSTEM_DIR="${OPENPA_SYSTEM_DIR:-}"
INSTALL_DIR="${OPENPA_INSTALL_DIR:-}"

# Resolve defaults (must match install.sh / settings.py).
if [ -z "$SYSTEM_DIR" ]; then
    SYSTEM_DIR="$HOME/.openpa"
fi
if [ -z "$INSTALL_DIR" ]; then
    case "$(uname -s)" in
        Darwin) INSTALL_DIR="$HOME/Library/Application Support/OpenPA" ;;
        *)      INSTALL_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/openpa" ;;
    esac
fi

while [ $# -gt 0 ]; do
    case "$1" in
        --keep)
            MODE=keep
            shift
            ;;
        --purge)
            MODE=purge
            shift
            ;;
        --system-dir)
            SYSTEM_DIR="$2"
            shift 2
            ;;
        --install-dir)
            INSTALL_DIR="$2"
            shift 2
            ;;
        --help|-h)
            sed -n '2,/^set -e/p' "$0" | sed 's/^# \{0,1\}//' | sed '$d'
            exit 0
            ;;
        *)
            echo "Unknown flag: $1" >&2
            echo "Use --help for usage." >&2
            exit 2
            ;;
    esac
done

# Sanity: at least one of the two dirs must exist for there to be
# anything to uninstall.
if [ ! -d "$SYSTEM_DIR" ] && [ ! -d "$INSTALL_DIR" ]; then
    echo "Nothing to uninstall — neither $SYSTEM_DIR nor $INSTALL_DIR exists."
    exit 0
fi

# Detect install kind from on-disk markers.
KIND=""
if [ -f "$INSTALL_DIR/docker/docker-compose.yml" ]; then
    KIND=docker
elif [ -d "$SYSTEM_DIR/venv" ]; then
    KIND=native
elif [ -d "$INSTALL_DIR" ]; then
    # Install Dir exists but no compose file and no venv — partial install
    # (script crashed mid-run, or already partly uninstalled). Treat as
    # native so we still clean up whatever's there.
    KIND=native
else
    echo "Unrecognized install layout." >&2
    echo "Expected $SYSTEM_DIR/venv (native) or $INSTALL_DIR/docker/docker-compose.yml (docker)." >&2
    exit 1
fi

# Interactive prompt if no mode flag passed.
if [ -z "$MODE" ]; then
    printf 'Uninstall OpenPA (%s):\n' "$KIND"
    printf '  System Dir:  %s\n' "$SYSTEM_DIR"
    printf '  Install Dir: %s\n\n' "$INSTALL_DIR"
    printf '  [k] Keep data    — remove the binaries + install scratch; preserve System Dir contents\n'
    printf '                     (.env, bootstrap.toml, storage/, tokens/, profile dirs)\n'
    printf '  [p] Purge all    — delete everything OpenPA installed'
    if [ "$KIND" = "docker" ]; then
        printf ' (incl. Docker volumes)'
    fi
    printf '\n'
    printf '  [c] Cancel\n\n> '
    read -r ans
    case "$ans" in
        k|K) MODE=keep ;;
        p|P) MODE=purge ;;
        *)
            echo "Cancelled."
            exit 0
            ;;
    esac
fi

# Pre-purge guard: refuse if the User Working Directory resolves equal to
# or inside the System Dir. Best-effort — only runs when sqlite3 is on PATH.
USER_DIR=""
if [ "$MODE" = "purge" ] && command -v sqlite3 >/dev/null 2>&1; then
    USER_DIR=$(sqlite3 "$SYSTEM_DIR/storage/openpa.db" \
        "select value from server_config where key='user_working_dir'" 2>/dev/null || true)
    if [ -n "$USER_DIR" ]; then
        rp_user=$(realpath -m "$USER_DIR" 2>/dev/null || echo "$USER_DIR")
        rp_sys=$(realpath -m "$SYSTEM_DIR" 2>/dev/null || echo "$SYSTEM_DIR")
        case "$rp_user" in
            "$rp_sys"|"$rp_sys"/*)
                # User Working Dir is inside the System Dir. That's the
                # expected case for default installs (~/.openpa is both),
                # so don't refuse — just warn.
                ;;
            *)
                # User picked a custom path outside the System Dir. Preserved
                # automatically since we only touch the two managed dirs.
                ;;
        esac
    fi
fi

# Stop any backend tracked via install.pid. Best-effort — a stale PID file
# from a crashed install just no-ops here.
if [ -f "$INSTALL_DIR/install.pid" ]; then
    pid=$(cat "$INSTALL_DIR/install.pid" 2>/dev/null || true)
    if [ -n "$pid" ]; then
        kill "$pid" 2>/dev/null || true
        for _ in 1 2 3 4 5; do
            kill -0 "$pid" 2>/dev/null || break
            sleep 1
        done
        kill -9 "$pid" 2>/dev/null || true
    fi
fi
# Also stop the long-running server (writes server.pid in System Dir
# after Setup Wizard completes).
if [ -f "$SYSTEM_DIR/server.pid" ]; then
    pid=$(cat "$SYSTEM_DIR/server.pid" 2>/dev/null || true)
    [ -n "$pid" ] && kill "$pid" 2>/dev/null || true
fi

# Docker container/volume cleanup.
if [ "$KIND" = "docker" ]; then
    if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
        if [ -d "$INSTALL_DIR/docker" ]; then
            if [ "$MODE" = "purge" ]; then
                echo "Stopping containers and removing volumes..."
                (cd "$INSTALL_DIR/docker" && docker compose -p openpa down -v --remove-orphans) || true
            else
                echo "Stopping containers (volumes preserved)..."
                (cd "$INSTALL_DIR/docker" && docker compose -p openpa down --remove-orphans) || true
            fi
        fi
    else
        echo "Warning: Docker daemon unreachable; containers/volumes left running." >&2
        echo "         Run 'docker compose -p openpa down -v' manually when Docker is available." >&2
    fi
fi

# Remove PATH-marker blocks from shell rc files. The install script writes
# a `# >>> openpa installer >>>` / `# <<< openpa installer <<<` marker pair
# around the PATH export. sed -i.bak then rm -f .bak works on both GNU
# sed and BSD sed (macOS).
PATH_MARKER_BEGIN='# >>> openpa installer >>>'
PATH_MARKER_END='# <<< openpa installer <<<'
for rc in \
    "$HOME/.bashrc" \
    "$HOME/.zshrc" \
    "$HOME/.bash_profile" \
    "$HOME/.profile" \
    "$HOME/.config/fish/config.fish"
do
    if [ -f "$rc" ] && grep -q "^${PATH_MARKER_BEGIN}\$" "$rc" 2>/dev/null; then
        sed -i.bak "/^${PATH_MARKER_BEGIN}\$/,/^${PATH_MARKER_END}\$/d" "$rc" 2>/dev/null || true
        rm -f "$rc.bak"
        echo "Removed PATH block from $rc"
    fi
done

# Apply the chosen mode.
if [ "$MODE" = "purge" ]; then
    # Belt-and-suspenders: refuse to purge a stray empty or root path.
    case "$SYSTEM_DIR" in
        /|"")
            echo "Refusing to purge a root-like System Dir: '$SYSTEM_DIR'" >&2
            exit 1
            ;;
    esac
    case "$INSTALL_DIR" in
        /|"")
            echo "Refusing to purge a root-like Install Dir: '$INSTALL_DIR'" >&2
            exit 1
            ;;
    esac
    [ -d "$SYSTEM_DIR" ]  && rm -rf "$SYSTEM_DIR"  && echo "Removed $SYSTEM_DIR."
    [ -d "$INSTALL_DIR" ] && rm -rf "$INSTALL_DIR" && echo "Removed $INSTALL_DIR."
    if [ -n "$USER_DIR" ]; then
        case "$USER_DIR" in
            "$SYSTEM_DIR"|"$SYSTEM_DIR"/*) ;;
            *) echo "User Working Directory preserved at: $USER_DIR" ;;
        esac
    fi
else
    # Keep mode: remove install scratch + Native binaries; preserve System
    # Dir contents (.env, bootstrap.toml, storage/, tokens/, profile dirs).
    rm -rf \
        "$SYSTEM_DIR/venv" \
        "$SYSTEM_DIR/bin" \
        "$SYSTEM_DIR/server.pid"
    # Wipe the Install Dir wholesale — it's all install scratch.
    [ -d "$INSTALL_DIR" ] && rm -rf "$INSTALL_DIR" && echo "Removed install scratch at $INSTALL_DIR."
    echo "Kept data in $SYSTEM_DIR (.env, bootstrap.toml, storage/, tokens/, profile dirs)."
fi

echo "Uninstall complete."

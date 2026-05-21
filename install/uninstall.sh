#!/usr/bin/env bash
# OpenPA uninstaller — Linux / macOS.
#
# Removes OpenPA's install and runtime artifacts from the System Directory.
# The User Working Directory (the user's documents / agent CWD) is NEVER
# touched, regardless of mode.
#
# Usage:
#   uninstall.sh                # interactive; prompts for keep vs purge
#   uninstall.sh --keep         # remove binaries; keep .env, storage, tokens
#   uninstall.sh --purge        # wipe the System Dir entirely + docker volumes
#   uninstall.sh --system-dir DIR
#                               # override OPENPA_SYSTEM_DIR for one run
#
# Detects the install kind from disk:
#   - Docker:  $SYSTEM_DIR/docker/docker-compose.yml present
#   - Native:  $SYSTEM_DIR/venv/ present
#
# Behavior per (mode, kind):
#   --keep + native: remove venv/, bin/, pip-cache/, install.pid, install.log
#                    Preserves .env, bootstrap.toml, storage/, tokens/.
#   --keep + docker: docker compose down (volumes preserved); remove docker/,
#                    install.pid, install.log. Preserves .env, storage/, tokens/.
#   --purge + native: rm -rf the entire System Dir.
#   --purge + docker: docker compose down -v (volumes destroyed), rm -rf System Dir.

set -e

MODE=""           # keep | purge
SYSTEM_DIR="${OPENPA_SYSTEM_DIR:-}"

# Resolve default System Dir (must match install.sh / settings.py).
if [ -z "$SYSTEM_DIR" ]; then
    case "$(uname -s)" in
        Darwin) SYSTEM_DIR="$HOME/Library/Application Support/OpenPA" ;;
        *)      SYSTEM_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/openpa" ;;
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

if [ ! -d "$SYSTEM_DIR" ]; then
    echo "Nothing to uninstall — System Directory not found at $SYSTEM_DIR"
    exit 0
fi

# Detect install kind. Docker wins if both markers exist (a previous native
# install whose dir was reused for Docker).
if [ -f "$SYSTEM_DIR/docker/docker-compose.yml" ]; then
    KIND=docker
elif [ -d "$SYSTEM_DIR/venv" ]; then
    KIND=native
else
    echo "Unrecognized install layout at $SYSTEM_DIR." >&2
    echo "Expected $SYSTEM_DIR/venv (native) or $SYSTEM_DIR/docker/docker-compose.yml (docker)." >&2
    exit 1
fi

# Interactive prompt if no mode flag passed.
if [ -z "$MODE" ]; then
    printf 'Uninstall OpenPA (%s) from:\n  %s\n\n' "$KIND" "$SYSTEM_DIR"
    printf '  [k] Keep data   — remove the binaries; preserve .env, storage, tokens\n'
    printf '  [p] Purge all   — delete the System Directory'
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

# Pre-purge guard: refuse if the user's working directory is inside the
# System Dir. We can't safely rm -rf the System Dir if the user has put
# their own files inside it (or set their User Working Directory to a
# subpath). Best-effort — only runs when sqlite3 is available.
USER_DIR=""
if [ "$MODE" = "purge" ] && command -v sqlite3 >/dev/null 2>&1; then
    USER_DIR=$(sqlite3 "$SYSTEM_DIR/storage/openpa.db" \
        "select value from server_config where key='user_working_dir'" 2>/dev/null || true)
    if [ -n "$USER_DIR" ]; then
        # realpath -m so non-existent paths still resolve (don't fail the check).
        rp_user=$(realpath -m "$USER_DIR" 2>/dev/null || echo "$USER_DIR")
        rp_sys=$(realpath -m "$SYSTEM_DIR" 2>/dev/null || echo "$SYSTEM_DIR")
        case "$rp_user" in
            "$rp_sys"|"$rp_sys"/*)
                echo "Refusing to purge: User Working Directory ($USER_DIR) is inside the System Dir." >&2
                echo "Move your data out of $SYSTEM_DIR (or pick a different User Working Directory) first." >&2
                exit 1
                ;;
        esac
    fi
fi

# Stop any backend process tracked via install.pid. Best-effort — a stale
# PID file from a crashed install just no-ops here.
if [ -f "$SYSTEM_DIR/install.pid" ]; then
    pid=$(cat "$SYSTEM_DIR/install.pid" 2>/dev/null || true)
    if [ -n "$pid" ]; then
        kill "$pid" 2>/dev/null || true
        # Give it a moment to exit cleanly before we yank files out from under it.
        for _ in 1 2 3 4 5; do
            kill -0 "$pid" 2>/dev/null || break
            sleep 1
        done
        kill -9 "$pid" 2>/dev/null || true
    fi
fi

# Docker container/volume cleanup.
if [ "$KIND" = "docker" ]; then
    if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
        if [ "$MODE" = "purge" ]; then
            echo "Stopping containers and removing volumes..."
            (cd "$SYSTEM_DIR/docker" && docker compose -p openpa down -v --remove-orphans) || true
        else
            echo "Stopping containers (volumes preserved)..."
            (cd "$SYSTEM_DIR/docker" && docker compose -p openpa down --remove-orphans) || true
        fi
    else
        echo "Warning: Docker daemon unreachable; containers/volumes left running." >&2
        echo "         Run 'docker compose -p openpa down -v' manually when Docker is available." >&2
    fi
fi

# Remove PATH-marker blocks from shell rc files. Idempotent — sed -i with
# the marker range deletes the whole block; a .bak file is left then removed
# so the operation works on both GNU sed and BSD sed (macOS).
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
    # Belt-and-suspenders: if someone passed --system-dir / and the
    # resolved path is suspicious, refuse. (Defense against a stray
    # '/' or empty SYSTEM_DIR.)
    case "$SYSTEM_DIR" in
        /|"")
            echo "Refusing to purge a root-like path: '$SYSTEM_DIR'" >&2
            exit 1
            ;;
    esac
    rm -rf "$SYSTEM_DIR"
    echo "Removed $SYSTEM_DIR."
    if [ -n "$USER_DIR" ]; then
        echo "User Working Directory preserved at: $USER_DIR"
    fi
else
    # Keep mode: remove only the binaries and ephemeral state.
    rm -rf \
        "$SYSTEM_DIR/venv" \
        "$SYSTEM_DIR/bin" \
        "$SYSTEM_DIR/pip-cache" \
        "$SYSTEM_DIR/uv-cache" \
        "$SYSTEM_DIR/python" \
        "$SYSTEM_DIR/install.pid" \
        "$SYSTEM_DIR/server.pid" \
        "$SYSTEM_DIR/install.log" \
        "$SYSTEM_DIR/server.log" \
        "$SYSTEM_DIR/server.err.log" \
        "$SYSTEM_DIR/.upgrade.lock" \
        "$SYSTEM_DIR/.upgrade.status.json" \
        "$SYSTEM_DIR/upgrade.log" \
        "$SYSTEM_DIR/upgrade-detached.log"
    # Docker keep-data: drop the compose bundle so a reinstall regenerates it
    # against the current templates. Volumes were preserved by ``compose down``.
    if [ "$KIND" = "docker" ]; then
        rm -rf "$SYSTEM_DIR/docker"
    fi
    echo "Kept data in $SYSTEM_DIR (.env, bootstrap.toml, storage/, tokens/, profile dirs)."
fi

echo "Uninstall complete."

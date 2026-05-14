#!/bin/bash
# Entrypoint for openpa/openpa-desktop.
#
# Starts four services and waits on all of them. If any of them dies the
# entrypoint exits, and Docker's restart policy brings the container
# back up — that's the supervision strategy. We keep it deliberately
# minimal (no supervisord, no s6) because the cost of getting one
# wrong outweighs the value at this scale.
#
# Order of operations:
#
#   1. Set the VNC password and clean stale lock files.
#   2. Start the X server (TigerVNC display :0) and websockify (noVNC web).
#   3. Run ``openpa db upgrade`` so a fresh DB gets the schema and an
#      existing one picks up new migrations on every restart.
#   4. Start ``openpa serve`` on :1112, inheriting DISPLAY=:0 so the agent
#      can drive the desktop. The same process opens a second listener
#      on :1515 for the SPA (OPENPA_UI_DIR points at the stage-1 build).
#   5. ``wait -n`` exits as soon as any background job dies; Docker
#      then restarts the whole container.
#
# Note: there is no longer a pre-start ``wait_for postgres / qdrant``
# block. The Setup Wizard now provisions sidecar services on demand
# (via ``docker compose up -d`` over the mounted Docker socket) and
# waits on each one before completing setup — so by the time
# ``openpa serve`` needs them, they're up.

set -e

# ── 0. Seed /opt/openpa/venv from baseline ───────────────────────────────
#
# The image builds a thin-core venv at /opt/openpa-baseline/venv; the
# runtime venv at /opt/openpa/venv is volume-mounted (named volume
# ``openpa-venv`` in docker-compose) so wizard-installed extras
# persist across container recreation.
#
# On a fresh container the volume is empty and we copy the baseline
# in. On subsequent boots the copy is skipped — anything the user
# installed via the Setup Wizard (sentence-transformers, qdrant,
# anthropic, ...) is preserved.
#
# The marker we check for is the ``openpa`` console script itself; if
# it's there, the venv is initialised. ``cp -a`` preserves the symlinks
# the venv uses to point at /usr/bin/python3.13.
#
# Note the ``/.`` suffix on the source: the named volume mount creates
# ``/opt/openpa/venv`` as an empty directory before the entrypoint runs,
# so ``cp -a SRC DST`` would copy SRC *into* DST (producing
# ``/opt/openpa/venv/venv/bin/openpa``). ``SRC/.`` copies the contents
# instead, landing the binary at the expected ``/opt/openpa/venv/bin/openpa``.
if [ ! -x /opt/openpa/venv/bin/openpa ]; then
    if [ ! -x /opt/openpa-baseline/venv/bin/openpa ]; then
        echo "ERROR: no baseline venv at /opt/openpa-baseline/venv" >&2
        exit 1
    fi
    echo "[entrypoint] seeding /opt/openpa/venv from baseline..."
    mkdir -p /opt/openpa/venv
    cp -a /opt/openpa-baseline/venv/. /opt/openpa/venv/

    # Re-target absolute paths from baseline → runtime. The image
    # built the venv at /opt/openpa-baseline/venv, so pyvenv.cfg,
    # activate scripts, and every pip-generated console-script
    # shebang carry that path verbatim. Without this rewrite,
    # /opt/openpa/venv/bin/openpa still invokes
    # /opt/openpa-baseline/venv/bin/python3.13 — and Python resolves
    # its venv from that path, so runtime pip installs land in the
    # in-image baseline site-packages instead of the openpa-venv
    # volume, where they'd vanish on the next container recreation.
    grep -rlF "/opt/openpa-baseline/venv" /opt/openpa/venv/bin /opt/openpa/venv/pyvenv.cfg 2>/dev/null \
        | xargs -r sed -i "s|/opt/openpa-baseline/venv|/opt/openpa/venv|g"
fi

# ── 1. VNC setup ──────────────────────────────────────────────────────────

if [[ ! "$RESOLUTION" =~ ^[0-9]+x[0-9]+$ ]]; then
    echo "ERROR: RESOLUTION must be WIDTHxHEIGHT (e.g., 1280x720)" >&2
    exit 1
fi

mkdir -p /root/.vnc
echo "$VNC_PASSWORD" | vncpasswd -f > /root/.vnc/passwd
chmod 600 /root/.vnc/passwd

rm -f /tmp/.X0-lock /tmp/.X11-unix/X0 2>/dev/null || true

# ── 2. X server + noVNC ──────────────────────────────────────────────────

echo "[entrypoint] starting vncserver at $RESOLUTION..."
vncserver :0 -geometry "$RESOLUTION" -depth 24 -localhost no

echo "[entrypoint] starting websockify on :80..."
# Foreground (no -D) so it shows up as a child of this script and ``wait``
# can reap it. Backgrounded with & for the same reason.
websockify --web=/usr/share/novnc/ 80 localhost:5900 \
    > /var/log/websockify.log 2>&1 &

# DISPLAY is what makes the rest of the children GUI-capable. The agent
# inherits this and can run Chromium, file managers, anything that
# expects an X server.
export DISPLAY=:0

# ── 3. Schema migration ──────────────────────────────────────────────────

# Only run migrations when bootstrap.toml already exists. On a fresh
# container the Setup Wizard hasn't been completed yet, so we don't
# know which DB provider the user will pick — running ``openpa db
# upgrade`` here would write ``bootstrap.toml`` with the wrong default
# (sqlite) and then crash trying to migrate the wrong engine. The
# wizard's first-setup endpoint calls the deferred-boot path which
# runs migrations against the chosen provider.
#
# For an existing install (volume already has bootstrap.toml), this
# branch idempotently applies any new migrations on every boot.
if [ -f "${OPENPA_WORKING_DIR:-/root/.openpa}/bootstrap.toml" ]; then
    echo "[entrypoint] running db migrations..."
    openpa db upgrade
else
    echo "[entrypoint] no bootstrap.toml — deferring DB init to the Setup Wizard."
fi

# ── 4. OpenPA backend (now also serves the SPA on :OPENPA_UI_PORT) ───────

# ``openpa serve`` opens two listeners in the same process: the API on
# :PORT and the SPA on :OPENPA_UI_PORT (set in the Dockerfile env to
# /opt/openpa-ui from the spa-builder stage). Hash routing means
# StaticFiles' ``html=True`` fallback to index.html covers every
# client-side route — no rewrite rules needed.
echo "[entrypoint] starting openpa serve (API :$PORT, SPA :${OPENPA_UI_PORT:-1515})..."
# tee to both the container's stdout (so ``docker logs`` surfaces
# startup crashes — without this, an import error or missing-dep
# failure would die into /var/log/openpa.log and the container would
# silently restart-loop) and the on-disk log file (so a VNC user can
# tail it from a terminal inside the desktop session).
openpa serve --host "$HOST" --port "$PORT" 2>&1 \
    | tee /var/log/openpa.log &

# ── 5. Watchdog ───────────────────────────────────────────────────────────

cat <<EOF
================================================================
 OpenPA-Desktop ready.
   Backend      : http://<host>:1112  (mapped from this container)
   Web UI       : http://<host>:1515  (served by openpa serve)
   noVNC        : http://<host>:6080/vnc.html  (VNC password set)
   Resolution   : $RESOLUTION
================================================================
EOF

# Exit on the first child failure so Docker's restart policy can recover.
# Without ``wait -n`` we'd silently keep running with a dead service.
wait -n
echo "[entrypoint] a child process exited; the container will restart." >&2
exit 1

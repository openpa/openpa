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
#   3. Wait for Postgres + Qdrant — opa serve refuses to come up if its
#      stores are unreachable, so this avoids a noisy crash loop.
#   4. Run ``opa db upgrade`` so a fresh DB gets the schema and an
#      existing one picks up new migrations on every restart.
#   5. Start ``opa serve`` on :1112, inheriting DISPLAY=:0 so the agent
#      can drive the desktop. The same process opens a second listener
#      on :1515 for the SPA (OPENPA_UI_DIR points at the stage-1 build).
#   6. ``wait -n`` exits as soon as any background job dies; Docker
#      then restarts the whole container.

set -e

# ── 1. VNC setup ──────────────────────────────────────────────────────────

if [[ ! "$RESOLUTION" =~ ^[0-9]+x[0-9]+$ ]]; then
    echo "ERROR: RESOLUTION must be WIDTHxHEIGHT (e.g., 1920x1080)" >&2
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

# ── 3. Wait for Postgres + Qdrant ─────────────────────────────────────────

wait_for() {
    local host="$1" port="$2" name="$3"
    local timeout="${4:-60}"
    echo "[entrypoint] waiting for $name at $host:$port ..."
    local i
    for ((i = 0; i < timeout; i++)); do
        if nc -z "$host" "$port" >/dev/null 2>&1; then
            echo "[entrypoint] $name is up"
            return 0
        fi
        sleep 1
    done
    echo "[entrypoint] timeout waiting for $name at $host:$port" >&2
    return 1
}

if [ "$OPENPA_DB_PROVIDER" = "postgres" ]; then
    wait_for "${OPENPA_POSTGRES_HOST:-postgres}" "${OPENPA_POSTGRES_PORT:-5432}" "postgres"
fi
if [ -n "${QDRANT_HOST:-}" ]; then
    wait_for "$QDRANT_HOST" "${QDRANT_PORT:-6333}" "qdrant" 30 || true
fi

# ── 4. Schema migration ──────────────────────────────────────────────────

# Idempotent on every boot — fresh DB gets the baseline, existing DB
# gets any new migrations. Failure here is fatal: if we can't reach
# the schema we want, opa serve will crash anyway.
echo "[entrypoint] running db migrations..."
opa db upgrade

# ── 5. OpenPA backend (now also serves the SPA on :OPENPA_UI_PORT) ───────

# ``opa serve`` opens two listeners in the same process: the API on
# :PORT and the SPA on :OPENPA_UI_PORT (set in the Dockerfile env to
# /opt/openpa-ui from the spa-builder stage). Hash routing means
# StaticFiles' ``html=True`` fallback to index.html covers every
# client-side route — no rewrite rules needed.
echo "[entrypoint] starting opa serve (API :$PORT, SPA :${OPENPA_UI_PORT:-1515})..."
opa serve --host "$HOST" --port "$PORT" \
    > /var/log/openpa.log 2>&1 &

# ── 6. Watchdog ───────────────────────────────────────────────────────────

cat <<EOF
================================================================
 OpenPA-Desktop ready.
   Backend     : http://<host>:1112  (mapped from this container)
   Web UI      : http://<host>:1515  (served by opa serve)
   noVNC       : http://<host>:6080/vnc.html  (VNC password set)
   Resolution  : $RESOLUTION
   DB provider : ${OPENPA_DB_PROVIDER:-sqlite}
================================================================
EOF

# Exit on the first child failure so Docker's restart policy can recover.
# Without ``wait -n`` we'd silently keep running with a dead service.
wait -n
echo "[entrypoint] a child process exited; the container will restart." >&2
exit 1

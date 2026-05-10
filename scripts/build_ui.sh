#!/usr/bin/env bash
# Build the openpa-ui SPA and stage it into ``app/static/ui/`` so the
# wheel produced by ``hatch build`` carries the built UI alongside the
# server.
#
# Run this:
#   - In CI before ``hatch build`` (so released wheels include the SPA).
#   - Locally before ``pip install -e .`` if you want the SPA served
#     by ``openpa serve`` from a checkout.
#
# Sources, in priority order:
#   1. OPENPA_UI_LOCAL=/path/to/openpa-ui   — use a local checkout
#                                             (skips the git clone).
#   2. OPENPA_UI_REPO + OPENPA_UI_REF       — clone from a fork / tag.
#                                             Defaults to the public
#                                             repo @ main.
#
# Output layout:
#   app/static/ui/
#     index.html
#     assets/...
#     .built-at        ← timestamp marker, also doubles as a sentinel
#                        for ``openpa serve``'s "is the SPA bundled?"
#                        check.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STATIC_DIR="$REPO_ROOT/app/static/ui"

UI_LOCAL="${OPENPA_UI_LOCAL:-}"
UI_REPO="${OPENPA_UI_REPO:-https://github.com/openpa/openpa-ui.git}"
UI_REF="${OPENPA_UI_REF:-main}"

cleanup_dir=""
trap '[ -n "$cleanup_dir" ] && rm -rf "$cleanup_dir"' EXIT

if [ -n "$UI_LOCAL" ]; then
    if [ ! -d "$UI_LOCAL" ]; then
        echo "OPENPA_UI_LOCAL=$UI_LOCAL is not a directory" >&2
        exit 1
    fi
    src="$UI_LOCAL"
    echo "[build_ui] using local checkout at $src"
else
    cleanup_dir="$(mktemp -d)"
    src="$cleanup_dir/openpa-ui"
    echo "[build_ui] cloning $UI_REPO @ $UI_REF"
    git clone --depth 1 --branch "$UI_REF" "$UI_REPO" "$src"
fi

# Pin Node deps with ``ci`` (lockfile-strict) so the wheel build is
# reproducible across runs. Fall back to a fresh ``npm install`` if
# ``ci`` fails — this works around npm/cli#4828, where lockfiles
# generated on one platform omit optional-dep entries for other
# platforms (e.g. a Windows-generated lockfile has no entry for
# @rollup/rollup-linux-x64-gnu, breaking ``npm ci`` on Linux CI).
if [ -f "$src/package-lock.json" ]; then
    if ! (cd "$src" && npm ci); then
        echo "[build_ui] npm ci failed; regenerating with fresh npm install (npm/cli#4828 workaround)"
        (cd "$src" && rm -rf node_modules package-lock.json && npm install)
    fi
else
    echo "[build_ui] WARNING: no package-lock.json; falling back to npm install."
    (cd "$src" && npm install)
fi

# ``web:build`` (not ``build``) runs the Electron-free build that emits
# a static SPA at dist-web/. The ``web:`` flavor leaves VITE_AGENT_URL
# unset, which is what we want — runtimeConfig.ts derives the agent URL
# from window.location at runtime so the same bundle works for any host.
#
# If the build fails, retry once after a clean reinstall. npm/cli#4828
# can leave optional native modules unresolved even when ``npm ci``
# exits 0 — rollup then crashes at runtime trying to require
# @rollup/rollup-linux-x64-gnu. Wiping node_modules + lockfile and
# running ``npm install`` forces a fresh resolve that picks up the
# host's optional binaries.
if ! (cd "$src" && npm run web:build); then
    echo "[build_ui] web:build failed; regenerating deps and retrying (npm/cli#4828 workaround)"
    (cd "$src" && rm -rf node_modules package-lock.json && npm install)
    (cd "$src" && npm run web:build)
fi

DIST="$src/dist-web"
if [ ! -f "$DIST/index.html" ]; then
    echo "[build_ui] expected $DIST/index.html after npm run web:build, but it's missing" >&2
    exit 1
fi

echo "[build_ui] copying $DIST/ → $STATIC_DIR/"
rm -rf "$STATIC_DIR"
mkdir -p "$STATIC_DIR"
cp -R "$DIST/." "$STATIC_DIR/"

date '+%Y-%m-%dT%H:%M:%SZ' > "$STATIC_DIR/.built-at"
echo "[build_ui] done."

"""Sync ui/package.json's "version" field from app/__version__.py.

The backend version in app/__version__.py is the single source of truth.
This script copies it into ui/package.json so the Electron app, the Vite
bundle, and the wheel all advertise the same version.

Wired into ui/package.json as prebuild/preweb:build/predev hooks so it
runs automatically before any UI build. Also safe to invoke manually:
    python scripts/sync_ui_version.py

Idempotent — exits 0 with a no-op message when the versions already match.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_VERSION_FILE = REPO_ROOT / "app" / "__version__.py"
UI_PACKAGE_JSON = REPO_ROOT / "ui" / "package.json"

VERSION_RE = re.compile(r'^__version__\s*=\s*"([^"]+)"', re.MULTILINE)


def read_backend_version() -> str:
    text = BACKEND_VERSION_FILE.read_text(encoding="utf-8")
    match = VERSION_RE.search(text)
    if not match:
        raise SystemExit(f"Could not find __version__ in {BACKEND_VERSION_FILE}")
    return match.group(1)


def main() -> int:
    backend_version = read_backend_version()
    package = json.loads(UI_PACKAGE_JSON.read_text(encoding="utf-8"))
    current = package.get("version")

    if current == backend_version:
        print(f"[sync_ui_version] ui/package.json already at {backend_version}")
        return 0

    package["version"] = backend_version
    UI_PACKAGE_JSON.write_text(
        json.dumps(package, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"[sync_ui_version] ui/package.json: {current} -> {backend_version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

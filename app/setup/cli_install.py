import os
import platform
import shutil
import stat
import sys
from pathlib import Path

from app.config.settings import BaseConfig
from app.utils.logger import logger


def install_cli_binary() -> Path | None:
    """Copy the bundled `opa` CLI into ~/.openpa/bin and put that dir on PATH.

    Idempotent: re-copies only when the source is newer than the destination.
    Never raises — failures are logged and server startup continues.
    """
    try:
        name = "opa.exe" if platform.system() == "Windows" else "opa"
        src = next((p for p in _candidate_sources(name) if p.is_file()), None)
        if src is None:
            logger.warning(
                f"CLI binary '{name}' not found in any candidate location; "
                f"`opa` will be unavailable inside Exec Shell"
            )
            return None

        bin_dir = Path(BaseConfig.OPENPA_WORKING_DIR) / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        dst = bin_dir / name

        if _needs_copy(src, dst):
            shutil.copy2(src, dst)
            if platform.system() != "Windows":
                dst.chmod(dst.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            logger.info(f"Installed CLI binary: {src} -> {dst}")
        else:
            logger.debug(f"CLI binary already up-to-date at {dst}")

        _ensure_on_path(bin_dir)
        return dst
    except Exception as e:  # noqa: BLE001
        logger.warning(f"CLI install failed (continuing startup): {e}")
        return None


def _candidate_sources(name: str) -> list[Path]:
    return [
        Path(__file__).resolve().parents[2] / "cli" / name,
        Path.cwd() / "cli" / name,
        Path(sys.prefix) / "share" / "openpa" / "cli" / name,
    ]


def _needs_copy(src: Path, dst: Path) -> bool:
    return (not dst.exists()) or src.stat().st_mtime > dst.stat().st_mtime


def _ensure_on_path(bin_dir: Path) -> None:
    bin_str = str(bin_dir)
    current = os.environ.get("PATH", "")
    if bin_str in current.split(os.pathsep):
        return
    os.environ["PATH"] = bin_str + os.pathsep + current
    logger.info(f"Prepended {bin_str} to PATH for child processes")

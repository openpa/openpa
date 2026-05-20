import asyncio
import inspect
import json
import os
import sys
from typing import Any, Dict

from loguru import logger

# Project root for relative paths
PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(__file__))
)  # Adjust based on your structure (app/utils/logger.py -> project root)


def get_caller_info(stack_index: int = 2) -> str:
    """Get caller file:line (relative path). stack_index=2 skips cloud and this func."""
    stack = inspect.stack()
    if len(stack) <= stack_index:
        return "unknown:0"
    frame = stack[stack_index]
    filename = frame.filename
    relative_path = os.path.relpath(filename, PROJECT_ROOT).replace("\\", "/")
    line = frame.lineno
    return f"{relative_path}:{line}"


# Remove default sink and add configured ones
logger.remove()
# Stdout with pretty format (includes file:function:line by default)
logger.add(
    sys.stderr,
    level="DEBUG",
    format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
)

# File logger
log_file_path = os.path.join(PROJECT_ROOT, "logs", "app.log")
logger.add(
    log_file_path,
    level="DEBUG",
    format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} - {message}",
    rotation="1 MB",
    retention="10 days",
    enqueue=True,
)


# In-memory bus sink — feeds the Developer page's live log viewer.
# Imported lazily to avoid a hard dependency cycle if the bus module
# ever wants to log something itself (the loopback guard below blocks
# records from this module and from the bus).
_LOOPBACK_PREFIXES = ("app.events.log_stream_bus", "app.api.logs_stream")
_TRACEBACK_CAP = 8 * 1024


def _bus_sink(message: Any) -> None:
    """Publish a Loguru record to the log stream bus.

    Loguru passes a ``Message`` whose ``.record`` is the structured
    dict. We extract a small JSON-safe shape and drop on any failure —
    a sink that raises breaks every ``logger.X`` call site.
    """
    try:
        record = message.record
        name = record.get("name") or ""
        if name.startswith(_LOOPBACK_PREFIXES):
            return
        time_obj = record.get("time")
        ts = time_obj.isoformat() if time_obj is not None else ""
        level_obj = record.get("level")
        level_name = getattr(level_obj, "name", str(level_obj or "INFO"))
        line = record.get("line") or 0
        function = record.get("function") or ""
        source = f"{name}:{function}:{line}" if name else function
        text = record.get("message") or ""
        if record.get("exception") is not None:
            formatted = str(message)
            if len(formatted) > _TRACEBACK_CAP:
                formatted = formatted[:_TRACEBACK_CAP] + "...[truncated]"
            text = formatted
        entry: Dict[str, Any] = {
            "ts": ts,
            "level": level_name,
            "source": source,
            "message": text,
        }
        from app.events.log_stream_bus import get_log_stream_bus
        get_log_stream_bus().publish(entry)
    except Exception:  # noqa: BLE001
        # Sinks must never raise.
        pass


logger.add(_bus_sink, level="DEBUG")


# Export
__all__ = ["logger"]

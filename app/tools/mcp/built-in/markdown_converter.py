"""Markdown Converter MCP server using stdio transport.

A standalone FastMCP server that converts documents (PDF, DOCX, XLSX, PPTX,
HTML, CSV, etc.) to Markdown using the markitdown library. Output files are
written to OPENPA_WORKING_DIR/<profile>/markdown_files/ with timestamped names.

Usage:
    python app/tools/mcp/built-in/markdown_converter.py

Environment:
    OPENPA_WORKING_DIR - Base directory for file operations (default: ~/.openpa)
"""

import os
import platform
import re
import sys
from datetime import datetime, timezone
from typing import Any, Dict

from fastmcp import FastMCP
from fastmcp.tools.tool import Tool, ToolResult

from app.types import ToolResultFile, ToolResultWithFiles
from app.utils.logger import logger

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DATA_DIR = os.environ.get(
    "OPENPA_WORKING_DIR",
    os.path.join(os.path.expanduser("~"), ".openpa"),
)

MAX_MARKITDOWN_FILE_SIZE = 50 * 1024 * 1024  # 50 MB

# ---------------------------------------------------------------------------
# Lazy markitdown converter
# ---------------------------------------------------------------------------

_markitdown = None


def _get_markitdown():
    """Return a MarkItDown instance, importing lazily to keep startup fast."""
    global _markitdown
    if _markitdown is None:
        try:
            from markitdown import MarkItDown
            _markitdown = MarkItDown()
        except ImportError:
            logger.warning("markitdown is not installed; conversion disabled")
            _markitdown = False  # sentinel: tried and failed
    return _markitdown if _markitdown is not False else None


# ---------------------------------------------------------------------------
# Security helpers
# ---------------------------------------------------------------------------

def _safe_resolve(relative_path: str) -> str:
    """Resolve *relative_path* inside DATA_DIR.  Raises ValueError on traversal."""
    base = os.path.realpath(DATA_DIR)
    os.makedirs(base, exist_ok=True)
    # Strip leading slashes so "/" or "/foo" are treated as relative to DATA_DIR
    relative_path = relative_path.lstrip("/\\")
    target = os.path.realpath(os.path.join(base, relative_path))
    if target != base and not target.startswith(base + os.sep):
        raise ValueError(f"Path traversal detected: {relative_path}")
    return target


def _validate_profile(profile: str) -> None:
    """Reject profile names that could escape DATA_DIR."""
    if not profile or not re.match(r'^[\w\-. ]+$', profile):
        raise ValueError(f"Invalid profile name: {profile!r}")


def _format_size(size: int) -> str:
    """Human-readable file size."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024:
            return f"{size:.1f} {unit}" if unit != "B" else f"{size} {unit}"
        size /= 1024
    return f"{size:.1f} PB"


# ---------------------------------------------------------------------------
# FastMCP server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    name="Markdown Converter",
    instructions=(
        "Converts files (PDF, DOCX, XLSX, PPTX, HTML, CSV, etc.) to Markdown "
        "format. Use this tool when the user wants to convert a document to "
        "markdown for further processing, editing, or analysis."
    ),
)


# ---------------------------------------------------------------------------
# Tool: convert_to_markdown
# ---------------------------------------------------------------------------

class ConvertToMarkdownTool(Tool):
    name: str = "convert_to_markdown"
    description: str = (
        "Convert a file (PDF, DOCX, XLSX, PPTX, HTML, CSV, JSON, XML, etc.) "
        "to Markdown format. The converted file is saved with a timestamped "
        "filename. If an output path is specified, the file is saved there; "
        "otherwise it defaults to <profile>/markdown_files/. "
        "E.g. 'Convert openclaw.pdf to markdown'"
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "source_path": {
                "type": "string",
                "description": (
                    "Relative path to the source file within the data directory. "
                    "E.g. 'documents/report.pdf' or 'default/uploads/openclaw.pdf'."
                ),
            },
            "profile": {
                "type": "string",
                "description": (
                    "Profile name. The output file will be saved under "
                    "<profile>/markdown_files/."
                ),
            },
            "output_path": {
                "type": "string",
                "description": (
                    "Optional relative path (within the data directory) where "
                    "the converted file should be saved. Can be a directory "
                    "path or a full file path ending in .md. "
                    "If omitted, defaults to <profile>/markdown_files/. "
                    "E.g. 'myprofile/docs' or 'myprofile/docs/report.md'."
                ),
            },
        },
        "required": ["source_path", "profile"],
        "additionalProperties": False,
    }

    async def run(self, arguments: Dict[str, Any]) -> ToolResult:
        source_path = arguments.get("source_path", "").strip()
        profile = arguments.get("profile", "").strip()
        output_path = arguments.get("output_path", "").strip()

        # -- Validate required params --
        if not source_path:
            return ToolResult(structured_content={
                "error": "Missing parameter",
                "message": "source_path is required.",
            })
        if not profile:
            return ToolResult(structured_content={
                "error": "Missing parameter",
                "message": "profile is required.",
            })

        # -- Validate profile --
        try:
            _validate_profile(profile)
        except ValueError as e:
            return ToolResult(structured_content={
                "error": "Access denied",
                "message": str(e),
            })

        # -- Resolve and validate source --
        try:
            source_abs = _safe_resolve(source_path)
        except ValueError as e:
            return ToolResult(structured_content={
                "error": "Access denied",
                "message": str(e),
            })

        if not os.path.isfile(source_abs):
            return ToolResult(structured_content={
                "error": "Not found",
                "message": f"'{source_path}' is not a file or does not exist.",
            })

        file_size = os.path.getsize(source_abs)
        if file_size > MAX_MARKITDOWN_FILE_SIZE:
            return ToolResult(structured_content={
                "error": "File too large",
                "message": (
                    f"File exceeds {_format_size(MAX_MARKITDOWN_FILE_SIZE)} limit "
                    f"(actual: {_format_size(file_size)})."
                ),
            })

        # -- Get converter --
        converter = _get_markitdown()
        if converter is None:
            return ToolResult(structured_content={
                "error": "Dependency missing",
                "message": "markitdown library is not available.",
            })

        # -- Convert --
        source_name = os.path.basename(source_abs)
        try:
            result = converter.convert(source_abs)
            md_content = result.text_content if result and result.text_content else None
        except Exception as e:
            logger.error(f"[ConvertToMarkdownTool] conversion failed for {source_name}: {e}")
            return ToolResult(structured_content={
                "error": "Conversion failed",
                "message": f"markitdown could not convert '{source_name}': {e}",
            })

        if not md_content:
            return ToolResult(structured_content={
                "error": "Conversion failed",
                "message": f"Conversion produced no content for '{source_name}'.",
            })

        # -- Build output path --
        base = os.path.realpath(DATA_DIR)
        source_stem = os.path.splitext(source_name)[0]

        if output_path:
            # User specified an output path — resolve it within DATA_DIR
            try:
                resolved_output = _safe_resolve(output_path)
            except ValueError as e:
                return ToolResult(structured_content={
                    "error": "Access denied",
                    "message": str(e),
                })

            if output_path.endswith(".md"):
                # Treat as a full file path
                output_dir = os.path.dirname(resolved_output)
                out_name = os.path.basename(resolved_output)
            else:
                # Treat as a directory path — derive name from source
                output_dir = resolved_output
                out_name = f"{source_stem}.md"
        else:
            # Default: <profile>/markdown_files/
            output_dir = os.path.join(DATA_DIR, profile, "markdown_files")
            # Verify output dir stays under DATA_DIR
            if not os.path.realpath(output_dir).startswith(base + os.sep):
                return ToolResult(structured_content={
                    "error": "Access denied",
                    "message": "Invalid profile name.",
                })
            out_name = f"{source_stem}.md"

        os.makedirs(output_dir, exist_ok=True)
        out_path = os.path.join(output_dir, out_name)

        # If file already exists, append a timestamp to avoid overwriting
        if os.path.exists(out_path):
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            stem = os.path.splitext(out_name)[0]
            out_name = f"{stem}_{timestamp}.md"
            out_path = os.path.join(output_dir, out_name)

        # -- Write output --
        try:
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(md_content)
        except OSError as e:
            logger.error(f"[ConvertToMarkdownTool] write failed: {e}")
            return ToolResult(structured_content={
                "error": "Write error",
                "message": f"Failed to write output file: {e}",
            })

        # -- Build result --
        rel_output = os.path.relpath(out_path, base).replace(os.sep, "/")
        file_uri = out_path.replace(os.sep, "/")

        file_entry: ToolResultFile = {
            "uri": file_uri,
            "name": out_name,
            "mime_type": "text/markdown",
        }

        payload: ToolResultWithFiles = {
            "text": (
                f"Converted '{source_name}' to markdown.\n"
                f"Output: {rel_output} ({len(md_content)} characters)"
            ),
            "_files": [file_entry],
        }

        logger.info(
            f"[ConvertToMarkdownTool] converted {source_name} -> {rel_output} "
            f"({len(md_content)} chars)"
        )

        return ToolResult(structured_content=payload)


# ---------------------------------------------------------------------------
# Register tools & run
# ---------------------------------------------------------------------------

mcp.add_tool(ConvertToMarkdownTool())

if __name__ == "__main__":
    sys.stderr.write(
        f"Starting Markdown Converter MCP Server "
        f"(data_dir={DATA_DIR}, os={platform.system()})\n"
    )
    mcp.run(transport="stdio")

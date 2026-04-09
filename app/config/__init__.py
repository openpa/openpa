"""Configuration management using Dynaconf + TOML with SQLite dynamic overrides.

Priority chain: SQLite dynamic config > TOML file defaults > environment variable fallback.
"""

import os
from pathlib import Path

import toml
from dynaconf import Dynaconf

# Project root is two levels up from this file (app/config/__init__.py -> openpa/)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_CONFIG_DIR = _PROJECT_ROOT / "config"

# Initialize Dynaconf for TOML-based configuration
settings = Dynaconf(
    settings_files=[str(_CONFIG_DIR / "settings.toml")],
    envvar_prefix="OPENPA",
    environments=False,
    load_dotenv=True,
)


def load_provider_catalog(provider_name: str) -> dict:
    """Load a provider's model catalog from its TOML config file.

    Returns the parsed TOML dict, or empty dict if file not found.
    """
    toml_path = _CONFIG_DIR / "providers" / f"{provider_name}.toml"
    if not toml_path.exists():
        return {}
    with open(toml_path, "r") as f:
        return toml.load(f)


def load_all_provider_catalogs() -> dict[str, dict]:
    """Load all provider catalogs from config/providers/*.toml.

    Returns dict keyed by provider name.
    """
    providers_dir = _CONFIG_DIR / "providers"
    catalogs = {}
    if not providers_dir.exists():
        return catalogs
    for toml_file in providers_dir.glob("*.toml"):
        data = toml.load(toml_file)
        provider_info = data.get("provider", {})
        name = provider_info.get("name", toml_file.stem)
        catalogs[name] = data
    return catalogs


def load_tool_schema(tool_name: str) -> dict:
    """Load a tool's config schema from its TOML config file.

    Returns the parsed TOML dict, or empty dict if file not found.
    """
    toml_path = _CONFIG_DIR / "tools" / f"{tool_name}.toml"
    if not toml_path.exists():
        return {}
    with open(toml_path, "r") as f:
        return toml.load(f)


def load_all_tool_schemas() -> dict[str, dict]:
    """Load all tool schemas from config/tools/*.toml.

    Returns dict keyed by tool name.
    """
    tools_dir = _CONFIG_DIR / "tools"
    schemas = {}
    if not tools_dir.exists():
        return schemas
    for toml_file in tools_dir.glob("*.toml"):
        data = toml.load(toml_file)
        tool_info = data.get("tool", {})
        name = tool_info.get("name", toml_file.stem)
        schemas[name] = data
    return schemas

"""Configuration management using Dynaconf + TOML with SQLite dynamic overrides.

Priority chain: SQLite dynamic config > TOML file defaults > environment variable fallback.
"""

from pathlib import Path

import toml
from dynaconf import Dynaconf

# Config directory is the same directory as this file (app/config/)
_CONFIG_DIR = Path(__file__).resolve().parent

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
    """Load all provider catalogs from app/config/providers/*.toml.

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



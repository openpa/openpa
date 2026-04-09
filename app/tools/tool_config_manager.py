"""Tool Configuration Manager.

Manages per-tool configuration including required secrets, enabled state,
and model overrides. Loads tool schemas from TOML files and checks SQLite
for user-provided configuration values.
"""

from app.config import load_all_tool_schemas, load_tool_schema
from app.storage.dynamic_config_storage import DynamicConfigStorage
from app.utils.logger import logger


class ToolConfigManager:
    """Manages per-tool configuration."""

    def __init__(self, config_storage: DynamicConfigStorage, tool_schemas: dict[str, dict] | None = None):
        self.config_storage = config_storage
        self.tool_schemas = tool_schemas or load_all_tool_schemas()

    def is_tool_configured(self, tool_name: str, profile: str | None = None) -> bool:
        """Check if all required_config keys for a tool have values in the DB.

        Returns True if the tool has no required config or all required keys are set.
        """
        schema = self.tool_schemas.get(tool_name, {})
        required_config = schema.get("tool", {}).get("required_config", {})

        if not required_config:
            return True

        _kw = {"profile": profile} if profile is not None else {}
        for key in required_config:
            value = self.config_storage.get_tool_config(tool_name, key, **_kw)
            if not value:
                return False
        return True

    def is_tool_enabled(self, tool_name: str, profile: str | None = None) -> bool:
        """Check if a tool is explicitly enabled/disabled in config.

        Returns True by default. If the tool has required config that isn't set,
        it should be considered disabled regardless of this setting.
        """
        _kw = {"profile": profile} if profile is not None else {}
        enabled = self.config_storage.get_tool_config(tool_name, "_enabled", **_kw)
        if enabled is not None:
            return enabled.lower() == "true"
        # Default: enabled if configured (or no config needed)
        return self.is_tool_configured(tool_name, profile=profile)

    def set_tool_enabled(self, tool_name: str, enabled: bool, profile: str | None = None):
        """Explicitly enable or disable a tool."""
        _kw = {"profile": profile} if profile is not None else {}
        self.config_storage.set_tool_config(tool_name, "_enabled", str(enabled).lower(), **_kw)

    def get_tool_env(self, tool_name: str, profile: str | None = None) -> dict[str, str]:
        """Get environment variables to pass to the tool's subprocess.

        Returns a dict of config keys to values for all configured (non-internal) keys.
        """
        _kw = {"profile": profile} if profile is not None else {}
        all_configs = self.config_storage.get_all_tool_configs(tool_name, include_secrets=True, **_kw)
        # Filter out internal keys (prefixed with '_')
        return {k: v for k, v in all_configs.items() if not k.startswith("_")}

    def get_tool_oauth_config(self, tool_name: str, profile: str | None = None) -> dict | None:
        """Get OAuth configuration for a tool from its TOML schema.

        Returns the oauth dict if present, with client_id/secret populated from DB.
        """
        schema = self.tool_schemas.get(tool_name, {})
        oauth = schema.get("tool", {}).get("oauth")
        if not oauth:
            return None

        _kw = {"profile": profile} if profile is not None else {}
        # Populate client credentials from tool config DB
        oauth_config = dict(oauth)
        client_id = self.config_storage.get_tool_config(tool_name, "GOOGLE_CLIENT_ID", **_kw)
        client_secret = self.config_storage.get_tool_config(tool_name, "GOOGLE_CLIENT_SECRET", **_kw)
        if client_id:
            oauth_config["client_id"] = client_id
        if client_secret:
            oauth_config["client_secret"] = client_secret

        return oauth_config

    def get_tool_status(self, tool_name: str, profile: str | None = None) -> dict:
        """Return config status for UI display."""
        schema = self.tool_schemas.get(tool_name, {})
        tool_info = schema.get("tool", {})
        required_config = tool_info.get("required_config", {})

        _kw = {"profile": profile} if profile is not None else {}
        # Get current values (masked for secrets)
        current_values = self.config_storage.get_all_tool_configs(tool_name, include_secrets=False, **_kw)

        # Build field status
        fields = {}
        for key, field_spec in required_config.items():
            fields[key] = {
                "description": field_spec.get("description", ""),
                "type": field_spec.get("type", "string"),
                "secret": field_spec.get("secret", False),
                "configured": key in current_values and current_values[key] != "***" or
                              self.config_storage.get_tool_config(tool_name, key, **_kw) is not None,
            }

        return {
            "name": tool_name,
            "display_name": tool_info.get("display_name", tool_name),
            "default_model_group": tool_info.get("default_model_group", "low"),
            "configured": self.is_tool_configured(tool_name, profile=profile),
            "enabled": self.is_tool_enabled(tool_name, profile=profile),
            "has_oauth": "oauth" in tool_info,
            "required_fields": fields,
            "current_values": current_values,
        }

    def get_all_tools_status(self, profile: str | None = None) -> list[dict]:
        """Return status for all known tools."""
        return [self.get_tool_status(name, profile=profile) for name in self.tool_schemas]

    def get_tool_config_schema(self, tool_name: str) -> dict:
        """Get the full TOML schema for a tool."""
        return self.tool_schemas.get(tool_name, {})

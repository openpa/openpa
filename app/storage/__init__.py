from app.config.settings import BaseConfig
from app.storage.autostart_storage import AutostartStorage
from app.storage.conversation_storage import ConversationStorage
from app.storage.dynamic_config_storage import DynamicConfigStorage
from app.storage.tool_storage import ToolStorage, get_tool_storage

_instance: ConversationStorage | None = None
_dynamic_config_instance: DynamicConfigStorage | None = None
_autostart_instance: AutostartStorage | None = None


def get_conversation_storage(db_path: str | None = None) -> ConversationStorage:
    global _instance
    if _instance is None:
        _instance = ConversationStorage(db_path or BaseConfig.SQLITE_DB_PATH)
    return _instance


def get_dynamic_config_storage(db_path: str | None = None) -> DynamicConfigStorage:
    global _dynamic_config_instance
    if _dynamic_config_instance is None:
        _dynamic_config_instance = DynamicConfigStorage(db_path or BaseConfig.SQLITE_DB_PATH)
    return _dynamic_config_instance


def get_autostart_storage(db_path: str | None = None) -> AutostartStorage:
    global _autostart_instance
    if _autostart_instance is None:
        _autostart_instance = AutostartStorage(db_path or BaseConfig.SQLITE_DB_PATH)
    return _autostart_instance


__all__ = [
    "AutostartStorage",
    "ConversationStorage",
    "DynamicConfigStorage",
    "ToolStorage",
    "get_autostart_storage",
    "get_conversation_storage",
    "get_dynamic_config_storage",
    "get_tool_storage",
]

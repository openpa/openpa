from app.config.settings import BaseConfig
from app.storage.autostart_storage import AutostartStorage
from app.storage.conversation_storage import ConversationStorage
from app.storage.dynamic_config_storage import DynamicConfigStorage
from app.storage.event_subscription_storage import EventSubscriptionStorage
from app.storage.file_watcher_storage import FileWatcherSubscriptionStorage
from app.storage.tool_storage import ToolStorage, get_tool_storage

_instance: ConversationStorage | None = None
_dynamic_config_instance: DynamicConfigStorage | None = None
_autostart_instance: AutostartStorage | None = None
_event_subscription_instance: EventSubscriptionStorage | None = None
_file_watcher_instance: FileWatcherSubscriptionStorage | None = None


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


def get_event_subscription_storage(db_path: str | None = None) -> EventSubscriptionStorage:
    global _event_subscription_instance
    if _event_subscription_instance is None:
        _event_subscription_instance = EventSubscriptionStorage(
            db_path or BaseConfig.SQLITE_DB_PATH
        )
    return _event_subscription_instance


def get_file_watcher_storage(db_path: str | None = None) -> FileWatcherSubscriptionStorage:
    global _file_watcher_instance
    if _file_watcher_instance is None:
        _file_watcher_instance = FileWatcherSubscriptionStorage(
            db_path or BaseConfig.SQLITE_DB_PATH
        )
    return _file_watcher_instance


__all__ = [
    "AutostartStorage",
    "ConversationStorage",
    "DynamicConfigStorage",
    "EventSubscriptionStorage",
    "FileWatcherSubscriptionStorage",
    "ToolStorage",
    "get_autostart_storage",
    "get_conversation_storage",
    "get_dynamic_config_storage",
    "get_event_subscription_storage",
    "get_file_watcher_storage",
    "get_tool_storage",
]

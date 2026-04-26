"""Event-driven skill execution package.

The :class:`EventManager` watches ``<skill_dir>/events/<event_type>/`` folders
for new ``*.md`` files; each file is treated as one event payload. For every
:class:`SkillEventSubscription` matching that (skill, event_type) pair, the
file content is enqueued to a per-conversation worker that re-runs the
reasoning agent with ``history_messages=[]`` and persists the result.
"""

from app.events.manager import EventManager, get_event_manager
from app.events.notifications_buffer import EventNotificationsBuffer, get_event_notifications
from app.events.notifications_bus import NotificationsStreamBus, get_notifications_stream_bus
from app.events.stream_bus import ConversationStreamBus, get_event_stream_bus

__all__ = [
    "ConversationStreamBus",
    "EventManager",
    "EventNotificationsBuffer",
    "NotificationsStreamBus",
    "get_event_manager",
    "get_event_notifications",
    "get_event_stream_bus",
    "get_notifications_stream_bus",
]

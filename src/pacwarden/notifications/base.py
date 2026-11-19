from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, Protocol

from ..config import NotificationsConfig
from ..reporting import warning


class EventType(StrEnum):
    SUCCESS_NO_UPDATES = "success_no_updates"
    SUCCESS_WITH_UPDATES = "success_with_updates"
    UPDATE_FAILED = "update_failed"
    REBOOT_REQUIRED = "reboot_required"
    REBOOTING = "rebooting"
    SCRIPT_CRASHED = "script_crashed"
    SERVICE_RESTARTED = "service_restarted"
    SERVICE_RESTART_FAILED = "service_restart_failed"


@dataclass(frozen=True)
class NotificationEvent:
    event_type: EventType
    title: str
    message: str
    priority: Literal["low", "default", "high", "urgent"] = "default"
    tags: tuple[str, ...] = ()


class NotificationProvider(Protocol):
    def send(self, event: NotificationEvent) -> None: ...


@dataclass(frozen=True)
class Notifier:
    provider: NotificationProvider
    settings: NotificationsConfig

    def send(self, event: NotificationEvent) -> None:
        if self.settings.enabled and getattr(self.settings, event.event_type.value):
            try:
                self.provider.send(event)
            except Exception:
                message = "crash notification" if event.event_type is EventType.SCRIPT_CRASHED else "notification"
                warning(f"Failed to send {message}")

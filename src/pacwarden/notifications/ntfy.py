import http.client
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from ..reporting import warning
from .base import NotificationEvent


@dataclass(frozen=True)
class NtfyProvider:
    url: str | None = field(repr=False)
    timeout_seconds: int

    def send(self, event: NotificationEvent) -> None:
        if not self.url or "YOUR_TOPIC_HERE" in self.url:
            warning("ntfy notification not sent: set notifications.ntfy.url_env or notifications.ntfy.url")
            return

        headers = {
            "Title": event.title,
            "Priority": event.priority,
            "Content-Type": "text/plain; charset=utf-8",
        }
        if event.tags:
            headers["Tags"] = ",".join(event.tags)

        try:
            request = urllib.request.Request(
                self.url, data=event.message.encode("utf-8"), headers=headers, method="POST"
            )
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                response.read()
        except urllib.error.HTTPError as exc:
            warning(f"Failed to send ntfy notification: HTTP status {exc.code}")
        except (OSError, ValueError, http.client.HTTPException):
            warning("Failed to send ntfy notification")

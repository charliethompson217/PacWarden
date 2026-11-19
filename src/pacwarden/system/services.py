from dataclasses import dataclass

from ..execution.base import CommandError, Executor
from ..models import UpdateResult
from ..reporting import error, log


@dataclass(frozen=True)
class ServiceResult:
    success: bool
    message: str


@dataclass(frozen=True)
class NginxService:
    executor: Executor
    restart_after_host_updates: bool

    def restart_if_needed(self, target: UpdateResult) -> ServiceResult | None:
        if not self.restart_after_host_updates or not target.success or not target.packages_updated:
            return None

        is_active = ["systemctl", "is-active", "--quiet", "nginx"]
        try:
            status = self.executor.run(is_active, check=False).returncode
            if status in (3, 4):  # systemctl reports an inactive or missing unit
                log("NGINX isn't active; skipping its restart")
                return None
            if status != 0:
                raise CommandError(f"systemctl is-active exited with status {status}", returncode=status)
            self.executor.run(["nginx", "-t"])
            self.executor.run(["systemctl", "restart", "nginx"])
            self.executor.run(is_active)
        except CommandError as exc:
            message = f"NGINX validation/restart failed: {exc}"
            error(message)
            return ServiceResult(False, message)

        message = f"NGINX restarted after {target.name} updates"
        log(message)
        return ServiceResult(True, message)

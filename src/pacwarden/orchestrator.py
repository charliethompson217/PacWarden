from collections.abc import Callable
from dataclasses import dataclass, replace

from .models import PackageChanges, UpdateResult, UpdateTarget
from .notifications.base import EventType, NotificationEvent, Notifier
from .reporting import describe_error, error, log, warning
from .system.reboot import HostReboot, RebootAction, RebootStatus, report_reboot
from .system.services import NginxService, ServiceResult


@dataclass(frozen=True)
class RunResult:
    host: UpdateResult
    containers: tuple[UpdateResult, ...]
    reboot: RebootStatus
    reboot_action: RebootAction

    @property
    def success(self) -> bool:
        return self.reboot.complete and self.host.success and all(result.success for result in self.containers)

    @property
    def total_updated(self) -> int:
        return sum(result.packages_updated or 0 for result in (self.host, *self.containers))

    def summary(self) -> str:
        lines: list[str] = []
        targets = (self.host, *self.containers)
        for result in targets:
            status = "OK" if result.success else "FAILED"
            if result.success:
                detail = f"{result.packages_updated} packages updated"
            elif result.packages_updated is None:
                detail = f"({result.message})"
            else:
                detail = f"{result.packages_updated} package updates observed ({result.message})"
            lines.append(f"{result.name:<16} {status:<7} {detail}")
        count_note = " (known total)" if any(result.packages_updated is None for result in targets) else ""
        required = self.reboot.required
        reboot_required = "unknown" if required is None else "yes" if required else "no"
        if required and not self.reboot.complete:
            reboot_required += " (assessment incomplete)"
        lines.extend([
            "",
            f"Total packages updated: {self.total_updated}{count_note}",
            "",
            f"Reboot required: {reboot_required}",
            f"Reboot action:   {self.reboot_action}",
        ])
        return "\n".join(lines)


def update_target(
    target: UpdateTarget,
    *,
    is_host: bool = False,
    before_upgrade: Callable[[dict[str, str]], None] | None = None,
) -> tuple[UpdateResult, PackageChanges | None]:
    label = "host" if is_host else f"container {target.name!r}"
    try:
        if target.prepare is not None:
            target.prepare()
        log(f"Recording installed package versions on {label}")
        before = target.package_manager.installed_versions(target.executor)
        if before_upgrade is not None:
            before_upgrade(before)
    except Exception as exc:
        message = describe_error(exc)
        error(f"Failed to prepare update on {label}: {message}")
        return UpdateResult(target.name, False, message=message), None

    failure: Exception | KeyboardInterrupt | None = None
    log(f"Updating {label}")
    try:
        target.package_manager.upgrade(target.executor)
    except (Exception, KeyboardInterrupt) as exc:
        failure = exc

    try:
        after = target.package_manager.installed_versions(target.executor)
    except (Exception, KeyboardInterrupt) as exc:
        message = f"Package changes unknown (post-update snapshot failed: {describe_error(exc)})"
        if failure is not None:
            message = f"{describe_error(failure)}. {message}"
        error(f"Failed to update {label}: {message}")
        interrupted = isinstance(failure, KeyboardInterrupt) or isinstance(exc, KeyboardInterrupt)
        return UpdateResult(target.name, False, None, message, interrupted), None

    changes = PackageChanges(before, after)
    if failure is not None:
        message = describe_error(failure)
        error(f"Failed to update {label}: {message}")
        return UpdateResult(
            target.name, False, changes.updated_count, message, isinstance(failure, KeyboardInterrupt),
        ), changes
    return UpdateResult(target.name, True, changes.updated_count), changes


@dataclass(frozen=True)
class Orchestrator:
    host: UpdateTarget
    containers: tuple[UpdateTarget, ...]
    nginx: NginxService
    reboot: HostReboot
    notifier: Notifier
    hostname: str

    def run(self, *, no_reboot: bool = False) -> RunResult:
        recovered_reasons = False

        def record_baseline(versions: dict[str, str]) -> None:
            nonlocal recovered_reasons
            recovered_reasons = self.reboot.record_baseline(versions)

        with self.reboot.store.locked():
            host, changes = update_target(self.host, is_host=True, before_upgrade=record_baseline)
            pending = self.reboot.evaluate(
                changes.after if changes is not None else None,
                recovered_reasons=recovered_reasons,
                upgrade_failed=not host.success,
            )
            if host.interrupted:
                report_reboot(pending, self.reboot.action(pending, no_reboot=no_reboot, host_failed=True))
                raise KeyboardInterrupt
            return self._finish(host, pending, no_reboot=no_reboot)

    def _finish(self, host: UpdateResult, pending: RebootStatus, *, no_reboot: bool) -> RunResult:
        service = self.nginx.restart_if_needed(host)
        if service is not None:
            self._notify_service(service)
            if not service.success:
                host = replace(host, success=False, message=service.message)

        log(f"Updating {len(self.containers)} configured Incus container(s)")
        containers: list[UpdateResult] = []
        for target in self.containers:
            container, _ = update_target(target)
            if container.interrupted:
                raise KeyboardInterrupt
            containers.append(container)

        action = self.reboot.action(pending, no_reboot=no_reboot, host_failed=not host.success)
        result = RunResult(host, tuple(containers), pending, action)
        summary = result.summary()
        print("\n\033[1mUpdate summary\033[0m\n--------------")
        for line in summary.splitlines():
            print(f"  {line}" if line else "")
        print(flush=True)
        self._notify_run(result, summary)

        if pending.new_reasons and action is not RebootAction.REBOOTING:
            self.notifier.send(NotificationEvent(
                EventType.REBOOT_REQUIRED,
                f"{self.hostname}: reboot required",
                f"{self.hostname} requires a manual reboot.\n\nReboot action: {action}",
                priority="high", tags=("warning",),
            ))

        report_reboot(pending, action)
        if action is RebootAction.REBOOTING:
            self.notifier.send(NotificationEvent(
                EventType.REBOOTING,
                f"{self.hostname}: rebooting after update",
                f"The host update completed on {self.hostname}. A reboot is required and is starting now.",
                tags=("arrows_counterclockwise",),
            ))
            self.reboot.reboot()
        return result

    def _notify_service(self, service: ServiceResult) -> None:
        if service.success:
            event = NotificationEvent(
                EventType.SERVICE_RESTARTED,
                f"{self.hostname}: NGINX restarted",
                f"{self.hostname}: {service.message}.",
                priority="low",
            )
        else:
            event = NotificationEvent(
                EventType.SERVICE_RESTART_FAILED,
                f"{self.hostname}: NGINX restart failed",
                service.message,
                priority="high", tags=("warning",),
            )
        self.notifier.send(event)

    def _notify_run(self, result: RunResult, summary: str) -> None:
        if result.reboot.check_errors:
            details = "\n".join(result.reboot.check_errors)
            self.notifier.send(NotificationEvent(
                EventType.SCRIPT_CRASHED,
                f"{self.hostname}: reboot assessment failed",
                f"Could not complete the reboot assessment on {self.hostname}.\n\n{details}",
                priority="urgent", tags=("rotating_light",),
            ))
            if result.host.success and all(container.success for container in result.containers):
                return
        if result.success:
            event_type = EventType.SUCCESS_WITH_UPDATES if result.total_updated else EventType.SUCCESS_NO_UPDATES
            event = NotificationEvent(
                event_type,
                f"{self.hostname}: update successful",
                f"Updates completed successfully on {self.hostname}.\n\n{summary}",
                priority="low", tags=("white_check_mark",),
            )
        else:
            warning("One or more updates failed")
            event = NotificationEvent(
                EventType.UPDATE_FAILED,
                f"{self.hostname}: update failed",
                f"One or more updates failed on {self.hostname}.\n\n{summary}\n\n"
                "Check: journalctl -u pacwarden.service -u update-server.service",
                priority="high", tags=("warning",),
            )
        self.notifier.send(event)

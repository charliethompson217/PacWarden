from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from ..execution.base import Executor
from ..reporting import DiagnosticError, describe_error, log, warning
from ..state import RebootState, RebootStateStore


def get_boot_id() -> str:
    boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    if not boot_id:
        raise DiagnosticError("Empty kernel boot ID")
    return boot_id


class RebootAction(StrEnum):
    NOT_NEEDED = "not needed"
    CHECK_INCOMPLETE = "skipped because reboot assessment is incomplete"
    HOST_FAILED = "skipped because host update or service verification failed"
    SUPPRESSED = "skipped by --no-reboot"
    DISABLED = "skipped because reboot.auto_reboot is disabled"
    REBOOTING = "rebooting"


@dataclass(frozen=True)
class RebootStatus:
    reasons: tuple[str, ...]
    new_reasons: bool
    check_errors: tuple[str, ...] = ()
    pending_package_check: bool = False

    @property
    def complete(self) -> bool:
        return not self.check_errors and not self.pending_package_check

    @property
    def required(self) -> bool | None:
        if self.reasons:
            return True
        return False if self.complete else None


@dataclass(frozen=True)
class HostReboot:
    executor: Executor
    store: RebootStateStore
    reboot_packages: tuple[str, ...]
    kernel_versions: Callable[[], tuple[str, str]]
    auto_reboot: bool

    def record_baseline(self, versions: dict[str, str]) -> bool:
        """Reconcile any interrupted run and save the baseline before upgrading."""
        boot_id = get_boot_id()
        previous = self.store.read(boot_id)
        reasons = previous.reasons | _package_reasons(previous.pending_versions, versions)
        baseline = {package: versions.get(package) for package in self.reboot_packages}
        self.store.write(boot_id, RebootState(reasons, baseline))
        return bool(reasons - previous.reasons)

    def evaluate(
        self,
        versions: dict[str, str] | None,
        *,
        recovered_reasons: bool = False,
        upgrade_failed: bool = False,
    ) -> RebootStatus:
        try:
            boot_id = get_boot_id()
            previous = self.store.read(boot_id)
        except Exception as exc:
            return RebootStatus((), False, (f"Could not read reboot state: {describe_error(exc)}",))

        reasons = set(previous.reasons)
        baseline = previous.pending_versions
        if baseline is not None and versions is not None:
            reasons.update(_package_reasons(baseline, versions))
            # Some commands can leave work behind after an error or timeout. Compare
            # the latest observation with the next run's snapshot as well.
            baseline = {package: versions.get(package) for package in baseline} if upgrade_failed else None

        errors: list[str] = []
        try:
            running, installed = self.kernel_versions()
        except Exception as exc:
            errors.append(f"Could not compare kernel versions: {describe_error(exc)}")
        else:
            reasons = {reason for reason in reasons if not reason.startswith("kernel: ")}
            if running != installed:
                reasons.add(f"kernel: {running} -> {installed}")

        try:
            self.store.write(boot_id, RebootState(frozenset(reasons), baseline))
        except Exception as exc:
            errors.append(f"Could not save reboot state: {describe_error(exc)}")
        return RebootStatus(
            tuple(sorted(reasons)), recovered_reasons or bool(reasons - previous.reasons),
            tuple(errors), pending_package_check=baseline is not None,
        )

    def action(self, status: RebootStatus, *, no_reboot: bool, host_failed: bool) -> RebootAction:
        if status.reasons and host_failed and not status.check_errors:
            return RebootAction.HOST_FAILED
        if not status.complete:
            return RebootAction.CHECK_INCOMPLETE
        if not status.reasons:
            return RebootAction.NOT_NEEDED
        if no_reboot:
            return RebootAction.SUPPRESSED
        if not self.auto_reboot:
            return RebootAction.DISABLED
        return RebootAction.REBOOTING

    def reboot(self) -> None:
        log("Rebooting host")
        self.executor.run(["systemctl", "reboot"])


def _package_reasons(baseline: dict[str, str | None] | None, versions: dict[str, str]) -> set[str]:
    if baseline is None:
        return set()
    reasons = set()
    for package, before in baseline.items():
        after = versions.get(package)
        if before != after:
            reasons.add(f"{package}: {before or 'not installed'} -> {after or 'not installed'}")
    return reasons


def report_reboot(status: RebootStatus, action: RebootAction) -> None:
    if action is RebootAction.NOT_NEEDED:
        log("No host reboot required")
        return

    if status.reasons:
        log("Pending host reboot reasons (local journal only):")
        for reason in status.reasons:
            print(f"  {reason}")
    if status.pending_package_check:
        warning("The host transaction needs another package check. The next run will use the saved baseline.")

    match action:
        case RebootAction.CHECK_INCOMPLETE:
            for message in status.check_errors:
                warning(message)
            warning("Reboot assessment is incomplete. Automatic reboot skipped.")
        case RebootAction.HOST_FAILED:
            warning("Host update or service verification failed. Automatic reboot skipped.")
        case RebootAction.SUPPRESSED:
            warning("A reboot is required, but --no-reboot was specified")
        case RebootAction.DISABLED:
            warning("A reboot is required, but reboot.auto_reboot is disabled")

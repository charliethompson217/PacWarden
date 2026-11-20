from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from subprocess import CompletedProcess
from typing import Protocol, runtime_checkable

from ..execution.base import CommandError, Executor
from ..reporting import DiagnosticError


class PackageManager(Protocol):
    def installed_versions(self, executor: Executor) -> dict[str, str]: ...

    def upgrade(self, executor: Executor) -> None: ...


class UpgradeStatus(StrEnum):
    READY = "ready"
    DEFERRED = "deferred"
    UNASSESSED = "unassessed"


@dataclass(frozen=True)
class PackageUpdate:
    name: str
    installed_version: str
    available_version: str
    status: UpgradeStatus = UpgradeStatus.UNASSESSED


@dataclass(frozen=True)
class AvailableUpdates:
    updates: tuple[PackageUpdate, ...]
    metadata_refreshed: bool

    def status_counts(self) -> Counter[UpgradeStatus]:
        return Counter(update.status for update in self.updates)


@runtime_checkable
class UpdateChecker(Protocol):
    def available_updates(self, executor: Executor, *, refresh: bool) -> AvailableUpdates: ...


def run_check_command(
    executor: Executor, command: Sequence[str], operation: str,
) -> CompletedProcess[str]:
    # Repository diagnostics can contain credentials. Capture them for local
    # classification and keep them out of check output.
    try:
        return executor.run(command, capture_output=True, check=False, sensitive=True)
    except CommandError as exc:
        raise DiagnosticError(f"{operation} could not run ({exc})") from None


def parse_installed_versions(output: str) -> dict[str, str]:
    versions: dict[str, str] = {}
    for line in output.splitlines():
        fields = line.split()
        if not fields:
            continue
        if len(fields) != 2 or fields[0] in versions:
            raise DiagnosticError("Unexpected installed package list format")
        name, version = fields
        versions[name] = version
    return versions

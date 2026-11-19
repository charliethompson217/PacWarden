from dataclasses import dataclass

from ..execution.base import Executor
from ..reporting import DiagnosticError, log
from .base import parse_installed_versions


_PRESENT_STATES = frozenset({
    "half-installed", "unpacked", "half-configured", "triggers-awaited", "triggers-pending", "installed",
})


@dataclass(frozen=True)
class AptPackageManager:
    autoremove: bool

    def installed_versions(self, executor: Executor) -> dict[str, str]:
        result = executor.run(
            ["dpkg-query", "-W", "-f=${db:Status-Status} ${binary:Package} ${Version}\\n"],
            capture_output=True,
        )
        packages = []
        for line in result.stdout.splitlines():
            status, _, package_version = line.partition(" ")
            if status in {"not-installed", "config-files"}:
                continue
            if status not in _PRESENT_STATES or not package_version.strip():
                raise DiagnosticError("Unexpected dpkg package status format")
            # A version can have been written to the database before configuration finishes.
            packages.append(package_version)
        return parse_installed_versions("\n".join(packages))

    def upgrade(self, executor: Executor) -> None:
        prefix = ["env", "DEBIAN_FRONTEND=noninteractive", "apt-get"]
        executor.run([*prefix, "update"])
        executor.run([*prefix, "dist-upgrade", "-y"])
        if self.autoremove:
            executor.run([*prefix, "autoremove", "-y"])
        else:
            log("Skipping apt autoremove")

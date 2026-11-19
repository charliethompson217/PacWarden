from typing import Protocol

from ..execution.base import Executor
from ..reporting import DiagnosticError


class PackageManager(Protocol):
    def installed_versions(self, executor: Executor) -> dict[str, str]: ...

    def upgrade(self, executor: Executor) -> None: ...


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

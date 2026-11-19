from ..execution.base import Executor
from ..reporting import DiagnosticError
from .base import parse_installed_versions


class PacmanPackageManager:
    def installed_versions(self, executor: Executor) -> dict[str, str]:
        result = executor.run(["pacman", "-Q"], capture_output=True)
        return parse_installed_versions(result.stdout)

    def upgrade(self, executor: Executor) -> None:
        executor.run(["pacman", "-Syu", "--noconfirm"])

    def installed_version(self, executor: Executor, package: str) -> str:
        result = executor.run(["pacman", "-Q", package], capture_output=True)
        fields = result.stdout.strip().split()
        if len(fields) != 2 or fields[0] != package:
            raise DiagnosticError(f"Unexpected pacman output for {package}")
        return fields[1]

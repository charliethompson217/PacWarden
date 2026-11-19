from collections.abc import Sequence
from dataclasses import dataclass
from subprocess import CompletedProcess

from ..reporting import DiagnosticError
from .base import Executor


_INACTIVE_STATUSES = frozenset({
    "STARTING", "STOPPING", "STOPPED", "ABORTING", "FREEZING", "FROZEN", "THAWED", "ERROR",
})


@dataclass(frozen=True)
class IncusExecutor:
    name: str
    local: Executor

    def run(
        self,
        command: Sequence[str],
        *,
        capture_output: bool = False,
        check: bool = True,
        sensitive: bool = False,
    ) -> CompletedProcess[str]:
        return self.local.run(
            ["incus", "exec", self.name, "--", *command],
            capture_output=capture_output,
            check=check,
            sensitive=sensitive,
        )


def ensure_container_running(executor: Executor, name: str) -> None:
    """Preflight for an Incus target, using the host executor for Incus itself."""
    result = executor.run(["incus", "info", name], capture_output=True, check=False)
    if result.returncode == 0:
        for line in result.stdout.splitlines():
            if line.startswith("Status:"):
                status = line.removeprefix("Status:").strip().upper()
                if status == "RUNNING":
                    return
                if status in _INACTIVE_STATUSES:
                    raise DiagnosticError(f"container status is {status!r}")
                raise DiagnosticError("container has an unrecognized status")
    detail = (result.stderr or "").lower()
    if any(phrase in detail for phrase in ("permission denied", "not authorized", "not trusted")):
        raise DiagnosticError("Permission denied inspecting the container. Check Incus access.")
    raise DiagnosticError("container does not exist or cannot be inspected")

from collections.abc import Sequence
from subprocess import CompletedProcess
from typing import Protocol

from ..reporting import DiagnosticError


class CommandError(DiagnosticError):
    """A command timed out, failed to launch, or exited unsuccessfully with check enabled."""

    def __init__(self, message: str, *, returncode: int | None = None) -> None:
        super().__init__(message)
        self.returncode = returncode


class Executor(Protocol):
    def run(
        self,
        command: Sequence[str],
        *,
        capture_output: bool = False,
        check: bool = True,
        sensitive: bool = False,
    ) -> CompletedProcess[str]:
        """Execute argv, raising CommandError on execution failure.

        With check disabled, a non-zero exit is returned in the result.
        Sensitive commands suppress argv in diagnostics and capture output.
        """
        ...

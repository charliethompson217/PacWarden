import os
import shlex
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass

from ..reporting import log
from .base import CommandError


@dataclass(frozen=True)
class LocalExecutor:
    timeout_seconds: int
    log_commands: bool = True

    def run(
        self,
        command: Sequence[str],
        *,
        capture_output: bool = False,
        check: bool = True,
        sensitive: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        display = "[sensitive command]" if sensitive else shlex.join(command)
        if self.log_commands:
            log(f"Running: {display}")
        returncode = None
        try:
            return subprocess.run(
                list(command),
                text=True,
                capture_output=capture_output or sensitive,
                check=check,
                timeout=self.timeout_seconds,
            )
        except subprocess.CalledProcessError as exc:
            returncode = exc.returncode
            message = f"{display} exited with status {returncode}"
        except subprocess.TimeoutExpired:
            message = f"{display} timed out after {self.timeout_seconds} seconds"
        except OSError as exc:
            detail = os.strerror(exc.errno) if exc.errno is not None else type(exc).__name__
            message = f"{display}: {detail}"
        except ValueError:
            message = f"{display}: invalid command or output encoding"
        raise CommandError(message, returncode=returncode)

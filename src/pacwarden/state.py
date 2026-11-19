"""Persist host reboot reasons for the current boot."""

import fcntl
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile

from .reporting import DiagnosticError


@dataclass(frozen=True)
class RebootState:
    reasons: frozenset[str] = frozenset()
    pending_versions: dict[str, str | None] | None = None


@dataclass(frozen=True)
class RebootStateStore:
    directory: Path

    @contextmanager
    def locked(self) -> Iterator[None]:
        """Serialize update runs that share this state directory."""
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path = self.directory / "reboot-required.lock"
        with os.fdopen(os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600), "r+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def read(self, boot_id: str) -> RebootState:
        try:
            contents = (self.directory / "reboot-required.json").read_text(encoding="utf-8")
            saved = json.loads(contents)
        except FileNotFoundError:
            return RebootState()
        except ValueError:
            raise DiagnosticError("Invalid persisted reboot state") from None
        if not isinstance(saved, dict):
            raise DiagnosticError("Invalid persisted reboot state")
        saved_boot_id = saved.get("boot_id")
        if not isinstance(saved_boot_id, str) or not saved_boot_id.strip():
            raise DiagnosticError("Invalid persisted reboot boot ID")
        if saved_boot_id != boot_id:
            return RebootState()
        reasons = saved.get("reasons")
        if not isinstance(reasons, list) or not all(isinstance(item, str) for item in reasons):
            raise DiagnosticError("Invalid persisted reboot reasons")
        pending = saved.get("pending_versions")
        if pending is not None and (
            not isinstance(pending, dict)
            or not all(version is None or isinstance(version, str) for version in pending.values())
        ):
            raise DiagnosticError("Invalid persisted package baseline")
        return RebootState(frozenset(reasons), pending)

    def write(self, boot_id: str, state: RebootState) -> None:
        path = self.directory / "reboot-required.json"
        data: dict[str, object] = {"boot_id": boot_id, "reasons": sorted(state.reasons)}
        if state.pending_versions is not None:
            data["pending_versions"] = state.pending_versions
        handle = NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=".reboot-required-", delete=False
        )
        temporary = Path(handle.name)
        try:
            with handle:
                os.fchmod(handle.fileno(), 0o600)
                json.dump(data, handle, indent=2)
                handle.write("\n")
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

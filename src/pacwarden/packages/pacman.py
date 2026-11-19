import re

from ..execution.base import Executor
from ..reporting import DiagnosticError
from .base import AvailableUpdates, PackageUpdate, parse_installed_versions, run_check_command


_CHECKUPDATES = """\
command -v checkupdates >/dev/null || exit 127
db=$(mktemp -d /tmp/pacwarden-check.XXXXXXXX) || exit 73
trap 'rm -rf -- "$db"' EXIT
CHECKUPDATES_DB="$db" command checkupdates --nocolor
"""
_PACKAGE_NAME = re.compile(r"[a-zA-Z0-9_@+][a-zA-Z0-9_@+.-]*")
_VERSION = re.compile(r"(?:[0-9]+:)?[^\s:/-]+(?:-[0-9]+(?:\.[0-9]+)?)?")


class PacmanPackageManager:
    def available_updates(self, executor: Executor, *, refresh: bool) -> AvailableUpdates:
        # checkupdates already syncs its separate database on every invocation.
        result = run_check_command(
            executor,
            ["env", "-u", "BASH_ENV", "-u", "LIBRARY", "-u", "CHANGE", "LC_ALL=C",
             "bash", "-c", _CHECKUPDATES],
            "Arch update check",
        )
        if result.returncode == 2:
            return AvailableUpdates((), metadata_refreshed=True)
        if result.returncode == 127:
            raise DiagnosticError("checkupdates is missing. Install pacman-contrib on this target.")
        if result.returncode == 73:
            raise DiagnosticError(
                "Cannot create an isolated checkupdates database. Check /tmp permissions and free space."
            )
        if result.returncode != 0:
            raise DiagnosticError(
                f"checkupdates failed (exit {result.returncode}). Check repository access, "
                "local package database permissions, and the pacman-contrib installation."
            )

        updates: dict[str, PackageUpdate] = {}
        for line in result.stdout.splitlines():
            fields = line.split()
            if len(fields) != 4 or fields[2] != "->":
                raise DiagnosticError("Unexpected checkupdates package list format")
            name, installed, _, available = fields
            if (not _PACKAGE_NAME.fullmatch(name) or name in updates or
                    not all(version.isprintable() and _VERSION.fullmatch(version)
                            for version in (installed, available))):
                raise DiagnosticError("Unexpected checkupdates package list format")
            updates[name] = PackageUpdate(name, installed, available)
        if not updates:
            raise DiagnosticError("checkupdates reported success without a package list")
        return AvailableUpdates(tuple(updates.values()), metadata_refreshed=True)

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

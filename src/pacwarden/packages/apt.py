import re
from dataclasses import dataclass

from ..execution.base import Executor
from ..reporting import DiagnosticError, log
from .base import AvailableUpdates, PackageUpdate, parse_installed_versions, run_check_command


_PRESENT_STATES = frozenset({
    "half-installed", "unpacked", "half-configured", "triggers-awaited", "triggers-pending", "installed",
})

# Only the target's system Python has access to its distribution's APT bindings.
_QUERY = """\
try:
    import apt
    import apt_pkg
except ImportError:
    raise SystemExit(11)

apt_pkg.config.set("Dir::Cache::srcpkgcache", "")
for package in apt.Cache(memonly=True):
    if package.is_upgradable and package.installed is not None and package.candidate is not None:
        print(package.fullname, package.installed.version, package.candidate.version, sep="\\t")
"""

# Apply this config after the target's configuration so arbitrary update hooks
# cannot run. Use a separate descriptor to leave stdin unavailable for prompts.
_REFRESH = """\
exec apt-get -c /dev/fd/3 --error-on=any \\
    -o APT::Get::Print-URIs=false -o APT::Get::Download=true update </dev/null 3<<'CONFIG'
#clear APT::Update;
CONFIG
"""
_PACKAGE_NAME = re.compile(r"[a-z0-9][a-z0-9+.-]*(?::[a-z0-9][a-z0-9-]*)?")
_VERSION = re.compile(r"[a-zA-Z0-9.+:~\-]+")


def _apt_error(operation: str, returncode: int, stderr: str | None) -> DiagnosticError:
    detail = (stderr or "").lower()
    if "permission denied" in detail or "are you root?" in detail:
        return DiagnosticError(
            f"{operation} failed. Check permissions on the target's APT metadata and package database."
        )
    if "could not get lock" in detail:
        return DiagnosticError(f"{operation} failed. Another APT operation holds the metadata lock.")
    return DiagnosticError(
        f"{operation} failed (exit {returncode}). Check APT configuration and repository access on the target."
    )


@dataclass(frozen=True)
class AptPackageManager:
    autoremove: bool

    def available_updates(self, executor: Executor, *, refresh: bool) -> AvailableUpdates:
        if refresh:
            result = run_check_command(
                executor, ["env", "LC_ALL=C", "DEBIAN_FRONTEND=noninteractive", "sh", "-c", _REFRESH],
                "APT metadata refresh",
            )
            if result.returncode:
                raise _apt_error("APT metadata refresh", result.returncode, result.stderr)

        result = run_check_command(
            executor, ["env", "LC_ALL=C", "/usr/bin/python3", "-IB", "-c", _QUERY],
            "APT update query",
        )
        if result.returncode in (11, 127):
            raise DiagnosticError("APT's Python bindings are unavailable. Install python3-apt on this target.")
        if result.returncode:
            raise _apt_error("APT update query", result.returncode, result.stderr)

        updates: dict[str, PackageUpdate] = {}
        for line in result.stdout.splitlines():
            fields = line.split("\t")
            if len(fields) != 3:
                raise DiagnosticError("Unexpected APT package list format")
            name, installed, available = fields
            if (not _PACKAGE_NAME.fullmatch(name) or name in updates or
                    not _VERSION.fullmatch(installed) or not _VERSION.fullmatch(available)):
                raise DiagnosticError("Unexpected APT package list format")
            updates[name] = PackageUpdate(name, installed, available)
        return AvailableUpdates(tuple(updates.values()), metadata_refreshed=refresh)

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

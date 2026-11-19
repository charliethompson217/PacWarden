"""Compare the running kernel release with the configured Arch kernel package."""

import os
import re
from collections.abc import Callable

from ..reporting import DiagnosticError


_KERNEL_VERSION_RE = re.compile(r"(\d+(?:\.\d+)+)[.-]arch(\d+)-(\d+)")


def normalize_kernel_version(version: str) -> str:
    match = _KERNEL_VERSION_RE.fullmatch(version.strip())
    if not match:
        raise DiagnosticError("Unexpected Arch kernel version format")
    version_numbers, arch_build, pkg_release = match.groups()
    return f"{version_numbers}-arch{arch_build}-{pkg_release}"


def get_kernel_versions(installed_version: Callable[[], str]) -> tuple[str, str]:
    running = normalize_kernel_version(os.uname().release)
    return running, normalize_kernel_version(installed_version())

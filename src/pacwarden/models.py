from collections.abc import Callable
from dataclasses import dataclass

from .execution.base import Executor
from .packages.base import PackageManager


@dataclass(frozen=True)
class UpdateTarget:
    name: str
    executor: Executor
    package_manager: PackageManager
    prepare: Callable[[], None] | None = None


@dataclass(frozen=True)
class PackageChanges:
    before: dict[str, str]
    after: dict[str, str]

    @property
    def updated_count(self) -> int:
        return sum(self.before.get(package) != version for package, version in self.after.items())


@dataclass(frozen=True)
class UpdateResult:
    name: str
    success: bool
    packages_updated: int | None = 0
    message: str | None = None
    interrupted: bool = False

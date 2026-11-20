import json
from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict, dataclass, replace

from .models import TargetCheckResult, UpdateTarget
from .packages.base import UpdateChecker, UpgradeStatus
from .reporting import DiagnosticError, describe_error, error


@dataclass(frozen=True)
class CheckSummary:
    targets: tuple[TargetCheckResult, ...]

    @property
    def failed_targets(self) -> int:
        return sum(not target.success for target in self.targets)

    @property
    def total_updates(self) -> int:
        return sum(len(target.available.updates) for target in self.targets if target.available is not None)

    def _status_counts(self) -> Counter[UpgradeStatus]:
        return Counter(
            update.status for target in self.targets if target.available is not None
            for update in target.available.updates
        )

    def to_json(self) -> str:
        targets = []
        for result in self.targets:
            available = result.available
            targets.append({
                "name": result.name,
                "distro": result.distro.value,
                "success": result.success,
                "metadata_refreshed": available.metadata_refreshed if available is not None else None,
                "updates": [asdict(update) for update in available.updates] if available is not None else None,
                "error": result.error,
            })
        counts = self._status_counts()
        return json.dumps({
            "targets": targets,
            "total_updates": self.total_updates,
            "failed_targets": self.failed_targets,
            **{f"{status}_updates": counts[status] for status in UpgradeStatus},
        }, indent=2)

    def to_console(self, *, details: bool) -> str:
        blocks = []
        targets_with_updates = 0
        summary_rows = [("TARGET", "DISTRO", "NEWER", "READY", "DEFERRED", "UNASSESSED", "METADATA")]
        for result in self.targets:
            title = f"{result.name} ({result.distro.value})"
            available = result.available
            if available is None:
                if details:
                    blocks.append(f"{title}: FAILED")
                else:
                    summary_rows.append((result.name, result.distro.value, "FAILED", "—", "—", "—", "—"))
                continue

            count = len(available.updates)
            counts = available.status_counts()
            metadata = "refreshed" if available.metadata_refreshed else "cached (may be stale)"
            targets_with_updates += bool(available.updates)
            if not details:
                summary_rows.append((result.name, result.distro.value, str(count), *(
                    str(counts[status]) for status in UpgradeStatus
                ), metadata))
                continue
            block = f"{title}: {_count(count, 'candidate')} — metadata {metadata}"
            if available.updates:
                packages = [("PACKAGE", "INSTALLED", "AVAILABLE", "STATUS"), *(
                    (item.name, item.installed_version, item.available_version, item.status.value)
                    for item in available.updates
                )]
                block += "\n\n" + _table(packages)
            blocks.append(block)

        if not details:
            blocks.append(_table(summary_rows))
        total = f"Total: {_count(self.total_updates, 'candidate')} across {_count(targets_with_updates, 'target')}."
        if self.total_updates:
            counts = self._status_counts()
            total += " " + ", ".join(f"{counts[status]} {status}" for status in UpgradeStatus) + "."
        if self.failed_targets:
            total += f" {_count(self.failed_targets, 'check')} failed."
        blocks.append(total)
        return "\n\n".join(blocks)


def _count(count: int, singular: str) -> str:
    return f"{count} {singular if count == 1 else singular + 's'}"


def _table(rows: Sequence[tuple[str, ...]]) -> str:
    widths = [max(map(len, column)) for column in zip(*rows)]
    return "\n".join("  ".join(value.ljust(width) for value, width in zip(row, widths)).rstrip()
                     for row in rows)


def check_targets(targets: Sequence[UpdateTarget], *, refresh: bool) -> CheckSummary:
    results = []
    for target in targets:
        try:
            manager = target.package_manager
            if not isinstance(manager, UpdateChecker):
                raise DiagnosticError("Package manager has no update check capability")
            if target.prepare is not None:
                target.prepare()
            available = manager.available_updates(target.executor, refresh=refresh)
            updates = tuple(sorted(available.updates, key=lambda update: update.name))
            results.append(TargetCheckResult(target.name, target.distro, replace(available, updates=updates)))
        except Exception as exc:
            message = describe_error(exc)
            error(f"Update check failed on {target.name}: {message}")
            results.append(TargetCheckResult(target.name, target.distro, error=message))
    return CheckSummary(tuple(results))

import argparse
import os
import shutil
import socket
import sys
from collections.abc import Sequence
from functools import partial
from pathlib import Path

from .checking import check_targets
from .config import DEFAULT_CONFIG_PATH, Config, ConfigError, Distro, load_config
from .execution.incus import IncusExecutor, ensure_container_running
from .execution.local import LocalExecutor
from .models import UpdateTarget
from .notifications.base import EventType, NotificationEvent, Notifier
from .notifications.ntfy import NtfyProvider
from .orchestrator import Orchestrator
from .packages.apt import AptPackageManager
from .packages.base import PackageManager
from .packages.pacman import PacmanPackageManager
from .reporting import DiagnosticError, describe_error, error
from .state import RebootStateStore
from .system.arch import get_kernel_versions
from .system.reboot import HostReboot
from .system.services import NginxService


def _add_global_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config", type=Path,
        help=f"Path to a TOML configuration file (default: {DEFAULT_CONFIG_PATH}).",
    )
    parser.add_argument(
        "--no-notifications", action="store_true",
        help="Suppress all ntfy notifications during updates.",
    )


def _add_update_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--no-reboot", action="store_true",
        help="Do not reboot even if a reboot is required.",
    )
    parser.add_argument(
        "--no-apt-autoremove", action="store_true",
        help="Skip apt-get autoremove for Ubuntu containers.",
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check for or apply package updates on an Arch host and its Incus containers."
    )
    _add_global_options(parser)
    _add_update_options(parser)
    commands = parser.add_subparsers(dest="command", metavar="[check|update]")
    # Subcommands only override options actually supplied after the command.
    check = commands.add_parser(
        "check", help="Report available package updates.", argument_default=argparse.SUPPRESS,
        description="Report available package updates on the host and configured Incus containers.",
    )
    _add_global_options(check)
    check.add_argument("--details", action="store_true", default=False,
                       help="Show package names, versions, and upgrade status by target.")
    check.add_argument("--json", action="store_true", default=False,
                       help="Write JSON with package details to stdout.")
    check.add_argument("--refresh", action="store_true", default=False,
                       help="Refresh APT metadata before checking. Arch checks always refresh an isolated database.")
    update = commands.add_parser(
        "update", help="Apply package updates (default).", argument_default=argparse.SUPPRESS,
    )
    _add_global_options(update)
    _add_update_options(update)
    parser.set_defaults(command="update")
    args = parser.parse_args(argv)
    if args.command == "check" and (args.no_reboot or args.no_apt_autoremove):
        parser.error("--no-reboot and --no-apt-autoremove are update options")
    return args


def validate_environment(*, needs_systemctl: bool) -> None:
    if os.geteuid() != 0:
        raise DiagnosticError("Run this script as root (sudo).")
    commands = ("pacman", "systemctl") if needs_systemctl else ("pacman",)
    for binary in commands:
        if shutil.which(binary) is None:
            raise DiagnosticError(f"Required command not found: {binary}")


def _build_targets(
    config: Config, local: LocalExecutor, pacman: PacmanPackageManager, *, apt_autoremove: bool,
) -> tuple[UpdateTarget, ...]:
    managers: dict[Distro, PackageManager] = {
        Distro.ARCH: pacman,
        Distro.UBUNTU: AptPackageManager(apt_autoremove),
    }
    return (UpdateTarget("host", local, pacman), *(
        UpdateTarget(
            container.name, IncusExecutor(container.name, local), managers[container.distro],
            prepare=partial(ensure_container_running, local, container.name), distro=container.distro,
        )
        for container in config.containers
    ))


def main(argv: Sequence[str] | None = None) -> int:
    notifier = None
    hostname = None
    checking = False
    try:
        args = parse_args(argv)
        checking = args.command == "check"
        try:
            config = load_config(args.config, disable_notifications=checking or args.no_notifications)
        except ConfigError as exc:
            error(str(exc))
            return 2
        check_nginx = config.services.restart_nginx_after_host_updates
        if not checking:
            ntfy = config.notifications.ntfy
            notifier = Notifier(NtfyProvider(ntfy.url, ntfy.timeout_seconds), config.notifications)
            hostname = socket.gethostname()
            validate_environment(needs_systemctl=check_nginx or (config.reboot.auto_reboot and not args.no_reboot))

        local = LocalExecutor(config.command_timeout_seconds, log_commands=not checking)
        pacman = PacmanPackageManager()
        targets = _build_targets(
            config, local, pacman,
            apt_autoremove=not checking and config.apt_autoremove and not args.no_apt_autoremove,
        )
        if checking:
            print("Checking for available package updates...\n", file=sys.stderr if args.json else sys.stdout,
                  flush=True)
            summary = check_targets(targets, refresh=args.refresh)
            print(summary.to_json() if args.json else summary.to_console(details=args.details))
            return 1 if summary.failed_targets else 0

        assert notifier is not None and hostname is not None
        host, *containers = targets
        kernel_package = config.reboot.kernel_package
        installed_kernel = partial(pacman.installed_version, local, kernel_package)
        orchestrator = Orchestrator(
            host=host,
            containers=tuple(containers),
            nginx=NginxService(local, check_nginx),
            reboot=HostReboot(
                executor=local,
                store=RebootStateStore(config.state_dir),
                reboot_packages=tuple(
                    package for package in config.reboot.reboot_packages if package != kernel_package
                ),
                kernel_versions=partial(get_kernel_versions, installed_kernel),
                auto_reboot=config.reboot.auto_reboot,
            ),
            notifier=notifier,
            hostname=hostname,
        )
        result = orchestrator.run(no_reboot=args.no_reboot)
        return 0 if result.success else 1
    except KeyboardInterrupt:
        error("Update check interrupted." if checking else "Update interrupted. The host will not reboot.")
        return 130
    except Exception as exc:
        details = describe_error(exc)
        if checking:
            error(f"Update check crashed: {details}")
            return 1
        error(f"Update script crashed: {details}")
        if notifier is not None and hostname is not None:
            event = NotificationEvent(
                EventType.SCRIPT_CRASHED,
                f"{hostname}: update script crashed",
                f"The update script crashed on {hostname}.\n\n{details}\n\nThe host will not reboot.",
                priority="urgent", tags=("rotating_light",),
            )
            notifier.send(event)
        return 1

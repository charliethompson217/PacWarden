import argparse
import os
import shutil
import socket
from collections.abc import Sequence
from functools import partial
from pathlib import Path

from .config import DEFAULT_CONFIG_PATH, ConfigError, Distro, load_config
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


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Update an Arch host and its configured Incus containers."
    )
    parser.add_argument(
        "--config", type=Path,
        help=f"Path to a TOML configuration file (default: {DEFAULT_CONFIG_PATH}).",
    )
    parser.add_argument(
        "--no-reboot", action="store_true",
        help="Do not reboot even if a reboot is required.",
    )
    parser.add_argument(
        "--no-apt-autoremove", action="store_true",
        help="Skip apt-get autoremove for Ubuntu containers.",
    )
    parser.add_argument(
        "--no-notifications", action="store_true",
        help="Suppress all ntfy notifications.",
    )
    return parser.parse_args(argv)


def validate_environment(*, needs_systemctl: bool) -> None:
    if os.geteuid() != 0:
        raise DiagnosticError("Run this script as root (sudo).")
    commands = ("pacman", "systemctl") if needs_systemctl else ("pacman",)
    for binary in commands:
        if shutil.which(binary) is None:
            raise DiagnosticError(f"Required command not found: {binary}")


def main(argv: Sequence[str] | None = None) -> int:
    notifier = None
    hostname = None
    try:
        args = parse_args(argv)
        try:
            config = load_config(args.config, disable_notifications=args.no_notifications)
        except ConfigError as exc:
            error(str(exc))
            return 2
        ntfy = config.notifications.ntfy
        notifier = Notifier(NtfyProvider(ntfy.url, ntfy.timeout_seconds), config.notifications)
        hostname = socket.gethostname()
        check_nginx = config.services.restart_nginx_after_host_updates
        validate_environment(needs_systemctl=check_nginx or (config.reboot.auto_reboot and not args.no_reboot))

        local = LocalExecutor(config.command_timeout_seconds)
        pacman = PacmanPackageManager()
        managers: dict[Distro, PackageManager] = {
            Distro.ARCH: pacman,
            Distro.UBUNTU: AptPackageManager(config.apt_autoremove and not args.no_apt_autoremove),
        }
        containers = []
        for container in config.containers:
            executor = IncusExecutor(container.name, local)
            containers.append(UpdateTarget(
                container.name, executor, managers[container.distro],
                partial(ensure_container_running, local, container.name),
            ))

        kernel_package = config.reboot.kernel_package
        installed_kernel = partial(pacman.installed_version, local, kernel_package)
        orchestrator = Orchestrator(
            host=UpdateTarget("host", local, pacman),
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
        error("Update interrupted. The host will not reboot.")
        return 130
    except Exception as exc:
        details = describe_error(exc)
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

"""Load and validate PacWarden's TOML configuration."""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field, fields
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


DEFAULT_CONFIG_PATH = Path("/etc/pacwarden/config.toml")
# Linux poll timeouts are bounded by a signed 32-bit millisecond count.
_MAX_TIMEOUT_SECONDS = 2_147_483


class ConfigError(ValueError):
    """The configuration could not be read or contains an invalid setting."""


class Distro(Enum):
    ARCH = "arch"
    UBUNTU = "ubuntu"


@dataclass(frozen=True)
class Container:
    name: str
    distro: Distro


@dataclass(frozen=True)
class RebootConfig:
    auto_reboot: bool = True
    kernel_package: str = "linux"
    reboot_packages: tuple[str, ...] = ("linux", "systemd", "glibc")


@dataclass(frozen=True)
class ServicesConfig:
    restart_nginx_after_host_updates: bool = True


@dataclass(frozen=True)
class NtfyConfig:
    url: str | None = field(default=None, repr=False)
    timeout_seconds: int = 10


@dataclass(frozen=True)
class NotificationsConfig:
    enabled: bool = True
    success_no_updates: bool = False
    success_with_updates: bool = True
    update_failed: bool = True
    reboot_required: bool = True
    rebooting: bool = True
    script_crashed: bool = True
    service_restarted: bool = False
    service_restart_failed: bool = True
    ntfy: NtfyConfig = NtfyConfig()


@dataclass(frozen=True)
class Config:
    containers: tuple[Container, ...] = ()
    apt_autoremove: bool = True
    command_timeout_seconds: int = 3600
    state_dir: Path = Path("/var/lib/update-server")
    reboot: RebootConfig = RebootConfig()
    services: ServicesConfig = ServicesConfig()
    notifications: NotificationsConfig = NotificationsConfig()


def _check_keys(table: dict[str, Any], location: str, allowed: set[str]) -> None:
    if table.keys() - allowed:
        # An unknown key can contain a credential pasted into the wrong place.
        allowed_keys = ", ".join(sorted(allowed))
        raise ConfigError(f"{location} has an unknown key. Allowed keys: {allowed_keys}")


def _table(value: object, location: str, allowed: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{location} must be a TOML table")
    _check_keys(value, location, allowed)
    return value


def _boolean(value: object, location: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{location} must be true or false")
    return value


def _seconds(value: object, location: str) -> int:
    if type(value) is not int or not 1 <= value <= _MAX_TIMEOUT_SECONDS:
        raise ConfigError(
            f"{location} must be an integer from 1 to {_MAX_TIMEOUT_SECONDS} seconds"
        )
    return value


def _string(value: object, location: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{location} must be a non-empty string")
    return value


def _name(value: object, location: str) -> str:
    name = _string(value, location)
    if name.startswith("-") or any(char.isspace() or not char.isprintable() for char in name):
        raise ConfigError(f"{location} must be a name without whitespace or a leading hyphen")
    return name


def _package_name(value: object, location: str) -> str:
    name = _string(value, location)
    if not re.fullmatch(r"[a-zA-Z0-9_@+][a-zA-Z0-9_@+.-]*", name):
        raise ConfigError(f"{location} must be an Arch package name")
    return name


def _parse_containers(value: object) -> tuple[Container, ...]:
    if not isinstance(value, list):
        raise ConfigError("containers must be an array of tables")

    containers = []
    names = set()
    for index, item in enumerate(value, start=1):
        location = f"containers entry {index}"
        table = _table(item, location, {"name", "distro"})
        name = _name(table.get("name"), f"{location}.name")
        if name in names:
            raise ConfigError(f"{location}.name must be unique")
        try:
            distro = Distro(table.get("distro"))
        except ValueError:
            raise ConfigError(f'{location}.distro must be "arch" or "ubuntu"') from None
        names.add(name)
        containers.append(Container(name, distro))
    return tuple(containers)


def _parse_reboot(value: object, defaults: RebootConfig) -> RebootConfig:
    table = _table(value, "reboot", {"auto_reboot", "kernel_package", "reboot_packages"})
    kernel = _package_name(
        table.get("kernel_package", defaults.kernel_package), "reboot.kernel_package"
    )
    if "reboot_packages" in table:
        packages = table["reboot_packages"]
        if not isinstance(packages, list):
            raise ConfigError("reboot.reboot_packages must be an array of package names")
        reboot_packages = tuple(
            _package_name(package, f"reboot.reboot_packages entry {index}")
            for index, package in enumerate(packages, start=1)
        )
        if len(set(reboot_packages)) != len(reboot_packages):
            raise ConfigError("reboot.reboot_packages must contain unique package names")
    else:
        reboot_packages = tuple(
            kernel if package == defaults.kernel_package else package
            for package in defaults.reboot_packages
        )

    auto_reboot = _boolean(table.get("auto_reboot", defaults.auto_reboot), "reboot.auto_reboot")
    return RebootConfig(auto_reboot=auto_reboot, kernel_package=kernel, reboot_packages=reboot_packages)


def _validate_url(value: object, location: str) -> str:
    url = _string(value, location)
    if "\\" not in url and not any(char.isspace() or not char.isprintable() for char in url):
        try:
            parts = urlsplit(url)
            if parts.scheme in {"http", "https"} and parts.hostname and parts.port != 0:
                return url
        except ValueError:
            pass
    raise ConfigError(f"{location} must be a valid HTTP or HTTPS URL with a hostname")


def _parse_ntfy(value: object, defaults: NtfyConfig, *, enabled: bool) -> NtfyConfig:
    table = _table(value, "notifications.ntfy", {"url", "url_env", "timeout_seconds"})
    timeout = _seconds(
        table.get("timeout_seconds", defaults.timeout_seconds), "notifications.ntfy.timeout_seconds"
    )
    if "url" in table and "url_env" in table:
        raise ConfigError("Set either notifications.ntfy.url or notifications.ntfy.url_env")

    url = None
    if "url" in table:
        url = _validate_url(table["url"], "notifications.ntfy.url")
    elif "url_env" in table:
        name = _string(table["url_env"], "notifications.ntfy.url_env")
        if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", name):
            raise ConfigError("notifications.ntfy.url_env must name an environment variable")
        if enabled:
            env_url = os.environ.get(name)
            if not env_url or not env_url.strip():
                raise ConfigError(
                    "Set the variable named by notifications.ntfy.url_env to the ntfy URL "
                    "in PacWarden's environment"
                )
            url = _validate_url(env_url, "The variable named by notifications.ntfy.url_env")
    return NtfyConfig(url=url, timeout_seconds=timeout)


def _parse_notifications(
    value: object, defaults: NotificationsConfig, *, disable_notifications: bool
) -> NotificationsConfig:
    switch_names = {item.name for item in fields(NotificationsConfig)} - {"ntfy"}
    table = _table(value, "notifications", switch_names | {"ntfy"})
    switches = {
        name: _boolean(table.get(name, getattr(defaults, name)), f"notifications.{name}")
        for name in sorted(switch_names)
    }
    switches["enabled"] = switches["enabled"] and not disable_notifications
    ntfy = _parse_ntfy(table.get("ntfy", {}), defaults.ntfy, enabled=switches["enabled"])
    return NotificationsConfig(**switches, ntfy=ntfy)


def _parse_config(data: dict[str, Any], *, disable_notifications: bool) -> Config:
    defaults = Config()
    _check_keys(data, "Configuration", {item.name for item in fields(Config)})

    containers = (
        _parse_containers(data["containers"]) if "containers" in data else defaults.containers
    )
    state_dir = _string(data.get("state_dir", str(defaults.state_dir)), "state_dir")
    if not Path(state_dir).is_absolute() or "\0" in state_dir:
        raise ConfigError("state_dir must be an absolute directory path")

    services = _table(data.get("services", {}), "services", {"restart_nginx_after_host_updates"})
    restart_nginx = _boolean(
        services.get(
            "restart_nginx_after_host_updates", defaults.services.restart_nginx_after_host_updates
        ),
        "services.restart_nginx_after_host_updates",
    )

    return Config(
        containers=containers,
        apt_autoremove=_boolean(data.get("apt_autoremove", defaults.apt_autoremove), "apt_autoremove"),
        command_timeout_seconds=_seconds(
            data.get("command_timeout_seconds", defaults.command_timeout_seconds),
            "command_timeout_seconds",
        ),
        state_dir=Path(state_dir),
        reboot=_parse_reboot(data.get("reboot", {}), defaults.reboot),
        services=ServicesConfig(restart_nginx_after_host_updates=restart_nginx),
        notifications=_parse_notifications(
            data.get("notifications", {}),
            defaults.notifications,
            disable_notifications=disable_notifications,
        ),
    )


def load_config(path: Path | None = None, *, disable_notifications: bool = False) -> Config:
    """Use built-in defaults if the implicit config path does not exist."""
    config_path = DEFAULT_CONFIG_PATH if path is None else path
    try:
        with config_path.open("rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError:
        if path is not None:
            raise ConfigError(
                f"{config_path}: file not found. Create it from config.example.toml"
            ) from None
        data = {}
    except tomllib.TOMLDecodeError as exc:
        # Older tomllib versions include the position only in the exception text.
        position = re.search(r" \((at line [0-9]+, column [0-9]+)\)$", str(exc))
        location = f" {position[1]}" if position else ""
        raise ConfigError(
            f"{config_path}: invalid TOML syntax{location}. Check the file against config.example.toml"
        ) from None
    except UnicodeDecodeError:
        raise ConfigError(f"{config_path}: save the configuration file as UTF-8") from None
    except (ValueError, RecursionError):
        raise ConfigError(f"{config_path}: TOML contains an unsupported value") from None
    except OSError as exc:
        raise ConfigError(f"{config_path}: cannot read configuration ({exc.strerror})") from None

    try:
        return _parse_config(data, disable_notifications=disable_notifications)
    except ConfigError as exc:
        raise ConfigError(f"{config_path}: {exc}") from None

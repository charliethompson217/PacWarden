# PacWarden

PacWarden keeps an Arch Linux host and its Arch Linux and Ubuntu Incus containers up to date with one command. Run it on demand or schedule it with systemd to keep up with package updates and know when the host needs a reboot.

## Features

- Update the Arch Linux host and any Arch Linux or Ubuntu Incus containers you configure.
- Run hourly with the included systemd timer.
- Detect when the host needs a reboot and choose whether manual runs may reboot it automatically.
- Validate and restart active NGINX after host package updates.
- Send configurable ntfy push notifications for updates, failures, and reboots.
- Manage settings in one TOML file.

PacWarden is designed to support more distributions, execution environments, and notification providers in the future.

## Requirements

- An Arch Linux host running systemd, with root or sudo access.
- Python 3.11 or newer and the Arch packages `python-pip`, `python-setuptools` (77 or newer), and `python-wheel` for installation. Install Git if you plan to clone the repository.
- Incus on the host if you want to update containers. The configured containers must already be running.

## Installation

```bash
git clone https://github.com/charliethompson217/PacWarden.git
cd PacWarden
sudo ./install.sh
```

The installer uses a dedicated virtual environment at `/opt/pacwarden/venv` and makes the `pacwarden` command available in `/usr/local/bin`. Review the configuration before running updates or enabling the timer.

To upgrade or reinstall from an updated checkout, run `git pull --ff-only` and then `sudo ./install.sh`. Reinstalls preserve your configuration, reboot-tracking state, and timer settings. An update already in progress can finish using its original environment. If an installed unit differs from the supplied file, review the difference and back up the unit before replacing it. Use systemd drop-ins for local changes.

## Configuration

```bash
sudoedit /etc/pacwarden/config.toml
```

The example starts with host-only updates. To include a container, replace `containers = []` with a block like this above the other section headers. Add one block per container, using `arch` or `ubuntu` for `distro`.

```toml
[[containers]]
name = "web"
distro = "ubuntu"
```

Review `[reboot]` and set `kernel_package` and `reboot_packages` for your host if you use a kernel such as `linux-lts`. Set `auto_reboot = false` to require manual reboots. To receive ntfy notifications, set the full HTTPS topic URL under `[notifications.ntfy]` in the root-only configuration file. See [config.example.toml](config.example.toml) for all settings and defaults.

## Usage

Run an update manually with automatic reboot and Ubuntu package autoremove disabled.

```bash
sudo /usr/local/bin/pacwarden --no-reboot --no-apt-autoremove
```

This command applies package updates and reports whether the host needs a reboot. Without the overrides, manual runs follow your TOML settings, which permit automatic reboot and Ubuntu autoremove by default. Run `pacwarden --help` to see all options, including `--config PATH` and `--no-notifications`.

### Check for available updates

```bash
pacwarden check                 # Count newer, ready, and deferred packages by target
pacwarden check --details       # Show package names, versions, and upgrade status
pacwarden check --json          # Write structured results to stdout
pacwarden check --refresh       # Refresh Ubuntu repository metadata first
```

Checks run on the host first and then on the configured containers in order. They leave installed packages, services, and reboot state untouched and send no notifications. Use `--config PATH` before or after `check` to select a configuration. The installer creates a root-only default config. A host check can run without root with a readable config. Container checks require Incus access, and APT refreshes require permission to write its metadata on the target.

- **Arch** checks require `pacman-contrib` and `fakeroot` on the host and each Arch container. Arch lists `fakeroot` as optional for `pacman-contrib`, but `checkupdates` requires it. Install both through your usual full-system update workflow. `checkupdates` refreshes an isolated sync database on every check and leaves the live pacman database alone. Results cover configured binary repositories. AUR packages built locally are outside that scope unless they are also in a configured repository. Arch candidates are reported as `unassessed` because `checkupdates` only compares versions.
- **Ubuntu** checks require `python3-apt` on each Ubuntu container. Cached repository metadata can be stale or empty. `--refresh` uses `apt-get update` with update hooks disabled and requires APT support for `--error-on=any` (available on Ubuntu 22.04 LTS and later). PacWarden queries the installed packages with newer APT candidates and runs APT's full-upgrade resolver in memory. Candidates marked for upgrade are `ready`. Other candidates are `deferred`, which can happen because of phasing, holds, or dependency constraints. The plan reflects the current metadata and package state and can change before a later update.

JSON always includes package details, including when `--details` is also supplied. The top-level fields are `targets`, `total_updates` (the total number of candidates from successful checks), `failed_targets`, and the counts `ready_updates`, `deferred_updates`, and `unassessed_updates`. Each target has `name`, `distro`, `success`, `metadata_refreshed`, `updates`, and `error`. On success, `metadata_refreshed` is `true` for a refresh in this run or `false` for cached metadata, `updates` is a list sorted by package name, and `error` is `null`. Each package has `name`, `installed_version`, `available_version`, and `status` (`ready`, `deferred`, or `unassessed`). On failure, `metadata_refreshed` and `updates` are `null` and `error` is a diagnostic string. Operational messages go to stderr in JSON mode.

Exit status is `0` if all checks succeed, even when updates are available, `1` if any target fails, and `2` for a configuration or CLI usage error. An interrupt exits with `130`. To apply updates, use `pacwarden update` or the existing invocation without a subcommand.

## Automatic updates with systemd

The included timer runs hourly and catches up on a missed run after downtime. Scheduled updates detect pending reboots and skip automatic reboot and Ubuntu autoremove.

```bash
sudo systemctl enable --now pacwarden.timer
systemctl list-timers --all pacwarden.timer
sudo journalctl -u pacwarden.service -n 50
```

## License

PacWarden is released under the [MIT License](LICENSE).

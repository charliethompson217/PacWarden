#!/usr/bin/env bash
set -Eeuo pipefail
export PATH=/usr/bin:/bin
export LC_ALL=C
unset PYTHONHOME PYTHONPATH
umask 022

die() {
    printf 'PacWarden installer: %s\n' "$*" >&2
    exit 1
}

[[ $EUID == 0 ]] || die 'Run this installer as root with sudo ./install.sh.'
[[ $(uname -s) == Linux ]] || die 'PacWarden requires an Arch Linux host.'
os_release=/etc/os-release
[[ -r $os_release ]] || os_release=/usr/lib/os-release
[[ -r $os_release ]] || die 'Cannot identify the host. No readable os-release file.'
# shellcheck source=/dev/null
. "$os_release"
[[ ${ID:-} == arch ]] || die 'PacWarden requires an Arch Linux host.'

for command in pacman systemctl python3 flock install cp cmp ln mv readlink mktemp rm sha256sum stat chmod; do
    command -v "$command" >/dev/null || die "Required command '$command' is missing."
done
python3 -I -c 'import sys, venv; sys.exit(sys.version_info < (3, 11))' ||
    die 'Install the Arch Linux python package (Python 3.11 or newer with venv support).'

# Use the Arch build tools already installed on the host. Pip cannot use an index
# or load local pip settings, and only installs into the new virtual environment.
export PIP_CONFIG_FILE=/dev/null
pip=(python3 -I -m pip --isolated --disable-pip-version-check --no-cache-dir --no-input)
"${pip[@]}" --version >/dev/null 2>&1 &&
    python3 -I -c 'import setuptools, wheel' >/dev/null 2>&1 ||
    die 'Install the Arch packages python-pip, python-setuptools, and python-wheel first.'

source_dir=$(CDPATH='' cd -- "${BASH_SOURCE[0]%/*}" && pwd -P)
prefix=/opt/pacwarden
releases=$prefix/releases
venv=$prefix/venv
command_link=/usr/local/bin/pacwarden
config_dir=/etc/pacwarden
config=$config_dir/config.toml
unit_dir=/etc/systemd/system
units=(pacwarden.service pacwarden.timer)

exists() {
    [[ -e $1 || -L $1 ]]
}

check_owned() {
    local mode
    [[ ! -L $1 && -O $1 ]] || die "$1 must be root-owned and cannot be a symlink."
    mode=$(stat -c '%a' -- "$1")
    (( (8#$mode & 8#022) == 0 )) || die "$1 must not be writable by group or others."
}

ensure_directory() {
    if exists "$1"; then
        [[ -d $1 ]] || die "$1 is not a directory."
        check_owned "$1"
    else
        install -d -o root -g root -m "$2" -- "$1"
    fi
}

unit_property() {
    systemctl --system show --no-pager --property="$1" --value "$2"
}

# Query the live system manager and check the paths it searches, including
# unit files added on disk since the last daemon-reload.
paths=$(systemctl --system show --no-pager --property=UnitPath --value) ||
    die 'Could not reach the system systemd manager.'
read -r -a unit_paths <<< "$paths"
[[ ${#unit_paths[@]} -gt 0 ]] || die 'Could not determine the systemd unit paths.'

check_units() {
    local unit path fragment active enabled legacy_found=false
    for unit in "${units[@]}"; do
        fragment=$(unit_property FragmentPath "$unit") || die "Could not inspect $unit."
        if [[ -n $fragment && ( $fragment != "$unit_dir/$unit" || ! -f $fragment ) ]]; then
            die "$unit is loaded from $fragment. Review the existing unit before rerunning."
        fi
        for path in "${unit_paths[@]}"; do
            if [[ $path != "$unit_dir" ]] && exists "$path/$unit"; then
                die "Found $path/$unit. Review the existing unit before rerunning."
            fi
        done
        if exists "$unit_dir/$unit"; then
            [[ -f $unit_dir/$unit ]] || die "$unit_dir/$unit is not a regular unit file."
            check_owned "$unit_dir/$unit"
            cmp -s -- "$source_dir/systemd/$unit" "$unit_dir/$unit" ||
                die "$unit_dir/$unit differs from the supplied unit. Back up and reconcile it before rerunning."
        fi
    done

    for unit in update-server.timer update-server.service; do
        active=$(unit_property ActiveState "$unit") || die "Could not inspect $unit."
        case "$active" in
            inactive|failed) ;;
            *) die "$unit is $active. Disable and stop the old timer, then let any running update finish." ;;
        esac
        enabled=$(unit_property UnitFileState "$unit") || die "Could not inspect $unit."
        if [[ -z $enabled ]]; then
            for path in "${unit_paths[@]}"; do
                if exists "$path/$unit"; then
                    die "Found $path/$unit but systemd could not report its enablement. Review it and reload systemd before rerunning."
                fi
            done
            continue
        fi
        legacy_found=true
        case "$unit:$enabled" in
            *:disabled|*:masked|update-server.service:static) ;;
            *) die "$unit is $enabled. Disable its automatic activation before rerunning." ;;
        esac
    done
    if [[ $legacy_found == true ]]; then
        printf '%s\n' 'Found inactive update-server units. Keep the old schedule disabled when enabling pacwarden.timer.'
    fi
}

[[ -f $source_dir/pyproject.toml && -f $source_dir/README.md &&
    -f $source_dir/config.example.toml &&
    -f $source_dir/systemd/pacwarden.service && -f $source_dir/systemd/pacwarden.timer &&
    -d $source_dir/src/pacwarden ]] || die 'Run install.sh from a complete PacWarden source checkout.'

check_owned "${prefix%/*}"
check_owned /usr/local
check_owned "$unit_dir"
ensure_directory "$prefix" 0755
ensure_directory "$releases" 0755
ensure_directory "${command_link%/*}" 0755
ensure_directory "$config_dir" 0700
[[ ! -L $prefix/.install.lock ]] || die "$prefix/.install.lock cannot be a symlink."
exec {lock_fd}>>"$prefix/.install.lock"
flock -n "$lock_fd" || die 'Another PacWarden installer is running. Try again after it finishes.'
check_units

current=
if exists "$venv"; then
    [[ -L $venv ]] || die "$venv is an existing unmanaged environment. Back it up after any updates finish."
    current=$(readlink -- "$venv")
    [[ $current == "$releases"/venv.* && ${current%/*} == "$releases" && ! -L $current ]] ||
        die "$venv does not point to an installer-managed environment. Review it before rerunning."
    if exists "$current"; then
        check_owned "$current"
    fi
fi
if exists "$command_link"; then
    [[ -L $command_link && $(readlink -- "$command_link") == "$venv/bin/pacwarden" ]] ||
        die "$command_link already exists and points elsewhere. Review it before rerunning."
fi
if exists "$config"; then
    [[ -f $config && ! -L $config ]] || die "$config must be a regular file."
    [[ -O $config && $(stat -c '%h' -- "$config") == 1 ]] ||
        die "$config must be a root-owned file without hard links. Review it before rerunning."
    chmod 0600 -- "$config"
fi

work=$(mktemp -d "$prefix/.install.XXXXXXXX")
candidate=
pending_file=
cleanup() {
    if [[ -n $pending_file ]]; then
        rm -f -- "$pending_file"
    fi
    if [[ -n $candidate && ! $venv -ef $candidate ]]; then
        rm -rf -- "$candidate"
    fi
    rm -rf -- "$work"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'die "Installation failed at line $LINENO. Resolve the error above and rerun sudo ./install.sh."' ERR

publish_file() {
    local source=$1 destination=$2 mode=$3
    if exists "$destination"; then
        return
    fi
    pending_file=$(mktemp "${destination%/*}/.pacwarden-install.XXXXXXXX")
    install -o root -g root -m "$mode" -- "$source" "$pending_file"
    ln -T -- "$pending_file" "$destination"
    rm -- "$pending_file"
    pending_file=
}

# Build in a temporary copy so installer-created files stay out of the checkout.
install -d -- "$work/source" "$work/wheels"
cp -R -- "$source_dir/src" "$work/source/"
shopt -s nullglob
cp -- "$source_dir/pyproject.toml" "$source_dir/README.md" "$source_dir"/LICENSE* "$work/source/"
# Fix archive timestamps so identical builds can reuse the installed environment.
SOURCE_DATE_EPOCH=315532800 "${pip[@]}" wheel --no-index --no-deps --no-build-isolation \
    --check-build-dependencies --wheel-dir "$work/wheels" "$work/source"
wheels=("$work/wheels/"*.whl)
[[ ${#wheels[@]} == 1 ]] || die 'Expected the build to produce one PacWarden wheel.'
digest=$(sha256sum < "${wheels[0]}")
python_version=$(python3 -I -c 'import sys; print(sys.version)')
build_id="${digest%% *} $python_version"

if [[ -n $current && -x $current/bin/pacwarden && -f $current/.build-id &&
      $(<"$current/.build-id") == "$build_id" ]]; then
    printf '%s\n' 'This PacWarden build is already installed.'
else
    # Entry points embed the interpreter's absolute path. Build at the permanent
    # release path, then switch the venv symlink without removing older releases.
    candidate=$(mktemp -d "$releases/venv.XXXXXXXX")
    python3 -I -m venv --without-pip "$candidate"
    "${pip[@]}" --python "$candidate" install --no-index "${wheels[0]}"
    [[ -x $candidate/bin/pacwarden ]] || die 'The installed wheel did not provide the pacwarden command.'
    printf '%s\n' "$build_id" > "$candidate/.build-id"
    chmod 0755 -- "$candidate"
    ln -s -- "$candidate" "$work/venv"
fi

publish_file "$source_dir/config.example.toml" "$config" 0600
if [[ -n $candidate ]]; then
    mv -Tf -- "$work/venv" "$venv"
fi
if ! exists "$command_link"; then
    ln -sT -- "$venv/bin/pacwarden" "$command_link"
fi
for unit in "${units[@]}"; do
    publish_file "$source_dir/systemd/$unit" "$unit_dir/$unit" 0644
done
systemctl --system daemon-reload ||
    die 'Files are installed, but systemd could not reload. Resolve the error and run sudo systemctl daemon-reload.'

cat <<'INSTRUCTIONS'

PacWarden is installed. Review your configuration first.
  sudoedit /etc/pacwarden/config.toml

Run updates manually with automatic reboot and Ubuntu autoremove disabled.
  sudo /usr/local/bin/pacwarden --no-reboot --no-apt-autoremove

To opt in to hourly updates, enable the timer.
  sudo systemctl enable --now pacwarden.timer

Inspect the schedule and service logs.
  systemctl list-timers --all pacwarden.timer
  sudo journalctl -u pacwarden.service
INSTRUCTIONS

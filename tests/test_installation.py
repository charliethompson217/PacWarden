import configparser
import shlex
import shutil
import subprocess
import tomllib
import unittest
from pathlib import Path

from pacwarden.cli import parse_args
from pacwarden.config import DEFAULT_CONFIG_PATH, load_config


ROOT = Path(__file__).resolve().parents[1]


class InstallationTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("bash"), "Bash is required for installer syntax validation")
    def test_installer_is_executable_bash(self):
        installer = ROOT / "install.sh"
        self.assertTrue(installer.stat().st_mode & 0o111)
        subprocess.run(["bash", "-n", installer], check=True)

    def test_service_uses_installed_entry_point_with_safe_scheduled_options(self):
        package = tomllib.loads((ROOT / "pyproject.toml").read_text())
        entry_points = package["project"]["scripts"]
        unit = configparser.ConfigParser()
        unit.read(ROOT / "systemd/pacwarden.service")
        service = unit["Service"]
        executable, *flags = shlex.split(service["ExecStart"])
        self.assertEqual(executable, "/opt/pacwarden/venv/bin/pacwarden")
        self.assertEqual(entry_points[Path(executable).name], "pacwarden.cli:main")
        self.assertEqual(service["Type"], "oneshot")
        self.assertEqual(service["TimeoutStartSec"], "2h")
        self.assertEqual(service.getint("Nice"), 10)
        self.assertEqual(service["IOSchedulingClass"], "best-effort")
        self.assertEqual(service.getint("IOSchedulingPriority"), 7)
        options = parse_args(flags)
        self.assertTrue(options.no_reboot)
        self.assertTrue(options.no_apt_autoremove)
        self.assertIsNone(options.config)
        self.assertNotIn("Install", unit)

    def test_timer_is_hourly_and_persistent(self):
        unit = configparser.ConfigParser()
        unit.read(ROOT / "systemd/pacwarden.timer")
        self.assertEqual(unit["Timer"]["OnCalendar"], "hourly")
        self.assertEqual(unit["Timer"]["AccuracySec"], "1min")
        self.assertTrue(unit["Timer"].getboolean("Persistent"))
        self.assertEqual(unit["Install"]["WantedBy"], "timers.target")

    def test_example_configuration_retains_default_reboot_state_path(self):
        config = load_config(ROOT / "config.example.toml")
        self.assertEqual(config.state_dir, Path("/var/lib/update-server"))
        self.assertEqual(DEFAULT_CONFIG_PATH, Path("/etc/pacwarden/config.toml"))

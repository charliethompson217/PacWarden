import os
import tempfile
import unittest
from pathlib import Path
from textwrap import dedent
from unittest.mock import patch

from pacwarden.config import DEFAULT_CONFIG_PATH, Config, ConfigError, Container, Distro, load_config


class ConfigTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(directory) / "config.toml"

    def load(self, contents, **kwargs):
        self.path.write_text(dedent(contents), encoding="utf-8")
        return load_config(self.path, **kwargs)

    def test_defaults_match_example(self):
        config = self.load("")
        self.assertEqual(config, Config())
        self.assertEqual(DEFAULT_CONFIG_PATH, Path("/etc/pacwarden/config.toml"))
        self.assertEqual(config.state_dir, Path("/var/lib/update-server"))
        self.assertEqual(config.command_timeout_seconds, 3600)
        self.assertTrue(config.apt_autoremove)
        self.assertTrue(config.services.restart_nginx_after_host_updates)
        self.assertTrue(config.reboot.auto_reboot)
        self.assertEqual(config.reboot.kernel_package, "linux")
        self.assertEqual(config.reboot.reboot_packages, ("linux", "systemd", "glibc"))
        self.assertEqual(config.containers, ())
        self.assertTrue(config.notifications.enabled)
        self.assertIsNone(config.notifications.ntfy.url)
        self.assertEqual(config.notifications.ntfy.timeout_seconds, 10)
        example = Path(__file__).resolve().parents[1] / "config.example.toml"
        self.assertEqual(load_config(example), config)

    def test_loads_overrides_and_preserves_container_order(self):
        config = self.load('''
            apt_autoremove = false
            command_timeout_seconds = 120
            state_dir = "/var/lib/pacwarden"

            [[containers]]
            name = "ubuntu-test"
            distro = "ubuntu"

            [[containers]]
            name = "remote:arch-test"
            distro = "arch"

            [reboot]
            auto_reboot = false
            kernel_package = "linux-test"
            reboot_packages = ["glibc", "amd-ucode"]

            [services]
            restart_nginx_after_host_updates = false

            [notifications.ntfy]
            url = "https://username:synthetic-token@ntfy.example.invalid/topic"
            timeout_seconds = 5
        ''')
        self.assertFalse(config.apt_autoremove)
        self.assertEqual(config.command_timeout_seconds, 120)
        self.assertEqual(config.state_dir, Path("/var/lib/pacwarden"))
        self.assertEqual(config.containers, (
            Container("ubuntu-test", Distro.UBUNTU),
            Container("remote:arch-test", Distro.ARCH),
        ))
        self.assertFalse(config.reboot.auto_reboot)
        self.assertEqual(config.reboot.kernel_package, "linux-test")
        self.assertEqual(config.reboot.reboot_packages, ("glibc", "amd-ucode"))
        self.assertFalse(config.services.restart_nginx_after_host_updates)
        self.assertEqual(config.notifications.ntfy.timeout_seconds, 5)
        self.assertEqual(config.notifications.ntfy.url,
                         "https://username:synthetic-token@ntfy.example.invalid/topic")
        self.assertNotIn("synthetic-token", repr(config))

    def test_empty_lists_and_custom_kernel_default(self):
        config = self.load('containers = []\n[reboot]\nreboot_packages = []')
        self.assertEqual(config.containers, ())
        self.assertEqual(config.reboot.reboot_packages, ())
        config = self.load('[reboot]\nkernel_package = "linux-test"')
        self.assertEqual(config.reboot.reboot_packages, ("linux-test", "systemd", "glibc"))

    def test_missing_default_uses_builtins_but_explicit_missing_is_an_error(self):
        with patch("pacwarden.config.DEFAULT_CONFIG_PATH", self.path):
            self.assertEqual(load_config(), Config())
            with self.assertRaisesRegex(ConfigError, "file not found.*config.example.toml"):
                load_config(self.path)
            self.path.write_text("apt_autoremove = false", encoding="utf-8")
            self.assertFalse(load_config().apt_autoremove)

    def test_invalid_toml_does_not_echo_input(self):
        with self.assertRaisesRegex(ConfigError, "invalid TOML syntax at line 2") as caught:
            self.load('"synthetic-token" = "abc"\n"synthetic-token" = "abc"\n')
        self.assertIn(str(self.path), str(caught.exception))
        self.assertNotIn("synthetic-token", str(caught.exception))

    def test_invalid_encoding_and_unreadable_path(self):
        self.path.write_bytes(b"\xff")
        with self.assertRaisesRegex(ConfigError, "UTF-8"):
            load_config(self.path)
        with self.assertRaisesRegex(ConfigError, "cannot read configuration"):
            load_config(self.path.parent)

    def test_invalid_types_and_values(self):
        cases = (
            ('apt_autoremove = "true"', "apt_autoremove"),
            ('command_timeout_seconds = true', "command_timeout_seconds"),
            ('command_timeout_seconds = -1', "command_timeout_seconds"),
            ('command_timeout_seconds = 9999999999', "command_timeout_seconds"),
            ('state_dir = "relative"', "state_dir"),
            ('state_dir = 42', "state_dir"),
            ('containers = "arch"', "containers must be an array"),
            ('containers = [true]', "containers entry 1 must be a TOML table"),
            ('containers = [{ distro = "arch" }]', "containers entry 1.name"),
            ('containers = [{ name = "--option", distro = "arch" }]', "containers entry 1.name"),
            ('containers = [{ name = "one", distro = "debian" }]', "containers entry 1.distro"),
            ('containers = [{ name = "one" }]', "containers entry 1.distro"),
            ('containers = [{ name = "x", distro = "arch" }, { name = "x", distro = "ubuntu" }]',
             "containers entry 2.name must be unique"),
            ('reboot = false', "reboot must be a TOML table"),
            ('[reboot]\nauto_reboot = 1', "reboot.auto_reboot"),
            ('[reboot]\nkernel_package = "--linux"', "reboot.kernel_package"),
            ('[reboot]\nreboot_packages = "linux"', "reboot.reboot_packages"),
            ('[reboot]\nreboot_packages = ["linux", 1]', "reboot.reboot_packages entry 2"),
            ('[reboot]\nreboot_packages = ["linux", "linux"]', "unique package names"),
            ('[services]\nrestart_nginx_after_host_updates = 1', "services.restart_nginx_after_host_updates"),
            ('[notifications]\nrebooting = "true"', "notifications.rebooting"),
            ('[notifications.ntfy]\ntimeout_seconds = 0', "notifications.ntfy.timeout_seconds"),
            ('[notifications.ntfy]\ntimeout_seconds = 0.5', "notifications.ntfy.timeout_seconds"),
            ('[notifications.ntfy]\nurl = "https:///topic"', "notifications.ntfy.url"),
            ('[notifications.ntfy]\nurl = "ftp://ntfy.example.invalid/topic"', "notifications.ntfy.url"),
            ('[notifications.ntfy]\nurl = "https://ntfy.example.invalid:70000/topic"', "notifications.ntfy.url"),
            ('[notifications.ntfy]\nurl_env = "${PACWARDEN_URL}"', "notifications.ntfy.url_env"),
            ('[notifications.ntfy]\nurl_env = "PACWARDEN_URL"\nurl = "https://ntfy.example.invalid"',
             "Set either notifications.ntfy.url or notifications.ntfy.url_env"),
        )
        for contents, message in cases:
            with self.subTest(contents=contents):
                with self.assertRaises(ConfigError) as caught:
                    self.load(contents)
                self.assertIn(message, str(caught.exception))

    def test_unknown_keys_in_every_table(self):
        cases = (
            ('typo = true', "Configuration"),
            ('[reboot]\ntypo = true', "reboot"),
            ('[services]\ntypo = true', "services"),
            ('[notifications]\ntypo = true', "notifications"),
            ('[notifications.ntfy]\n"https://ntfy.example.invalid/synthetic-token" = true',
             "notifications.ntfy"),
            ('[[containers]]\nname = "x"\ndistro = "arch"\ntypo = true', "containers entry 1"),
        )
        for contents, location in cases:
            with self.subTest(location=location):
                with self.assertRaises(ConfigError) as caught:
                    self.load(contents)
                self.assertIn(f"{location} has an unknown key. Allowed keys:", str(caught.exception))
                self.assertNotIn("synthetic-token", str(caught.exception))

    def test_notification_category_defaults_and_overrides(self):
        defaults = {
            "success_no_updates": False, "success_with_updates": True, "update_failed": True,
            "reboot_required": True, "rebooting": True, "script_crashed": True,
            "service_restarted": False, "service_restart_failed": True,
        }
        switches = [f"{name} = {str(not value).lower()}" for name, value in defaults.items()]
        default_config = self.load("")
        overrides = self.load("[notifications]\n" + "\n".join(switches))
        for name, default in defaults.items():
            with self.subTest(name=name):
                self.assertIs(getattr(default_config.notifications, name), default)
                self.assertIs(getattr(overrides.notifications, name), not default)

    def test_environment_url_resolves_without_exposing_secrets(self):
        contents = '[notifications.ntfy]\nurl_env = "TEST_NTFY_URL"'
        url = "https://ntfy.example.invalid/synthetic-token"
        with patch.dict(os.environ, {"TEST_NTFY_URL": url}):
            self.assertEqual(self.load(contents).notifications.ntfy.url, url)
        with patch.dict(os.environ, {"TEST_NTFY_URL": "invalid-synthetic-token"}):
            with self.assertRaisesRegex(ConfigError, "variable named by notifications.ntfy.url_env") as caught:
                self.load(contents)
            self.assertNotIn("synthetic-token", str(caught.exception))
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ConfigError, "Set the variable named by notifications.ntfy.url_env"):
                self.load(contents)
            self.assertFalse(self.load(contents, disable_notifications=True).notifications.enabled)
            self.assertFalse(self.load("[notifications]\nenabled = false\n" + contents).notifications.enabled)

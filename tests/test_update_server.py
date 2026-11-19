import errno
import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from textwrap import dedent
from unittest.mock import Mock, patch

from pacwarden.cli import main, validate_environment


PACMAN_QUERY = ["pacman", "-Q"]
PACMAN_UPGRADE = ["pacman", "-Syu", "--noconfirm"]
KERNEL_QUERY = ["pacman", "-Q", "linux"]
NGINX_ACTIVE = ["systemctl", "is-active", "--quiet", "nginx"]
REBOOT = ["systemctl", "reboot"]
APT_PREFIX = ["env", "DEBIAN_FRONTEND=noninteractive", "apt-get"]
DPKG_QUERY = ["dpkg-query", "-W", "-f=${db:Status-Status} ${binary:Package} ${Version}\\n"]
KERNEL_MATCH = (KERNEL_QUERY, 0, "linux 6.12.1.arch1-1\n")


class CliTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.stdout = self.enterContext(redirect_stdout(io.StringIO()))
        self.stderr = self.enterContext(redirect_stderr(io.StringIO()))
        self.validate = self.enterContext(patch("pacwarden.cli.validate_environment"))
        self.enterContext(patch("pacwarden.cli.socket.gethostname", return_value="test-host"))
        self.enterContext(patch("pacwarden.system.reboot.get_boot_id", return_value="test-boot"))
        self.enterContext(patch(
            "pacwarden.system.arch.os.uname", return_value=Mock(release="6.12.1-arch1-1")
        ))
        self.subprocess_run = self.enterContext(patch(
            "subprocess.run", side_effect=AssertionError("Unexpected external command")
        ))
        self.urlopen = self.enterContext(patch(
            "urllib.request.urlopen", side_effect=AssertionError("Unexpected network call")
        ))

    def write_config(self, contents, name="config.toml"):
        path = self.directory / name
        state_dir = json.dumps(str(self.directory))
        path.write_text(f"state_dir = {state_dir}\n" + dedent(contents), encoding="utf-8")
        return str(path)

    def expect_commands(self, *responses):
        remaining = iter(responses)

        def run(command, *, text, capture_output, check, timeout):
            expected, code, stdout = next(remaining)
            self.assertEqual(command, expected)
            if isinstance(code, BaseException):
                raise code
            if code and check:
                raise subprocess.CalledProcessError(code, command)
            return subprocess.CompletedProcess(command, code, stdout=stdout)

        self.subprocess_run.side_effect = run
        self.addCleanup(lambda: self.assertEqual(
            [call.args[0] for call in self.subprocess_run.call_args_list],
            [response[0] for response in responses],
        ))

    def test_explicit_path_and_cli_flags_override_config(self):
        path = self.write_config("""
            apt_autoremove = true
            [[containers]]
            name = "ubuntu"
            distro = "ubuntu"
            [services]
            restart_nginx_after_host_updates = false
            [notifications.ntfy]
            url_env = "MISSING_NTFY_URL"
        """, name="custom.toml")
        default_path = self.write_config("malformed TOML")
        incus = ["incus", "exec", "ubuntu", "--"]
        dpkg = [*incus, *DPKG_QUERY]
        self.expect_commands(
            (PACMAN_QUERY, 0, "glibc 1\n"),
            (PACMAN_UPGRADE, 0, ""),
            (PACMAN_QUERY, 0, "glibc 2\n"),
            KERNEL_MATCH,
            (["incus", "info", "ubuntu"], 0, "Status: RUNNING\n"),
            (dpkg, 0, "installed curl:amd64 1\n"),
            ([*incus, *APT_PREFIX, "update"], 0, ""),
            ([*incus, *APT_PREFIX, "dist-upgrade", "-y"], 0, ""),
            (dpkg, 0, "installed curl:amd64 2\n"),
        )
        with patch("pacwarden.config.DEFAULT_CONFIG_PATH", Path(default_path)):
            self.assertEqual(main([
                "--config", path, "--no-reboot", "--no-apt-autoremove", "--no-notifications",
            ]), 0)
        self.assertIn("skipped by --no-reboot", self.stdout.getvalue())
        self.assertIn("Total packages updated: 2", self.stdout.getvalue())
        self.validate.assert_called_once_with(needs_systemctl=False)
        self.urlopen.assert_not_called()

    def test_config_can_disable_behaviors_without_cli_flags(self):
        path = self.write_config("""
            apt_autoremove = false
            [[containers]]
            name = "ubuntu"
            distro = "ubuntu"
            [reboot]
            auto_reboot = false
            [services]
            restart_nginx_after_host_updates = false
            [notifications]
            enabled = false
        """)
        incus = ["incus", "exec", "ubuntu", "--"]
        dpkg = [*incus, *DPKG_QUERY]
        self.expect_commands(
            (PACMAN_QUERY, 0, "glibc 1\n"),
            (PACMAN_UPGRADE, 0, ""),
            (PACMAN_QUERY, 0, "glibc 2\n"),
            KERNEL_MATCH,
            (["incus", "info", "ubuntu"], 0, "Status: RUNNING\n"),
            (dpkg, 0, "installed curl:amd64 1\n"),
            ([*incus, *APT_PREFIX, "update"], 0, ""),
            ([*incus, *APT_PREFIX, "dist-upgrade", "-y"], 0, ""),
            (dpkg, 0, "installed curl:amd64 2\n"),
        )
        self.assertEqual(main(["--config", path]), 0)
        self.assertIn("skipped because reboot.auto_reboot is disabled", self.stdout.getvalue())
        self.urlopen.assert_not_called()

    def test_invalid_config_exits_before_updating(self):
        path = self.write_config("""
            [notifications.ntfy]
            url = "not-a-url-with-synthetic-token"
        """)
        self.assertEqual(main(["--config", path]), 2)
        self.validate.assert_not_called()
        self.subprocess_run.assert_not_called()
        self.assertIn("notifications.ntfy.url", self.stderr.getvalue())
        self.assertNotIn("synthetic-token", self.stderr.getvalue())

    def test_omitted_containers_updates_only_the_host(self):
        path = self.write_config("")
        self.expect_commands(
            (PACMAN_QUERY, 0, "glibc 1\n"),
            (PACMAN_UPGRADE, 0, ""),
            (PACMAN_QUERY, 0, "glibc 1\n"),
            (KERNEL_QUERY, 0, "linux 6.12.1.arch1-1\n"),
        )
        self.assertEqual(main(["--config", path, "--no-notifications"]), 0)
        self.assertIn("Updating 0 configured Incus container(s)", self.stdout.getvalue())

    def test_host_validation_checks_only_enabled_capabilities(self):
        self.validate.side_effect = validate_environment
        path = self.write_config("""
            [services]
            restart_nginx_after_host_updates = false
        """)
        self.expect_commands(
            (PACMAN_QUERY, 0, "glibc 1\n"),
            (PACMAN_UPGRADE, 0, ""),
            (PACMAN_QUERY, 0, "glibc 1\n"),
            KERNEL_MATCH,
        )
        with (
            patch("pacwarden.cli.os.geteuid", return_value=0),
            patch("pacwarden.cli.shutil.which", side_effect={"pacman": "/usr/bin/pacman"}.get) as which,
        ):
            self.assertEqual(main(["--config", path, "--no-reboot", "--no-notifications"]), 0)
            which.assert_called_once_with("pacman")
            self.assertEqual(main(["--config", path, "--no-notifications"]), 1)
        self.assertIn("Required command not found: systemctl", self.stderr.getvalue())

    def test_missing_incus_counts_as_container_failure_and_host_still_reboots(self):
        self.validate.side_effect = validate_environment
        path = self.write_config("""
            [[containers]]
            name = "guest"
            distro = "arch"
            [services]
            restart_nginx_after_host_updates = false
            [notifications.ntfy]
            url = "https://ntfy.example.invalid/synthetic-token"
        """)
        self.expect_commands(
            (PACMAN_QUERY, 0, "glibc 1\n"),
            (PACMAN_UPGRADE, 0, ""),
            (PACMAN_QUERY, 0, "glibc 2\n"),
            KERNEL_MATCH,
            (["incus", "info", "guest"], FileNotFoundError(errno.ENOENT, "missing"), ""),
            (REBOOT, 0, ""),
        )
        self.urlopen.side_effect = RuntimeError("synthetic-token")
        available = {"pacman": "/usr/bin/pacman", "systemctl": "/usr/bin/systemctl"}
        with (
            patch("pacwarden.cli.os.geteuid", return_value=0),
            patch("pacwarden.cli.shutil.which", side_effect=available.get),
        ):
            self.assertEqual(main(["--config", path]), 1)
        self.assertIn("incus info guest: No such file", self.stderr.getvalue())
        self.assertIn("Failed to send notification", self.stdout.getvalue())
        self.assertNotIn("synthetic-token", self.stdout.getvalue() + self.stderr.getvalue())

    def test_host_and_nginx_run_first_and_container_failure_does_not_block_reboot(self):
        path = self.write_config("""
            [[containers]]
            name = "failed"
            distro = "arch"
            [[containers]]
            name = "working"
            distro = "arch"
        """)
        working = ["incus", "exec", "working", "--"]
        self.expect_commands(
            (PACMAN_QUERY, 0, "glibc 1\n"),
            (PACMAN_UPGRADE, 0, ""),
            (PACMAN_QUERY, 0, "glibc 2\n"),
            KERNEL_MATCH,
            (NGINX_ACTIVE, 0, ""),
            (["nginx", "-t"], 0, ""),
            (["systemctl", "restart", "nginx"], 0, ""),
            (NGINX_ACTIVE, 0, ""),
            (["incus", "info", "failed"], 1, ""),
            (["incus", "info", "working"], 0, "Status: RUNNING\n"),
            ([*working, *PACMAN_QUERY], 0, "curl 1\n"),
            ([*working, *PACMAN_UPGRADE], 0, ""),
            ([*working, *PACMAN_QUERY], 0, "curl 2\n"),
            (REBOOT, 0, ""),
        )
        self.assertEqual(main(["--config", path, "--no-notifications"]), 1)
        state = json.loads((self.directory / "reboot-required.json").read_text())
        self.assertEqual(state, {"boot_id": "test-boot", "reasons": ["glibc: 1 -> 2"]})
        self.assertIn("Total packages updated: 2", self.stdout.getvalue())

    def test_failed_host_upgrade_still_updates_containers_and_blocks_reboot(self):
        path = self.write_config("""
            [[containers]]
            name = "working"
            distro = "arch"
        """)
        working = ["incus", "exec", "working", "--"]
        self.expect_commands(
            (PACMAN_QUERY, 0, "glibc 1\n"),
            (PACMAN_UPGRADE, 1, ""),
            (PACMAN_QUERY, 0, "glibc 2\n"),
            (KERNEL_QUERY, 0, "linux 6.12.2.arch1-1\n"),
            (["incus", "info", "working"], 0, "Status: RUNNING\n"),
            ([*working, *PACMAN_QUERY], 0, "curl 1\n"),
            ([*working, *PACMAN_UPGRADE], 0, ""),
            ([*working, *PACMAN_QUERY], 0, "curl 2\n"),
        )
        self.assertEqual(main(["--config", path, "--no-notifications"]), 1)
        self.assertIn("skipped because host update or service verification failed", self.stdout.getvalue())
        state = json.loads((self.directory / "reboot-required.json").read_text())
        self.assertEqual(state["reasons"], ["glibc: 1 -> 2", "kernel: 6.12.1-arch1-1 -> 6.12.2-arch1-1"])
        self.assertIn("1 package updates observed (pacman -Syu --noconfirm exited with status 1)", self.stdout.getvalue())

    def test_container_upgrade_and_snapshot_failure_do_not_block_reboot(self):
        path = self.write_config("""
            [[containers]]
            name = "guest"
            distro = "arch"
            [services]
            restart_nginx_after_host_updates = false
        """)
        guest = ["incus", "exec", "guest", "--"]
        self.expect_commands(
            (PACMAN_QUERY, 0, "glibc 1\n"),
            (PACMAN_UPGRADE, 0, ""),
            (PACMAN_QUERY, 0, "glibc 2\n"),
            KERNEL_MATCH,
            (["incus", "info", "guest"], 0, "Status: RUNNING\n"),
            ([*guest, *PACMAN_QUERY], 0, "curl 1\n"),
            ([*guest, *PACMAN_UPGRADE], -15, ""),
            ([*guest, *PACMAN_QUERY], 1, ""),
            (REBOOT, 0, ""),
        )
        self.assertEqual(main(["--config", path, "--no-notifications"]), 1)
        self.assertIn("exited with status -15", self.stderr.getvalue())
        self.assertIn("Total packages updated: 1 (known total)", self.stdout.getvalue())

    def test_reboot_command_failure_leaves_reason_and_next_hour_can_retry(self):
        path = self.write_config("""
            [services]
            restart_nginx_after_host_updates = false
        """)
        self.expect_commands(
            (PACMAN_QUERY, 0, "glibc 1\n"),
            (PACMAN_UPGRADE, 0, ""),
            (PACMAN_QUERY, 0, "glibc 2\n"),
            KERNEL_MATCH,
            (REBOOT, 1, ""),
            (PACMAN_QUERY, 0, "glibc 2\n"),
            (PACMAN_UPGRADE, 0, ""),
            (PACMAN_QUERY, 0, "glibc 2\n"),
            KERNEL_MATCH,
            (REBOOT, 0, ""),
        )
        flags = ["--config", path, "--no-notifications"]
        self.assertEqual(main(flags), 1)
        state = json.loads((self.directory / "reboot-required.json").read_text())
        self.assertEqual(state["reasons"], ["glibc: 1 -> 2"])
        self.assertEqual(main(flags), 0)

    def test_nginx_post_restart_verification_failure_skips_reboot(self):
        path = self.write_config("containers = []")
        self.expect_commands(
            (PACMAN_QUERY, 0, "glibc 1\n"),
            (PACMAN_UPGRADE, 0, ""),
            (PACMAN_QUERY, 0, "glibc 2\n"),
            KERNEL_MATCH,
            (NGINX_ACTIVE, 0, ""),
            (["nginx", "-t"], 0, ""),
            (["systemctl", "restart", "nginx"], 0, ""),
            (NGINX_ACTIVE, 3, ""),
        )
        self.assertEqual(main(["--config", path, "--no-notifications"]), 1)
        self.assertIn("NGINX validation/restart failed", self.stderr.getvalue())
        self.assertIn("skipped because host update or service verification failed", self.stdout.getvalue())
        state = json.loads((self.directory / "reboot-required.json").read_text())
        self.assertEqual(state["reasons"], ["glibc: 1 -> 2"])

    def test_nginx_status_command_failure_skips_reboot(self):
        path = self.write_config("")
        self.expect_commands(
            (PACMAN_QUERY, 0, "glibc 1\n"),
            (PACMAN_UPGRADE, 0, ""),
            (PACMAN_QUERY, 0, "glibc 2\n"),
            KERNEL_MATCH,
            (NGINX_ACTIVE, 1, ""),
        )
        self.assertEqual(main(["--config", path, "--no-notifications"]), 1)
        self.assertIn("NGINX validation/restart failed: systemctl is-active exited with status 1", self.stderr.getvalue())
        self.assertIn("skipped because host update or service verification failed", self.stdout.getvalue())

    def test_failed_initial_snapshot_prevents_upgrade_and_still_checks_kernel(self):
        path = self.write_config("")
        self.expect_commands(
            (PACMAN_QUERY, 1, ""),
            (KERNEL_QUERY, 0, "linux 6.12.2.arch1-1\n"),
        )
        self.assertEqual(main(["--config", path, "--no-notifications"]), 1)
        self.assertIn("pacman -Q exited with status 1", self.stderr.getvalue())
        self.assertIn("Reboot required: yes", self.stdout.getvalue())
        state = json.loads((self.directory / "reboot-required.json").read_text())
        self.assertEqual(state["reasons"], ["kernel: 6.12.1-arch1-1 -> 6.12.2-arch1-1"])
        self.assertNotIn("pending_versions", state)

    def test_timeout_and_failed_snapshot_are_reconciled_on_next_successful_run(self):
        path = self.write_config("""
            [services]
            restart_nginx_after_host_updates = false
        """)
        self.expect_commands(
            (PACMAN_QUERY, 0, "glibc 1\n"),
            (PACMAN_UPGRADE, subprocess.TimeoutExpired(PACMAN_UPGRADE, 3600), ""),
            (PACMAN_QUERY, 2, ""),
            KERNEL_MATCH,
            (PACMAN_QUERY, 0, "glibc 2\n"),
            (PACMAN_UPGRADE, 0, ""),
            (PACMAN_QUERY, 0, "glibc 2\n"),
            KERNEL_MATCH,
            (REBOOT, 0, ""),
        )
        flags = ["--config", path, "--no-notifications"]
        self.assertEqual(main(flags), 1)
        self.assertIn(
            "pacman -Syu --noconfirm timed out after 3600 seconds. "
            "Package changes unknown (post-update snapshot failed: pacman -Q exited with status 2)",
            self.stderr.getvalue(),
        )
        self.assertIn("Reboot required: unknown", self.stdout.getvalue())
        self.assertIn("Total packages updated: 0 (known total)", self.stdout.getvalue())
        state_path = self.directory / "reboot-required.json"
        state = json.loads(state_path.read_text())
        self.assertEqual(state["pending_versions"], {"glibc": "1", "systemd": None})
        self.assertEqual(main(flags), 0)
        self.assertEqual(json.loads(state_path.read_text()), {
            "boot_id": "test-boot", "reasons": ["glibc: 1 -> 2"],
        })

    def test_successful_command_with_unreadable_after_snapshot_is_incomplete(self):
        path = self.write_config("")
        self.expect_commands(
            (PACMAN_QUERY, 0, "glibc 1\n"),
            (PACMAN_UPGRADE, 0, ""),
            (PACMAN_QUERY, 0, "malformed-version-list\n"),
            KERNEL_MATCH,
        )
        self.assertEqual(main(["--config", path, "--no-notifications"]), 1)
        self.assertIn("Package changes unknown", self.stderr.getvalue())
        self.assertIn("Reboot required: unknown", self.stdout.getvalue())

    def test_saved_baseline_recovers_a_change_after_process_exits_mid_upgrade(self):
        path = self.write_config("""
            [services]
            restart_nginx_after_host_updates = false
        """)
        self.expect_commands(
            (PACMAN_QUERY, 0, "glibc 1\n"),
            (PACMAN_UPGRADE, SystemExit(), ""),
            (PACMAN_QUERY, 0, "glibc 2\n"),
            (PACMAN_UPGRADE, 0, ""),
            (PACMAN_QUERY, 0, "glibc 1\n"),
            KERNEL_MATCH,
        )
        flags = ["--config", path, "--no-notifications", "--no-reboot"]
        with self.assertRaises(SystemExit):
            main(flags)
        self.assertEqual(main(flags), 0)
        state = json.loads((self.directory / "reboot-required.json").read_text())
        self.assertEqual(state["reasons"], ["glibc: 1 -> 2", "glibc: 2 -> 1"])

    def test_no_notifications_also_suppresses_crash_alert(self):
        path = self.write_config("""
            [notifications.ntfy]
            url = "https://ntfy.example.invalid/synthetic-token"
        """)
        self.validate.side_effect = RuntimeError("synthetic-token")
        self.assertEqual(main(["--config", path, "--no-notifications"]), 1)
        self.assertIn("Update script crashed", self.stderr.getvalue())
        self.assertNotIn("synthetic-token", self.stderr.getvalue())
        self.urlopen.assert_not_called()

    def test_unexpected_crash_alert_omits_exception_data(self):
        path = self.write_config("""
            [notifications.ntfy]
            url = "https://ntfy.example.invalid/synthetic-token"
        """)
        self.validate.side_effect = RuntimeError("synthetic-token")
        self.urlopen.side_effect = None
        self.assertEqual(main(["--config", path]), 1)
        request, = self.urlopen.call_args.args
        self.assertEqual(request.get_header("Priority"), "urgent")
        self.assertEqual(request.get_header("Tags"), "rotating_light")
        self.assertNotIn(b"synthetic-token", request.data)
        self.assertNotIn("synthetic-token", self.stderr.getvalue())

    def test_crash_notification_failure_does_not_surface_transport_details(self):
        path = self.write_config('''
            [notifications.ntfy]
            url = "https://ntfy.example.invalid/synthetic-token"
        ''')
        self.validate.side_effect = RuntimeError("synthetic-token")
        self.urlopen.side_effect = RuntimeError("synthetic-token")
        self.assertEqual(main(["--config", path]), 1)
        self.assertIn("Failed to send crash notification", self.stdout.getvalue())
        self.assertNotIn("synthetic-token", self.stdout.getvalue() + self.stderr.getvalue())

    def test_manual_reboot_notification_fires_once_and_omits_reasons(self):
        path = self.write_config('''
            containers = []
            [services]
            restart_nginx_after_host_updates = false
            [notifications.ntfy]
            url = "https://ntfy.example.invalid/synthetic-token"
        ''')
        self.expect_commands(
            (PACMAN_QUERY, 0, "glibc 1\n"),
            (PACMAN_UPGRADE, 0, ""),
            (PACMAN_QUERY, 0, "glibc 2\n"),
            (KERNEL_QUERY, 0, "linux 6.12.1.arch1-1\n"),
            (PACMAN_QUERY, 0, "glibc 2\n"),
            (PACMAN_UPGRADE, 0, ""),
            (PACMAN_QUERY, 0, "glibc 2\n"),
            (KERNEL_QUERY, 0, "linux 6.12.1.arch1-1\n"),
        )
        self.urlopen.side_effect = None
        for _ in range(2):
            self.assertEqual(main(["--config", path, "--no-reboot"]), 0)

        requests = [call.args[0] for call in self.urlopen.call_args_list]
        self.assertEqual([request.get_header("Priority") for request in requests], ["low", "high"])
        self.assertEqual([request.get_header("Title") for request in requests], [
            "test-host: update successful", "test-host: reboot required",
        ])
        self.assertIn(b"skipped by --no-reboot", requests[1].data)
        self.assertTrue(all(b"glibc" not in request.data for request in requests))
        self.assertIn("glibc: 1 -> 2", self.stdout.getvalue())

    def test_unexpected_kernel_output_reports_unknown_without_reboot(self):
        path = self.write_config("containers = []")
        self.expect_commands(
            (PACMAN_QUERY, 0, "glibc 1\n"),
            (PACMAN_UPGRADE, 0, ""),
            (PACMAN_QUERY, 0, "glibc 1\n"),
            (KERNEL_QUERY, 0, "synthetic-token\n"),
        )
        self.assertEqual(main(["--config", path, "--no-notifications"]), 1)
        self.assertIn("Unexpected pacman output for linux", self.stdout.getvalue())
        self.assertIn("Reboot required: unknown", self.stdout.getvalue())
        self.assertNotIn("synthetic-token", self.stdout.getvalue() + self.stderr.getvalue())

    def test_interrupted_upgrade_records_changes_and_exits_130(self):
        path = self.write_config("")
        self.expect_commands(
            (PACMAN_QUERY, 0, "systemd 1\n"),
            (PACMAN_UPGRADE, KeyboardInterrupt(), ""),
            (PACMAN_QUERY, 0, "systemd 2\n"),
            KERNEL_MATCH,
        )
        self.assertEqual(main(["--config", path]), 130)
        state = json.loads((self.directory / "reboot-required.json").read_text())
        self.assertEqual(state["reasons"], ["systemd: 1 -> 2"])
        self.assertIn("Update interrupted", self.stderr.getvalue())
        self.urlopen.assert_not_called()

    def test_interruption_during_after_snapshot_keeps_baseline_and_exits_130(self):
        path = self.write_config("")
        self.expect_commands(
            (PACMAN_QUERY, 0, "glibc 1\n"),
            (PACMAN_UPGRADE, 0, ""),
            (PACMAN_QUERY, KeyboardInterrupt(), ""),
            KERNEL_MATCH,
        )
        self.assertEqual(main(["--config", path]), 130)
        state = json.loads((self.directory / "reboot-required.json").read_text())
        self.assertEqual(state["pending_versions"]["glibc"], "1")
        self.assertIn("Package changes unknown", self.stderr.getvalue())
        self.urlopen.assert_not_called()

    def test_interruption_exits_130_without_reboot_or_notification(self):
        path = self.write_config("containers = []")
        self.subprocess_run.side_effect = KeyboardInterrupt
        self.assertEqual(main(["--config", path]), 130)
        self.subprocess_run.assert_called_once()
        self.assertIn("Update interrupted", self.stderr.getvalue())
        self.urlopen.assert_not_called()

import errno
import io
import json
import stat
import subprocess
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, call, create_autospec, patch

from pacwarden.config import NotificationsConfig
from pacwarden.execution.base import CommandError
from pacwarden.execution.incus import IncusExecutor, ensure_container_running
from pacwarden.execution.local import LocalExecutor
from pacwarden.models import UpdateResult, UpdateTarget
from pacwarden.notifications.base import EventType, NotificationEvent, Notifier
from pacwarden.notifications.ntfy import NtfyProvider
from pacwarden.orchestrator import update_target
from pacwarden.packages.apt import AptPackageManager
from pacwarden.packages.pacman import PacmanPackageManager
from pacwarden.reporting import DiagnosticError
from pacwarden.state import RebootState, RebootStateStore
from pacwarden.system.arch import get_kernel_versions, normalize_kernel_version
from pacwarden.system.reboot import HostReboot, RebootAction, RebootStatus
from pacwarden.system.services import NginxService


class ExecutorTests(unittest.TestCase):
    def setUp(self):
        self.stdout = self.enterContext(redirect_stdout(io.StringIO()))
        self.run_subprocess = self.enterContext(patch("subprocess.run"))
        self.local = LocalExecutor(timeout_seconds=25)

    def test_local_preserves_arguments_streams_and_unchecked_status(self):
        completed = subprocess.CompletedProcess([], 3, stdout="output", stderr="error")
        self.run_subprocess.return_value = completed
        result = self.local.run(("fake-command", "two words"), capture_output=True, check=False)
        self.assertIs(result, completed)
        self.run_subprocess.assert_called_once_with(
            ["fake-command", "two words"], text=True, capture_output=True, check=False, timeout=25,
        )
        self.assertIn("Running: fake-command 'two words'", self.stdout.getvalue())

    def test_incus_wraps_argv_and_uses_local_execution_options(self):
        executor = IncusExecutor("remote:guest", self.local)
        result = executor.run(["fake-command", "; reboot"], capture_output=True, check=False)
        self.assertIs(result, self.run_subprocess.return_value)
        self.run_subprocess.assert_called_once_with(
            ["incus", "exec", "remote:guest", "--", "fake-command", "; reboot"],
            text=True, capture_output=True, check=False, timeout=25,
        )

    def test_checked_failures_are_safe_and_include_exit_status(self):
        command = ["fake-command", "synthetic-token"]
        self.run_subprocess.side_effect = subprocess.CalledProcessError(
            7, command, stderr="synthetic-token"
        )
        with self.assertRaisesRegex(CommandError, "exited with status 7") as caught:
            IncusExecutor("guest", self.local).run(command, sensitive=True)
        self.assertEqual(caught.exception.returncode, 7)
        self.assertIsNone(caught.exception.__context__)
        self.assertNotIn("synthetic-token", str(caught.exception))
        self.assertNotIn("synthetic-token", self.stdout.getvalue())
        self.assertTrue(self.run_subprocess.call_args.kwargs["capture_output"])

    def test_timeout_and_launch_failure_have_safe_diagnostics(self):
        executor = IncusExecutor("guest", self.local)
        self.run_subprocess.side_effect = subprocess.TimeoutExpired(
            ["synthetic-token"], 25, output="synthetic-token"
        )
        with self.assertRaisesRegex(CommandError, "incus exec guest -- missing timed out after 25 seconds"):
            executor.run(["missing"])
        self.run_subprocess.side_effect = FileNotFoundError(errno.ENOENT, "missing", "synthetic-token")
        with self.assertRaisesRegex(CommandError, "incus exec guest -- missing: No such file") as caught:
            executor.run(["missing"])
        self.assertNotIn("synthetic-token", str(caught.exception))

    def test_incus_preflight_leaves_stopped_container_untouched(self):
        self.run_subprocess.return_value = subprocess.CompletedProcess([], 0, stdout="Status: stopped\n")
        with self.assertRaisesRegex(DiagnosticError, "container status is 'STOPPED'"):
            ensure_container_running(self.local, "guest")
        self.run_subprocess.assert_called_once_with(
            ["incus", "info", "guest"], text=True, capture_output=True, check=False, timeout=25,
        )


class PackageManagerTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(redirect_stdout(io.StringIO()))
        self.enterContext(patch("pacwarden.orchestrator.error"))

    def test_pacman_uses_same_workflow_with_local_and_incus_executors(self):
        manager = PacmanPackageManager()
        for executor_class in (LocalExecutor, IncusExecutor):
            with self.subTest(executor=executor_class.__name__):
                executor = create_autospec(executor_class, instance=True)
                executor.run.side_effect = [
                    Mock(stdout="curl 1\nremoved 1\nunchanged 1\n"),
                    Mock(),
                    Mock(stdout="curl 2\nadded 1\nunchanged 1\n"),
                ]
                result, changes = update_target(UpdateTarget("arch", executor, manager))
                self.assertEqual(result, UpdateResult("arch", True, 2))
                self.assertNotIn("removed", changes.after)
                self.assertEqual(executor.run.call_args_list, [
                    call(["pacman", "-Q"], capture_output=True),
                    call(["pacman", "-Syu", "--noconfirm"]),
                    call(["pacman", "-Q"], capture_output=True),
                ])

    def test_apt_uses_incus_executor_and_runs_autoremove_when_enabled(self):
        executor = create_autospec(IncusExecutor, instance=True)
        executor.run.side_effect = [
            Mock(stdout="installed curl:amd64 1\nconfig-files left-behind 1\n"),
            Mock(), Mock(), Mock(), Mock(stdout="installed curl:amd64 2\n"),
        ]
        result, changes = update_target(UpdateTarget("ubuntu", executor, AptPackageManager(autoremove=True)))
        self.assertEqual(result, UpdateResult("ubuntu", True, 1))
        self.assertEqual(changes.after, {"curl:amd64": "2"})
        query = ["dpkg-query", "-W", "-f=${db:Status-Status} ${binary:Package} ${Version}\\n"]
        apt = ["env", "DEBIAN_FRONTEND=noninteractive", "apt-get"]
        self.assertEqual(executor.run.call_args_list, [
            call(query, capture_output=True),
            call([*apt, "update"]),
            call([*apt, "dist-upgrade", "-y"]),
            call([*apt, "autoremove", "-y"]),
            call(query, capture_output=True),
        ])

    def test_apt_failure_at_each_stage_still_collects_package_observations(self):
        for stage, status, version in (
            ("update", "installed", "1"), ("dist-upgrade", "unpacked", "2"), ("autoremove", "installed", "2"),
        ):
            with self.subTest(stage=stage):
                executor = create_autospec(IncusExecutor, instance=True)
                failure = CommandError(f"apt-get {stage} failed")
                stages = ["update", "dist-upgrade", "autoremove"]
                executor.run.side_effect = [
                    Mock(stdout="installed curl:amd64 1\n"),
                    *[Mock() for _ in stages[:stages.index(stage)]], failure,
                    Mock(stdout=f"{status} curl:amd64 {version}\n"),
                ]
                result, changes = update_target(UpdateTarget("guest", executor, AptPackageManager(autoremove=True)))
                self.assertFalse(result.success)
                self.assertEqual(result.message, str(failure))
                self.assertEqual(result.packages_updated, int(version != "1"))
                self.assertEqual(changes.after, {"curl:amd64": version})
                self.assertEqual(executor.run.call_count, stages.index(stage) + 3)

    def test_malformed_or_duplicate_package_rows_fail_the_snapshot(self):
        executor = create_autospec(LocalExecutor, instance=True)
        manager = PacmanPackageManager()
        for output in ("glibc\n", "glibc 1 2\n", "glibc 1\nglibc 2\n"):
            with self.subTest(output=output):
                executor.run.return_value = Mock(stdout=output)
                with self.assertRaisesRegex(DiagnosticError, "installed package list format"):
                    manager.installed_versions(executor)


class ArchKernelTests(unittest.TestCase):
    def test_kernel_versions_are_normalized_before_comparison(self):
        with patch("pacwarden.system.arch.os.uname", return_value=Mock(release="6.12.1-arch1-1")):
            self.assertEqual(get_kernel_versions(lambda: "6.12.1.arch1-1"), ("6.12.1-arch1-1",) * 2)
        with self.assertRaisesRegex(DiagnosticError, "Unexpected Arch kernel version format"):
            normalize_kernel_version("invalid")


class RebootTests(unittest.TestCase):
    def setUp(self):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.state = directory / "reboot-required.json"
        self.boot_id = self.enterContext(patch("pacwarden.system.reboot.get_boot_id", return_value="a"))
        self.kernel = Mock(return_value=("running", "running"))
        self.store = RebootStateStore(directory)
        self.reboot = HostReboot(
            create_autospec(LocalExecutor, instance=True), self.store, ("glibc",), self.kernel, auto_reboot=True,
        )
        self.enterContext(self.store.locked())

    def test_existing_state_survives_current_boot_and_clears_after_new_boot(self):
        reasons = ("glibc: 1 -> 2",)
        self.state.write_text(json.dumps({"boot_id": "a", "reasons": reasons}), encoding="utf-8")
        self.assertEqual(self.reboot.evaluate(None), RebootStatus(reasons, False))
        self.boot_id.return_value = "b"
        self.assertEqual(self.reboot.evaluate(None), RebootStatus((), False))
        self.assertEqual(json.loads(self.state.read_text()), {"boot_id": "b", "reasons": []})
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o600)

    def test_kernel_mismatch_is_replaced_and_package_removal_is_tracked(self):
        self.kernel.return_value = ("running", "new")
        self.reboot.record_baseline({"glibc": "1"})
        removal = "glibc: 1 -> not installed"
        mismatch = "kernel: running -> new"
        self.assertEqual(self.reboot.evaluate({}), RebootStatus((removal, mismatch), True))
        self.assertEqual(self.reboot.evaluate(None), RebootStatus((removal, mismatch), False))
        self.kernel.return_value = ("running", "newer")
        self.assertEqual(self.reboot.evaluate(None), RebootStatus((
            removal, "kernel: running -> newer",
        ), True))
        self.kernel.return_value = ("running", "running")
        self.assertEqual(self.reboot.evaluate(None), RebootStatus((removal,), False))
        self.assertEqual(self.kernel.call_count, 4)

    def test_failed_kernel_check_keeps_previous_mismatch_and_saves_package_changes(self):
        old_reason = "kernel: running -> pending"
        self.store.write("a", RebootState(frozenset({old_reason})))
        self.reboot.record_baseline({"glibc": "1"})
        self.kernel.side_effect = DiagnosticError("query failed")
        status = self.reboot.evaluate({"glibc": "2"})
        self.assertEqual(status.reasons, ("glibc: 1 -> 2", old_reason))
        self.assertTrue(status.required)
        self.assertFalse(status.complete)
        self.assertEqual(status.check_errors, ("Could not compare kernel versions: query failed",))
        self.assertEqual(self.store.read("a"), RebootState(frozenset(status.reasons)))
        self.assertEqual(self.reboot.action(status, no_reboot=False, host_failed=False), RebootAction.CHECK_INCOMPLETE)

    def test_manual_reboot_discards_unresolved_baseline_but_detects_kernel_mismatch(self):
        self.reboot.record_baseline({"glibc": "1"})
        self.assertIsNone(self.reboot.evaluate(None).required)
        self.boot_id.return_value = "b"
        self.kernel.return_value = ("old", "new")
        self.assertFalse(self.reboot.record_baseline({"glibc": "2"}))
        self.assertEqual(self.reboot.evaluate({"glibc": "2"}), RebootStatus(("kernel: old -> new",), True))

    def test_failed_state_write_preserves_pending_baseline_and_blocks_automatic_reboot(self):
        self.reboot.record_baseline({"glibc": "1"})
        with patch("pacwarden.state.os.replace", side_effect=OSError):
            status = self.reboot.evaluate({"glibc": "2"})
        self.assertEqual(status.reasons, ("glibc: 1 -> 2",))
        self.assertEqual(status.check_errors, ("Could not save reboot state: OSError",))
        self.assertEqual(self.store.read("a"), RebootState(pending_versions={"glibc": "1"}))
        self.assertEqual(self.reboot.action(status, no_reboot=False, host_failed=False), RebootAction.CHECK_INCOMPLETE)
        self.assertEqual(sorted(p.name for p in self.store.directory.iterdir()), [
            "reboot-required.json", "reboot-required.lock",
        ])

    def test_corrupt_current_boot_state_is_rejected_and_preserved(self):
        corrupt = '{"boot_id": "a", "reasons": 42}'
        self.state.write_text(corrupt, encoding="utf-8")
        status = self.reboot.evaluate(None)
        self.assertIsNone(status.required)
        self.assertIn("Invalid persisted reboot reasons", status.check_errors[0])
        self.assertEqual(self.state.read_text(), corrupt)
        self.kernel.assert_not_called()


class ServiceTests(unittest.TestCase):
    def test_no_nginx_commands_for_failed_or_unchanged_host_or_disabled_restart(self):
        local = create_autospec(LocalExecutor, instance=True)
        nginx = NginxService(local, restart_after_host_updates=True)
        self.assertIsNone(nginx.restart_if_needed(UpdateResult("host", False, 1)))
        self.assertIsNone(nginx.restart_if_needed(UpdateResult("host", True, 0)))
        disabled = replace(nginx, restart_after_host_updates=False)
        self.assertIsNone(disabled.restart_if_needed(UpdateResult("host", True, 1)))
        local.run.assert_not_called()

    def test_nginx_is_left_inactive(self):
        local = create_autospec(LocalExecutor, instance=True)
        local.run.return_value = subprocess.CompletedProcess([], 3)
        with redirect_stdout(io.StringIO()):
            result = NginxService(local, restart_after_host_updates=True).restart_if_needed(
                UpdateResult("host", True, 1)
            )
        self.assertIsNone(result)
        local.run.assert_called_once_with(["systemctl", "is-active", "--quiet", "nginx"], check=False)

    def test_nginx_action_can_use_the_targets_executor(self):
        local = create_autospec(LocalExecutor, instance=True)
        local.run.return_value = subprocess.CompletedProcess([], 0)
        guest = IncusExecutor("guest", local)
        with redirect_stdout(io.StringIO()):
            result = NginxService(guest, restart_after_host_updates=True).restart_if_needed(
                UpdateResult("guest", True, 1)
            )
        self.assertTrue(result.success)
        prefix = ["incus", "exec", "guest", "--"]
        active = [*prefix, "systemctl", "is-active", "--quiet", "nginx"]
        self.assertEqual([entry.args[0] for entry in local.run.call_args_list], [
            active, [*prefix, "nginx", "-t"], [*prefix, "systemctl", "restart", "nginx"], active,
        ])


class NotificationTests(unittest.TestCase):
    def test_master_and_event_switches_filter_structured_events(self):
        provider = Mock()
        defaults = NotificationsConfig()
        for event_type in EventType:
            with self.subTest(event_type=event_type):
                event = NotificationEvent(event_type, "title", "message")
                for enabled, category in ((True, True), (True, False), (False, True)):
                    provider.reset_mock()
                    settings = replace(defaults, enabled=enabled, **{event_type.value: category})
                    Notifier(provider, settings).send(event)
                    if enabled and category:
                        provider.send.assert_called_once_with(event)
                    else:
                        provider.send.assert_not_called()

    def test_ntfy_formats_post_and_hides_transport_errors(self):
        provider = NtfyProvider("https://ntfy.example.invalid/synthetic-token", 4)
        event = NotificationEvent(EventType.UPDATE_FAILED, "title", "message", "high", ("warning",))
        with (
            patch("urllib.request.urlopen", side_effect=urllib.error.URLError("synthetic-token")) as urlopen,
            patch("pacwarden.notifications.ntfy.warning") as warning,
        ):
            provider.send(event)
        warning.assert_called_once_with("Failed to send ntfy notification")
        request, = urlopen.call_args.args
        self.assertEqual(urlopen.call_args.kwargs, {"timeout": 4})
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.data, b"message")
        self.assertEqual(request.get_header("Title"), "title")
        self.assertEqual(request.get_header("Priority"), "high")
        self.assertEqual(request.get_header("Tags"), "warning")
        self.assertNotIn("synthetic-token", repr(provider))

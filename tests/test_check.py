import io
import json
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from textwrap import dedent
from types import SimpleNamespace
from unittest.mock import Mock, create_autospec, patch

from pacwarden.checking import check_targets
from pacwarden.cli import main, parse_args
from pacwarden.execution.base import Executor
from pacwarden.models import UpdateTarget
from pacwarden.packages import apt, pacman
from pacwarden.packages.base import AvailableUpdates, PackageUpdate
from pacwarden.reporting import DiagnosticError


REPOSITORY_ERROR = "\033[31mhttps://user:private-token@example.test/repo\033[0m"


def completed(code=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], code, stdout=stdout, stderr=stderr)


class PackageCheckTests(unittest.TestCase):
    def setUp(self):
        self.executor = create_autospec(Executor, instance=True)

    def test_arch_parses_versions_and_refreshes_once_per_call(self):
        self.executor.run.return_value = completed(stdout="clang-tools 1:21.0.1-1 -> 1:21.0.2-1.2\n")
        manager = pacman.PacmanPackageManager()
        for refresh in (False, True):
            with self.subTest(refresh=refresh):
                self.executor.run.reset_mock()
                result = manager.available_updates(self.executor, refresh=refresh)
                self.assertEqual(result, AvailableUpdates((
                    PackageUpdate("clang-tools", "1:21.0.1-1", "1:21.0.2-1.2"),
                ), True))
                self.executor.run.assert_called_once()
                self.assertEqual(self.executor.run.call_args.kwargs, {
                    "capture_output": True, "check": False, "sensitive": True,
                })

    def test_arch_exit_status(self):
        manager = pacman.PacmanPackageManager()
        self.executor.run.return_value = completed(2)
        self.assertEqual(manager.available_updates(self.executor, refresh=False), AvailableUpdates((), True))
        for code, message in ((1, "exit 1"), (127, "Install pacman-contrib"), (73, "/tmp permissions")):
            with self.subTest(code=code):
                self.executor.run.return_value = completed(code, stderr=REPOSITORY_ERROR)
                with self.assertRaisesRegex(DiagnosticError, message) as caught:
                    manager.available_updates(self.executor, refresh=False)
                self.assertNotIn("private-token", str(caught.exception))

    def test_arch_rejects_malformed_or_duplicate_results(self):
        manager = pacman.PacmanPackageManager()
        for output in ("", "curl 1-1 => 2-1\n", "curl 1-1 -> 2-1\n" * 2,
                       "curl 1-1 -> https://user:private-token@example.test/2\n"):
            with self.subTest(output=output):
                self.executor.run.return_value = completed(stdout=output)
                with self.assertRaises(DiagnosticError):
                    manager.available_updates(self.executor, refresh=False)

    def test_apt_parses_cached_metadata_without_refreshing(self):
        self.executor.run.return_value = completed(stdout=(
            "libfoo++1:amd64\t2:1.0~rc1-0ubuntu0.1\t2:1.0+really1.2-1ubuntu1\n"
            "libfoo++1:i386\t2:1.0~rc1-0ubuntu0.1\t2:1.0+really1.2-1ubuntu1\n"
        ))
        manager = apt.AptPackageManager(autoremove=True)
        result = manager.available_updates(self.executor, refresh=False)
        self.assertFalse(result.metadata_refreshed)
        self.assertEqual(result.updates, tuple(
            PackageUpdate(f"libfoo++1:{arch}", "2:1.0~rc1-0ubuntu0.1", "2:1.0+really1.2-1ubuntu1")
            for arch in ("amd64", "i386")
        ))
        self.executor.run.assert_called_once_with(
            ["env", "LC_ALL=C", "/usr/bin/python3", "-IB", "-c", apt._QUERY],
            capture_output=True, check=False, sensitive=True,
        )

    def test_apt_no_updates_with_and_without_refresh(self):
        self.executor.run.return_value = completed()
        manager = apt.AptPackageManager(autoremove=True)
        for refresh in (False, True):
            with self.subTest(refresh=refresh):
                self.executor.run.reset_mock()
                self.assertEqual(manager.available_updates(self.executor, refresh=refresh), AvailableUpdates((), refresh))
                scripts = [call.args[0][-1] for call in self.executor.run.call_args_list]
                self.assertEqual(scripts, [apt._REFRESH, apt._QUERY] if refresh else [apt._QUERY])

    def test_apt_refresh_failure_stops_before_query_and_redacts_output(self):
        manager = apt.AptPackageManager(autoremove=True)
        for stderr, message in ((REPOSITORY_ERROR, "exit 100"), ("Permission denied", "Check permissions"),
                                ("Could not get lock", "holds the metadata lock")):
            with self.subTest(message=message):
                self.executor.run.reset_mock()
                self.executor.run.return_value = completed(100, stderr=stderr)
                with self.assertRaisesRegex(DiagnosticError, f"APT metadata refresh failed.*{message}") as caught:
                    manager.available_updates(self.executor, refresh=True)
                self.assertNotIn("private-token", str(caught.exception))
                self.executor.run.assert_called_once()

    def test_apt_query_failures(self):
        manager = apt.AptPackageManager(autoremove=False)
        for code, message in ((11, "Install python3-apt"), (1, "APT update query failed")):
            with self.subTest(code=code):
                self.executor.run.return_value = completed(code, stderr=REPOSITORY_ERROR)
                with self.assertRaisesRegex(DiagnosticError, message) as caught:
                    manager.available_updates(self.executor, refresh=False)
                self.assertNotIn("private-token", str(caught.exception))

    def test_apt_rejects_malformed_or_duplicate_results(self):
        manager = apt.AptPackageManager(autoremove=False)
        for output in ("curl 1 2\n", "curl\t1\t2\n" * 2, "curl\t1\t\033[31m2\n", REPOSITORY_ERROR):
            with self.subTest(output=output):
                self.executor.run.return_value = completed(stdout=output)
                with self.assertRaisesRegex(DiagnosticError, "Unexpected APT package list format"):
                    manager.available_updates(self.executor, refresh=False)

    def test_apt_query_uses_candidates_from_an_in_memory_cache(self):
        package = SimpleNamespace(fullname="libssl3:i386", is_upgradable=True,
                                  installed=SimpleNamespace(version="3.0.0-1"),
                                  candidate=SimpleNamespace(version="3.0.0-2"))
        cache = Mock(return_value=[SimpleNamespace(is_upgradable=False), package])
        config = Mock()
        with (patch.dict("sys.modules", {"apt": SimpleNamespace(Cache=cache),
                                         "apt_pkg": SimpleNamespace(config=config)}),
              redirect_stdout(io.StringIO()) as stdout):
            exec(apt._QUERY, {})
        self.assertEqual(stdout.getvalue(), "libssl3:i386\t3.0.0-1\t3.0.0-2\n")
        cache.assert_called_once_with(memonly=True)
        config.set.assert_called_once_with("Dir::Cache::srcpkgcache", "")

    def test_workflow_accepts_a_new_package_manager_capability(self):
        class OtherPackageManager:
            def available_updates(self, executor, *, refresh):
                return AvailableUpdates((PackageUpdate("other", "1", "2"),), refresh)

            def installed_versions(self, executor):
                raise AssertionError("Check called installed_versions")

            def upgrade(self, executor):
                raise AssertionError("Check called upgrade")

        target = UpdateTarget("test", self.executor, OtherPackageManager())
        result, = check_targets([target], refresh=True).targets
        self.assertTrue(result.success)
        self.assertEqual(result.available, AvailableUpdates((PackageUpdate("other", "1", "2"),), True))
        self.executor.run.assert_not_called()


class CheckCommandSafetyTests(unittest.TestCase):
    def setUp(self):
        self.bin = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def fake_binary(self, name, script):
        executable = self.bin / name
        executable.write_text("#!/bin/sh\n" + script, encoding="utf-8")
        executable.chmod(0o755)

    def link_binaries(self, *names):
        for name in names:
            path = shutil.which(name)
            if path is None:
                self.skipTest(f"{name} is required to exercise the check wrapper")
            (self.bin / name).symlink_to(path)

    def test_arch_wrapper_overrides_inherited_db_and_deletes_only_its_private_db(self):
        self.link_binaries("bash", "mktemp", "rm", "env")
        live_db = self.bin / "live-sync"
        live_db.mkdir()
        recorded_db = self.bin / "used-db"
        self.fake_binary("checkupdates", dedent("""\
            test "$#" -eq 1 && test "$1" = --nocolor || exit 1
            test -z "${CHANGE+x}${LIBRARY+x}${BASH_ENV+x}" || exit 1
            case "$CHECKUPDATES_DB" in /tmp/pacwarden-check.*) ;; *) exit 1 ;; esac
            test -d "$CHECKUPDATES_DB" || exit 1
            printf '%s' "$CHECKUPDATES_DB" > "$TEST_USED_DB"
            exit 2
        """))
        executor = Mock(spec=Executor)
        executor.run.side_effect = lambda command, **_: subprocess.run(
            command, text=True, capture_output=True, check=False, timeout=5,
            env={"PATH": str(self.bin), "CHECKUPDATES_DB": str(live_db), "CHANGE": "1",
                 "LIBRARY": "unexpected", "BASH_ENV": "unexpected", "TEST_USED_DB": str(recorded_db)},
        )
        manager = pacman.PacmanPackageManager()
        self.assertEqual(manager.available_updates(executor, refresh=False), AvailableUpdates((), True))
        self.assertTrue(live_db.is_dir())
        self.assertFalse(Path(recorded_db.read_text()).exists())
        (self.bin / "checkupdates").unlink()
        with self.assertRaisesRegex(DiagnosticError, "Install pacman-contrib"):
            manager.available_updates(executor, refresh=False)

    def test_apt_refresh_wrapper_passes_hook_override_and_only_updates_metadata(self):
        self.link_binaries("sh")
        self.fake_binary("apt-get", dedent("""\
            test "$#" -eq 8 && test "$1" = -c && test "$2" = /dev/fd/3 || exit 1
            test "$3" = --error-on=any && test "$8" = update || exit 1
            IFS= read -r override < "$2" || exit 1
            test "$override" = '#clear APT::Update;' || exit 1
            if IFS= read -r input; then exit 1; fi
        """))
        result = subprocess.run([str(self.bin / "sh"), "-c", apt._REFRESH], capture_output=True,
                                text=True, env={"PATH": str(self.bin)}, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)


class CheckCliTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.config = self.directory / "config.toml"
        self.state = self.directory / "reboot-state"
        self.stdout = self.enterContext(redirect_stdout(io.StringIO()))
        self.stderr = self.enterContext(redirect_stderr(io.StringIO()))
        self.commands = self.enterContext(patch("subprocess.run"))
        self.update_components = [self.enterContext(patch(f"pacwarden.cli.{name}")) for name in (
            "Orchestrator", "RebootStateStore", "HostReboot", "NginxService", "Notifier", "NtfyProvider",
            "validate_environment",
        )]
        self.addCleanup(self.assert_read_only)

    def assert_read_only(self):
        for component in self.update_components:
            component.assert_not_called()
        self.assertFalse(self.state.exists())
        for call in self.commands.call_args_list:
            self.assertTrue(call.kwargs["capture_output"])
            self.assertFalse(call.kwargs["check"])
            command = call.args[0]
            if command[:2] == ["incus", "info"]:
                continue
            self.assertIn(command[-1], (pacman._CHECKUPDATES, apt._REFRESH, apt._QUERY))

    def write_config(self, text=""):
        self.config.write_text(f"state_dir = {json.dumps(str(self.state))}\n" + dedent(text), encoding="utf-8")

    def test_host_only_runs_without_root_or_notification_credentials(self):
        self.write_config("""
            [notifications.ntfy]
            url_env = "UNSET_TEST_PACWARDEN_NTFY_URL"
        """)
        self.commands.return_value = completed(2)
        with patch("os.geteuid", return_value=1000):
            self.assertEqual(main(["--config", str(self.config), "check"]), 0)
        self.commands.assert_called_once()
        self.assertRegex(self.stdout.getvalue(), r"host\s+arch\s+0\s+refreshed")
        self.assertIn("0 updates across 0 targets", self.stdout.getvalue())
        self.assertEqual(self.stderr.getvalue(), "")

    def test_partial_failures_keep_target_order_and_emit_only_json_on_stdout(self):
        self.write_config("""
            [[containers]]
            name = "stopped"
            distro = "arch"
            [[containers]]
            name = "remote:ubuntu"
            distro = "ubuntu"
            [[containers]]
            name = "inaccessible"
            distro = "ubuntu"
            [[containers]]
            name = "apt-failure"
            distro = "ubuntu"
            [[containers]]
            name = "arch"
            distro = "arch"
        """)
        running = completed(stdout="Status: RUNNING\n")
        self.commands.side_effect = [
            completed(stdout="zstd 1.5.7-1 -> 1.5.7-2\nnginx 1-1 -> 2-1\n"),
            completed(stdout="Status: STOPPED\n"),
            running, completed(stdout="libssl3:arm64\t3.0.0-1\t3.0.0-2\n"),
            completed(1, stderr=f"permission denied {REPOSITORY_ERROR}"),
            running, completed(1, stderr=REPOSITORY_ERROR),
            running, completed(2),
        ]
        self.assertEqual(main(["check", "--config", str(self.config), "--json", "--details"]), 1)
        raw = self.stdout.getvalue()
        output = json.loads(raw)
        host, stopped, ubuntu, inaccessible, apt_failure, arch = output["targets"]
        self.assertEqual([target["name"] for target in output["targets"]],
                         ["host", "stopped", "remote:ubuntu", "inaccessible", "apt-failure", "arch"])
        self.assertEqual(output["total_updates"], 3)
        self.assertEqual(output["failed_targets"], 3)
        self.assertTrue(host["metadata_refreshed"])
        self.assertIsNone(host["error"])
        self.assertEqual(host["updates"], [
            {"name": "nginx", "installed_version": "1-1", "available_version": "2-1"},
            {"name": "zstd", "installed_version": "1.5.7-1", "available_version": "1.5.7-2"},
        ])
        self.assertEqual(ubuntu["distro"], "ubuntu")
        self.assertTrue(ubuntu["success"])
        self.assertFalse(ubuntu["metadata_refreshed"])
        self.assertEqual(arch["updates"], [])
        for target, reason in ((stopped, "STOPPED"), (inaccessible, "Incus access"), (apt_failure, "APT update query")):
            self.assertFalse(target["success"])
            self.assertIsNone(target["updates"])
            self.assertIsNone(target["metadata_refreshed"])
            self.assertIn(reason, target["error"])
        self.assertNotIn("\033", raw)
        self.assertNotIn("\\u001b", raw)
        self.assertNotIn("private-token", raw + self.stderr.getvalue())
        self.assertIn("Checking for available package updates", self.stderr.getvalue())
        self.assertEqual([call.args[0][:4] for call in self.commands.call_args_list[1:]], [
            ["incus", "info", "stopped"],
            ["incus", "info", "remote:ubuntu"], ["incus", "exec", "remote:ubuntu", "--"],
            ["incus", "info", "inaccessible"],
            ["incus", "info", "apt-failure"], ["incus", "exec", "apt-failure", "--"],
            ["incus", "info", "arch"], ["incus", "exec", "arch", "--"],
        ])

    def test_refresh_checks_ubuntu_even_when_host_check_fails(self):
        self.write_config('[[containers]]\nname = "guest"\ndistro = "ubuntu"\n')
        self.commands.side_effect = [completed(1), completed(stdout="Status: RUNNING\n"),
                                     completed(), completed(stdout="curl:amd64\t1\t2\n")]
        self.assertEqual(main(["check", "--config", str(self.config), "--refresh", "--json"]), 1)
        host, guest = json.loads(self.stdout.getvalue())["targets"]
        self.assertFalse(host["success"])
        self.assertTrue(guest["metadata_refreshed"])
        self.assertEqual([call.args[0][-1] for call in self.commands.call_args_list],
                         [pacman._CHECKUPDATES, "guest", apt._REFRESH, apt._QUERY])

    def test_details_show_full_fields_and_cached_metadata(self):
        self.write_config('[[containers]]\nname = "guest"\ndistro = "ubuntu"\n')
        package = "package-with-a-very-long-name-for-the-details-column:amd64"
        version = "2:1.2345678901234567890~really-long-1ubuntu2.1"
        self.commands.side_effect = [completed(127), completed(stdout="Status: RUNNING\n"),
                                     completed(stdout=f"{package}\t1.0\t{version}\n")]
        self.assertEqual(main(["check", "--config", str(self.config), "--details"]), 1)
        output = self.stdout.getvalue()
        self.assertIn("host (arch): FAILED", output)
        self.assertIn("guest (ubuntu): 1 update — metadata cached (may be stale)", output)
        self.assertIn(package, output)
        self.assertIn(version, output)
        self.assertIn("1 update across 1 target. 1 check failed.", output)
        self.assertIn("Install pacman-contrib", self.stderr.getvalue())

    def test_config_and_usage_errors(self):
        self.config.write_text("containers = true\n", encoding="utf-8")
        self.assertEqual(main(["check", "--json", "--config", str(self.config)]), 2)
        self.assertEqual(self.stdout.getvalue(), "")
        self.assertIn("containers must be an array", self.stderr.getvalue())
        for argv in (["--json"], ["check", "--no-reboot"], ["--no-reboot", "check"], ["update", "--refresh"]):
            with self.subTest(argv=argv), self.assertRaises(SystemExit) as caught:
                main(argv)
            self.assertEqual(caught.exception.code, 2)
        self.commands.assert_not_called()

    def test_checks_with_available_updates_exit_zero(self):
        self.write_config()
        self.commands.return_value = completed(stdout="curl 1-1 -> 2-1\n")
        self.assertEqual(main(["--config", str(self.config), "check", "--json"]), 0)
        self.assertEqual(json.loads(self.stdout.getvalue())["total_updates"], 1)

    def test_check_interrupt_exits_130(self):
        self.write_config()
        self.commands.side_effect = KeyboardInterrupt
        self.assertEqual(main(["check", "--json", "--config", str(self.config)]), 130)
        self.assertEqual(self.stdout.getvalue(), "")
        self.assertIn("Update check interrupted", self.stderr.getvalue())

    def test_legacy_and_explicit_update_parse_the_same_options(self):
        flags = ["--no-reboot", "--config", str(self.config), "--no-apt-autoremove", "--no-notifications"]
        legacy = parse_args(flags)
        self.assertEqual(legacy.command, "update")
        self.assertEqual(legacy, parse_args(["update", *flags]))
        self.assertEqual(legacy, parse_args([*flags[:3], "update", *flags[3:]]))

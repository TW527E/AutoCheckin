"""Black-box installer tests; no command can reach the host's systemd.

Only the copied installer's UNIT_DIR assignment is changed. PATH is an isolated
allowlist of harmless utilities plus fake account/systemd commands; runuser never
switches identity, and every home, config, unit and staging path is temporary.
"""

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TIMER = "autocheckin-checkin.timer"
CHECKIN = "autocheckin-checkin.service"
TELEGRAM = "autocheckin-telegram.service"
UNITS = (TIMER, CHECKIN, TELEGRAM)

# One executable is installed under each mocked command name. Unsupported
# invocations fail rather than falling through to any real host command.
FAKE_COMMAND = r'''
import json
import os
import sys
from pathlib import Path

name = Path(sys.argv[0]).name
args = sys.argv[1:]
root = Path(os.environ["FIXTURE_ROOT"])
state = json.loads((root / "mock-state.json").read_text())
with (root / "commands.jsonl").open("a") as stream:
    stream.write(json.dumps([name, *args]) + "\n")

def unexpected():
    print("UNEXPECTED MOCK COMMAND: " + repr([name, *args]), file=sys.stderr)
    sys.exit(97)

if [name, *args] in state.get("fail_calls", []):
    sys.exit(1)

if name == "id":
    print(state.get("euid", 0) if args == ["-u"] else state.get("current_user", "root"))
elif name == "getent":
    accounts = {"alice": (1234, 2345, root / "home"), "bob": (4567, 5678, root / "bob-home")}
    if args[1] not in accounts:
        sys.exit(2)
    uid, gid, home = accounts[args[1]]
    print(f"{args[1]}:x:{uid}:{gid}:Fixture account:{home}:/bin/sh")
elif name == "systemd-analyze":
    sys.exit(1 if state.get("bad_calendar") else 0)
elif name == "runuser":
    flag, path = args[4:6]
    modes = {"-r": os.R_OK, "-x": os.X_OK, "-w": os.W_OK}
    sys.exit(0 if os.access(path, modes[flag]) else 1)
elif name == "systemctl":
    if args[0] == "show":
        print("loaded" if (Path(os.environ["SYSTEM_UNIT_DIR"]) / args[1]).exists() else "not-found")
    elif args[0] == "stop" and args[1] in state.get("stop_failures", []):
        sys.exit(1)
    elif args[0] not in ("stop", "disable", "enable", "daemon-reload", "--no-pager"):
        unexpected()
    elif args[0] == "--no-pager":
        sys.exit(state.get("status_exit", 0))
else:
    # Includes loginctl/sudo guards: neither may be called.
    unexpected()
'''


class SystemdInstallerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="autocheckin-systemd-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.bin = self.root / "bin"
        self.project = self.root / "project with spaces"
        self.home = self.root / "home"
        self.units = self.root / "system-units"
        self.tmp = self.root / "tmp"
        for directory in (self.bin, self.project, self.home, self.units, self.tmp, self.root / "bob-home"):
            directory.mkdir(parents=True, exist_ok=True)
        self.script = self.project / "install_linux_systemd.sh"
        source = (ROOT / "install_linux_systemd.sh").read_text(encoding="utf-8")
        assignment = "UNIT_DIR=/etc/systemd/system"
        self.assertEqual(source.count(assignment), 1, "Review fixture isolation if UNIT_DIR changes")
        self.script.write_text(source.replace(assignment, "UNIT_DIR=" + shlex.quote(str(self.units)), 1))
        self.runner = self.project / "run_checkin.sh"
        self.runner.write_text("#!/bin/sh\nexit 99\n")
        self.runner.chmod(0o755)
        self.config = self.project / "config.json"
        self.write_config()
        self.state_path = self.root / "mock-state.json"
        self.log_path = self.root / "commands.jsonl"
        self.state_path.write_text("{}")
        self.log_path.write_text("")
        self.bash = shutil.which("bash")
        self.assertIsNotNone(self.bash)
        # No ambient PATH: even a machine with systemd cannot reach its real tools.
        for name in ("dirname", "grep", "mkdir", "install", "mktemp", "rm"):
            executable = shutil.which(name)
            self.assertIsNotNone(executable, name + " is required to run installer tests")
            (self.bin / name).symlink_to(executable)
        (self.bin / "python3").symlink_to(sys.executable)
        for name in ("id", "getent", "systemctl", "runuser", "systemd-analyze", "loginctl", "sudo"):
            path = self.bin / name
            path.write_text("#!" + sys.executable + "\n" + FAKE_COMMAND)
            path.chmod(0o755)
        self.env = {
            "PATH": str(self.bin), "HOME": str(self.home), "TMPDIR": str(self.tmp),
            "LC_ALL": "C", "SUDO_USER": "alice", "FIXTURE_ROOT": str(self.root),
            "SYSTEM_UNIT_DIR": str(self.units),
        }

    def write_config(self, telegram=None, timezone="Asia/Taipei"):
        self.config.write_text(json.dumps({"timezone": timezone, "telegram": telegram or {}}))

    def configure(self, **settings):
        state = json.loads(self.state_path.read_text())
        state.update(settings)
        self.state_path.write_text(json.dumps(state))

    def run_installer(self, *args, success=True, env=None):
        self.log_path.write_text("")
        result = subprocess.run(
            [self.bash, str(self.script), *args], cwd=self.project,
            env=self.env if env is None else env, text=True, capture_output=True, timeout=20,
        )
        self.calls = [json.loads(line) for line in self.log_path.read_text().splitlines()]
        self.assertNotIn("UNEXPECTED MOCK COMMAND", result.stderr, result.stderr)
        if success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(any(call[0] in ("loginctl", "sudo") for call in self.calls))
        self.assertFalse(any(call[0] == "systemctl" and "start" in call for call in self.calls), self.calls)
        return result

    def ctl_calls(self):
        return [call[1:] for call in self.calls if call[0] == "systemctl"]

    def assert_no_changes(self):
        for call in self.ctl_calls():
            self.assertNotIn(call[0], ("stop", "disable", "enable", "start", "restart", "daemon-reload"), self.calls)

    def snapshot(self):
        result = {}
        for directory in (self.project, self.home, self.units):
            for path in directory.rglob("*"):
                key = str(path.relative_to(self.root))
                result[key] = ("link", os.readlink(path)) if path.is_symlink() else (
                    ("dir",) if path.is_dir() else ("file", path.read_bytes(), path.stat().st_mode)
                )
        return result

    def seed_units(self, directory):
        directory.mkdir(parents=True, exist_ok=True)
        for unit in UNITS:
            text = "[Unit]\nDescription=AutoCheckin existing fixture\n"
            text += ("[Timer]\nUnit=" + CHECKIN + "\n") if unit == TIMER else (
                '[Service]\nExecStart="' + str(self.runner) + '" --old-fixture\n'
            )
            (directory / unit).write_text(text)

    def assert_installed(self, telegram=False, uid=1234, gid=2345, home=None):
        for unit in UNITS:
            self.assertTrue((self.units / unit).is_file())
            self.assertEqual((self.units / unit).stat().st_mode & 0o777, 0o644)
        checkin = (self.units / CHECKIN).read_text()
        listener = (self.units / TELEGRAM).read_text()
        for text in (checkin, listener):
            self.assertIn("User=" + str(uid) + "\n", text)
            self.assertIn("Group=" + str(gid) + "\n", text)
            self.assertIn('Environment="HOME=' + str(home or self.home) + '"', text)
            self.assertIn("WorkingDirectory=" + str(self.project).replace("%", "%%") + "\n", text)
            self.assertIn('--config "' + str(self.config) + '"', text)
        self.assertIn("--headless --no-telegram-poll", checkin)
        self.assertIn("--telegram-listen", listener)
        self.assertIn("Persistent=true", (self.units / TIMER).read_text())
        enabled = [call for call in self.ctl_calls() if call[0] == "enable"]
        self.assertEqual(enabled, [["enable", "--now", TIMER]] + (
            [["enable", "--now", TELEGRAM]] if telegram else []))
        self.assertEqual(list(self.tmp.iterdir()), [], "Staging should be cleaned up")

    def test_fresh_install_writes_units_for_the_sudo_account(self):
        self.run_installer()
        self.assert_installed()
        self.assertIn(["getent", "passwd", "alice"], self.calls)
        self.assertIn(["systemd-analyze", "calendar", "*-*-* 08:00:00 Asia/Taipei"], self.calls)
        self.assertIn("OnCalendar=*-*-* 08:00:00 Asia/Taipei", (self.units / TIMER).read_text())
        self.assertFalse(any(call[0] in ("stop", "disable") for call in self.ctl_calls()))
        for flag, path in (("-r", self.config), ("-x", self.runner), ("-w", self.project)):
            self.assertIn(["runuser", "-u", "alice", "--", "test", flag, str(path)], self.calls)

    def test_explicit_run_as_overrides_sudo_user(self):
        self.run_installer("--run-as", "bob")
        self.assert_installed(uid=4567, gid=5678, home=self.root / "bob-home")
        self.assertIn(["getent", "passwd", "bob"], self.calls)

    def test_without_sudo_user_uses_current_account(self):
        env = dict(self.env)
        del env["SUDO_USER"]
        self.configure(current_user="alice")
        self.run_installer(env=env)
        self.assertIn(["id", "-un"], self.calls)
        self.assert_installed()

    def test_install_and_remove_require_root_without_changes(self):
        self.seed_units(self.units)
        before = self.snapshot()
        self.configure(euid=1234)
        for args in ((), ("--remove",)):
            with self.subTest(args=args):
                result = self.run_installer(*args, success=False)
                self.assertIn("requires root", result.stderr)
                self.assertEqual(before, self.snapshot())
                self.assertEqual(self.ctl_calls(), [])

    def test_show_is_read_only_without_root_or_a_valid_config(self):
        self.seed_units(self.units)
        self.config.write_text("not json")
        self.configure(euid=1234, status_exit=3)
        before = self.snapshot()
        self.run_installer("--show")
        self.assertEqual(self.calls, [["systemctl", "--no-pager", "status", *UNITS]])
        self.assertEqual(before, self.snapshot())

    def test_remove_stops_and_disables_all_three_and_preserves_user_data(self):
        self.seed_units(self.units)
        self.config.write_text("invalid but removal must not validate configuration")
        (self.home / "browser-profile").mkdir()
        (self.home / "browser-profile/Cookies").write_text("fixture cookies")
        (self.project / "telegram-state.json").write_text("fixture state")
        (self.units / "unrelated.service").write_text("custom unrelated unit")
        before = self.snapshot()
        self.run_installer("--remove")
        self.assertEqual(self.ctl_calls(), [call for unit in UNITS for call in (
            ["show", unit, "--property=LoadState", "--value"], ["stop", unit], ["disable", unit]
        )] + [["daemon-reload"]])
        after = self.snapshot()
        for unit in UNITS:
            before.pop(str((self.units / unit).relative_to(self.root)))
        self.assertEqual(before, after)
        self.assertFalse(any(call[0] in ("getent", "runuser", "systemd-analyze") for call in self.calls))

    def test_remove_missing_units_is_idempotent(self):
        for _ in range(2):
            self.run_installer("--remove")
            self.assertFalse(any(call[0] in ("stop", "disable", "enable") for call in self.ctl_calls()))
            self.assertEqual(list(self.units.iterdir()), [])

    def test_system_stop_failure_prevents_overwrite_or_remove(self):
        self.seed_units(self.units)
        before = self.snapshot()
        for args in ((), ("--remove",)):
            for unit in UNITS:
                with self.subTest(args=args, unit=unit):
                    self.configure(stop_failures=[unit])
                    self.run_installer(*args, success=False)
                    self.assertEqual(before, self.snapshot())
                    self.assertFalse(any(call[0] in ("enable", "daemon-reload") for call in self.ctl_calls()))

    def test_unknown_units_symlinks_and_dropins_are_preserved(self):
        custom = self.root / "custom.service"
        custom.write_text("custom outside selected unit directories")
        for kind in ("unknown", "wrong-timer", "wrong-exec", "symlink", "dropin"):
            with self.subTest(kind=kind):
                unit = TIMER if kind == "wrong-timer" else CHECKIN
                path = self.units / (unit + ".d" if kind == "dropin" else unit)
                if kind == "dropin":
                    path.mkdir()
                    (path / "override.conf").write_text("[Service]\nEnvironment=CUSTOM=1\n")
                elif kind == "symlink":
                    path.symlink_to(custom)
                else:
                    path.write_text({"unknown": "[Service]\nExecStart=/bin/true\n",
                                     "wrong-timer": "Description=AutoCheckin\nUnit=custom.service\n",
                                     "wrong-exec": "Description=AutoCheckin\nExecStart=/bin/true\n"}[kind])
                before = self.snapshot()
                for args in ((), ("--remove",)):
                    self.run_installer(*args, success=False)
                    self.assertEqual(before, self.snapshot())
                    self.assert_no_changes()
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
                else:
                    path.unlink()
        self.assertEqual(custom.read_text(), "custom outside selected unit directories")

    def test_invalid_config_never_changes_existing_services(self):
        self.seed_units(self.units)
        for text in ("{invalid", "[]", '{"timezone":"Not/A_Zone"}', '{"telegram":[]}', '{"timezone":null}'):
            with self.subTest(config=text):
                self.config.write_text(text)
                before = self.snapshot()
                result = self.run_installer(success=False)
                self.assertIn("Configuration validation failed", result.stderr)
                self.assertEqual(before, self.snapshot())
                self.assert_no_changes()

    def test_invalid_calendar_never_changes_existing_services(self):
        self.seed_units(self.units)
        self.configure(bad_calendar=True)
        before = self.snapshot()
        result = self.run_installer("--on-calendar", "not a calendar", success=False)
        self.assertIn("Invalid calendar", result.stderr)
        self.assertIn(["systemd-analyze", "calendar", "not a calendar Asia/Taipei"], self.calls)
        self.assertEqual(before, self.snapshot())
        self.assert_no_changes()

    def test_missing_config_and_runtime_permission_failures_are_non_destructive(self):
        self.seed_units(self.units)
        before = self.snapshot()
        self.run_installer("--config", str(self.project / "missing.json"), success=False)
        self.assertEqual(before, self.snapshot())
        self.assert_no_changes()
        for flag, path in (("-r", self.config), ("-x", self.runner), ("-w", self.project)):
            with self.subTest(flag=flag):
                self.configure(fail_calls=[["runuser", "-u", "alice", "--", "test", flag, str(path)]])
                self.run_installer(success=False)
                self.assertEqual(before, self.snapshot())
                self.assert_no_changes()

    def test_calendar_and_relative_config_options(self):
        self.write_config(timezone="UTC")
        self.run_installer("--config", "config.json", "--on-calendar", "Mon..Fri 09:30:00")
        self.assert_installed()
        self.assertIn("OnCalendar=Mon..Fri 09:30:00 UTC", (self.units / TIMER).read_text())
        self.assertIn(["systemd-analyze", "calendar", "Mon..Fri 09:30:00 UTC"], self.calls)

    def test_telegram_requires_both_credentials_and_rerun_disables_old_listener(self):
        cases = (({}, False), ({"bot_token": "fixture-token"}, False),
                 ({"chat_id": "123"}, False),
                 ({"bot_token": "fixture-token", "chat_id": "123"}, True),
                 ({"bot_token": "", "chat_id": "123"}, False),
                 ({"bot_token": "fixture-token", "chat_id": "123"}, True))
        for index, (telegram, enabled) in enumerate(cases):
            with self.subTest(telegram=telegram):
                self.write_config(telegram=telegram)
                before_config = self.config.read_bytes()
                result = self.run_installer()
                self.assert_installed(telegram=enabled)
                self.assertEqual(before_config, self.config.read_bytes())
                self.assertEqual("installed but not started" in result.stderr, not enabled)
                if index:
                    calls = self.ctl_calls()
                    for unit in UNITS:
                        self.assertIn(["stop", unit], calls)
                        self.assertIn(["disable", unit], calls)
                        self.assertLess(calls.index(["disable", unit]), calls.index(["enable", "--now", TIMER]))

    def test_rerun_is_stable_and_preserves_unrelated_files(self):
        (self.project / "telegram-state.json").write_text("fixture state")
        (self.home / "browser-profile").mkdir()
        (self.home / "browser-profile/Cookies").write_text("fixture cookies")
        (self.units / "unrelated.service").write_text("custom system service")
        preserved = {path: path.read_bytes() for path in (
            self.config, self.project / "telegram-state.json",
            self.home / "browser-profile/Cookies", self.units / "unrelated.service",
        )}
        self.run_installer()
        first = self.snapshot()
        self.run_installer()
        self.assert_installed()
        self.assertEqual(first, self.snapshot())
        for path, content in preserved.items():
            self.assertEqual(path.read_bytes(), content)


if __name__ == "__main__":
    unittest.main()

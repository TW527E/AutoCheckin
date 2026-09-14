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
import subprocess
import sys
from pathlib import Path

name = Path(sys.argv[0]).name
args = sys.argv[1:]
root = Path(os.environ["FIXTURE_ROOT"])
state_path = root / "mock-state.json"
state = json.loads(state_path.read_text())
with (root / "commands.jsonl").open("a") as stream:
    stream.write(json.dumps([name, *args]) + "\n")

def unexpected():
    print("UNEXPECTED MOCK COMMAND: " + repr([name, *args]), file=sys.stderr)
    sys.exit(97)

def inside(path):
    try:
        Path(path).resolve().relative_to(root.resolve())
    except ValueError:
        unexpected()

def save():
    state_path.write_text(json.dumps(state))

if [name, *args] in state.get("fail_calls", []):
    sys.exit(1)

if name == "id":
    if args == ["-u"]:
        print(state.get("euid", 0))
    elif args == ["-un"]:
        print(state.get("current_user", "root"))
    else:
        unexpected()
elif name == "getent":
    if len(args) != 2 or args[0] != "passwd":
        unexpected()
    accounts = {"alice": (1234, 2345, root / "home"),
                "bob": (4567, 5678, root / "bob-home"),
                "root": (0, 0, root / "root-home")}
    if args[1] not in accounts:
        sys.exit(2)
    uid, gid, home = accounts[args[1]]
    print(f"{args[1]}:x:{uid}:{gid}:Fixture account:{home}:/bin/sh")
elif name == "systemd-analyze":
    if len(args) != 2 or args[0] != "calendar":
        unexpected()
    sys.exit(1 if state.get("bad_calendar") else 0)
elif name == "readlink":
    if len(args) != 3 or args[:2] != ["-m", "--"]:
        unexpected()
    inside(args[2])
    print(Path(args[2]).resolve())
elif name == "runuser":
    if len(args) < 4 or args[0] != "-u" or args[2] != "--":
        unexpected()
    command = args[3:]
    if len(command) == 3 and command[0] == "test":
        flag, path = command[1:]
        inside(path)
        modes = {"-r": os.R_OK, "-x": os.X_OK, "-w": os.W_OK}
        if flag not in modes:
            unexpected()
        sys.exit(0 if os.access(path, modes[flag]) else 1)
    if (len(command) >= 5 and command[0] == "env"
            and command[1].startswith("XDG_RUNTIME_DIR=/run/user/")
            and command[2].startswith("DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/")
            and command[3:5] == ["systemctl", "--user"]):
        # Deliberately ignore the real runtime/bus paths; invoke only our fake.
        sys.exit(subprocess.call([str(root / "bin" / "systemctl"), *command[4:]]))
    unexpected()
elif name == "systemctl":
    user = "--user" in args
    machine = any(arg.startswith("--machine=") for arg in args)
    command = [arg for arg in args if arg != "--user" and not arg.startswith("--machine=")]
    scope = "user" if user else "system"
    if command == ["show-environment"] and user:
        sys.exit(1 if state.get("machine_bus_failure" if machine else "bus_failure") else 0)
    if command and command[0] == "show":
        if len(command) != 4 or command[-1] != "--value":
            unexpected()
        unit, prop = command[1:3]
        if unit.startswith("user@") and not user and prop == "--property=ActiveState":
            print(state.get("manager", "inactive"))
        elif unit in ("autocheckin-checkin.timer", "autocheckin-checkin.service", "autocheckin-telegram.service"):
            directory = Path(os.environ["LEGACY_DIR"] if user else os.environ["SYSTEM_UNIT_DIR"])
            if prop == "--property=LoadState":
                loaded = state.get(scope + "_loaded", [])
                missing = unit in state.get(scope + "_not_found", [])
                print("loaded" if not missing and (unit in loaded or (directory / unit).exists()) else "not-found")
            elif prop == "--property=FragmentPath" and user:
                print(state.get("fragments", {}).get(unit, str(directory / unit)))
            elif prop == "--property=DropInPaths" and user:
                print(state.get("dropins", {}).get(unit, ""))
            elif prop == "--property=ActiveState" and user:
                print(state.get("active_after_stop", {}).get(unit, "inactive")
                      if unit in state.get("user_stopped", []) else "active")
            else:
                unexpected()
        else:
            unexpected()
    elif command == ["--no-pager", "status", "autocheckin-checkin.timer", "autocheckin-checkin.service", "autocheckin-telegram.service"] and not user:
        sys.exit(state.get("status_exit", 0))
    elif command == ["daemon-reload"]:
        pass
    elif len(command) == 2 and command[0] in ("stop", "disable") and command[1] in ("autocheckin-checkin.timer", "autocheckin-checkin.service", "autocheckin-telegram.service"):
        if command[0] == "stop":
            if [scope, command[1]] in state.get("stop_failures", []):
                sys.exit(1)
            state.setdefault(scope + "_stopped", []).append(command[1])
            save()
    elif len(command) == 3 and command[:2] == ["enable", "--now"] and command[2] in ("autocheckin-checkin.timer", "autocheckin-telegram.service") and not user:
        pass
    else:
        unexpected()
else:
    # Includes loginctl/sudo/dbus-run-session guards: none may be called.
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
        self.legacy = self.home / ".config/systemd/user"
        self.units = self.root / "system-units"
        self.tmp = self.root / "tmp"
        for directory in (self.bin, self.project, self.legacy, self.units, self.tmp,
                          self.root / "bob-home", self.root / "root-home"):
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
        for name in ("id", "getent", "systemctl", "runuser", "systemd-analyze", "readlink",
                     "loginctl", "sudo", "dbus-run-session"):
            path = self.bin / name
            path.write_text("#!" + sys.executable + "\n" + FAKE_COMMAND)
            path.chmod(0o755)
        self.env = {
            "PATH": str(self.bin), "HOME": str(self.home), "TMPDIR": str(self.tmp),
            "LC_ALL": "C", "SUDO_USER": "alice", "FIXTURE_ROOT": str(self.root),
            "LEGACY_DIR": str(self.legacy), "SYSTEM_UNIT_DIR": str(self.units),
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
        self.assertFalse(any(call[0] in ("loginctl", "sudo", "dbus-run-session") for call in self.calls))
        self.assertFalse(any(call[0] == "systemctl" and "start" in call for call in self.calls), self.calls)
        return result

    def ctl_calls(self, user=False):
        return [
            [arg for arg in call[1:] if arg != "--user" and not arg.startswith("--machine=")]
            for call in self.calls if call[0] == "systemctl" and ("--user" in call) == user
        ]

    def assert_no_changes(self):
        for call in self.ctl_calls() + self.ctl_calls(user=True):
            self.assertNotIn(call[0], ("stop", "disable", "enable", "start", "restart", "daemon-reload"), self.calls)

    def assert_no_user_bus(self):
        self.assertEqual(self.ctl_calls(user=True), [])
        self.assertFalse(any(call[0] == "runuser" and "env" in call for call in self.calls))

    def snapshot(self):
        result = {}
        for directory in (self.project, self.home, self.units):
            for path in directory.rglob("*"):
                key = str(path.relative_to(self.root))
                result[key] = ("link", os.readlink(path)) if path.is_symlink() else (
                    ("dir",) if path.is_dir() else ("file", path.read_bytes(), path.stat().st_mode)
                )
        return result

    def seed_units(self, directory, links=False):
        directory.mkdir(parents=True, exist_ok=True)
        for unit in UNITS:
            text = "[Unit]\nDescription=AutoCheckin existing fixture\n"
            text += ("[Timer]\nUnit=" + CHECKIN + "\n") if unit == TIMER else (
                '[Service]\nExecStart="' + str(self.runner) + '" --old-fixture\n'
            )
            (directory / unit).write_text(text)
            if links:
                for target in ("default.target.wants", "fixture.target.requires"):
                    parent = directory / target
                    parent.mkdir(exist_ok=True)
                    (parent / unit).symlink_to("../" + unit if target.endswith("wants") else directory / unit)

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

    def test_fresh_install_inactive_user_never_starts_user_bus_or_linger(self):
        self.run_installer()
        self.assert_installed()
        self.assert_no_user_bus()
        self.assertIn(["show", "user@1234.service", "--property=ActiveState", "--value"], self.ctl_calls())
        self.assertIn(["systemd-analyze", "calendar", "*-*-* 08:00:00 Asia/Taipei"], self.calls)
        self.assertIn("OnCalendar=*-*-* 08:00:00 Asia/Taipei", (self.units / TIMER).read_text())
        self.assertFalse(any(call[0] in ("stop", "disable") for call in self.ctl_calls()))

    def test_sudo_user_is_runtime_identity_not_effective_root(self):
        self.configure(current_user="root")
        self.run_installer()
        self.assert_installed()
        self.assertIn(["getent", "passwd", "alice"], self.calls)
        for flag, path in (("-r", self.config), ("-x", self.runner), ("-w", self.project)):
            self.assertIn(["runuser", "-u", "alice", "--", "test", flag, str(path)], self.calls)

    def test_explicit_run_as_overrides_sudo_user(self):
        self.run_installer("--run-as", "bob")
        self.assert_installed(uid=4567, gid=5678, home=self.root / "bob-home")
        self.assertIn(["getent", "passwd", "bob"], self.calls)
        self.assertIn(["show", "user@4567.service", "--property=ActiveState", "--value"], self.ctl_calls())

    def test_without_sudo_user_uses_current_account(self):
        env = dict(self.env)
        del env["SUDO_USER"]
        self.configure(current_user="alice")
        self.run_installer(env=env)
        self.assertIn(["id", "-un"], self.calls)
        self.assert_installed()

    def test_install_and_remove_require_root_without_changes(self):
        self.seed_units(self.units)
        self.seed_units(self.legacy, links=True)
        before = self.snapshot()
        self.configure(euid=1234)
        for args in ((), ("--remove",)):
            with self.subTest(args=args):
                result = self.run_installer(*args, success=False)
                self.assertIn("requires root", result.stderr)
                self.assertEqual(before, self.snapshot())
                self.assert_no_changes()
                self.assertEqual(self.ctl_calls(), [])

    def test_show_is_read_only_without_root_valid_config_or_account(self):
        self.seed_units(self.units)
        self.config.write_text("not json")
        self.configure(euid=1234, status_exit=3)
        before = self.snapshot()
        self.run_installer("--show", "--run-as", "missing-account")
        self.assertEqual(self.calls, [["systemctl", "--no-pager", "status", *UNITS]])
        self.assertEqual(before, self.snapshot())
        self.assert_no_changes()

    def test_remove_stops_and_disables_all_three_and_preserves_user_data(self):
        self.seed_units(self.units)
        self.seed_units(self.legacy, links=True)
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
        self.assert_no_user_bus()
        self.assertFalse(any(call[0] in ("getent", "runuser", "systemd-analyze") for call in self.calls))

    def test_remove_missing_units_is_idempotent(self):
        for _ in range(2):
            self.run_installer("--remove")
            self.assertFalse(any(call[0] in ("stop", "disable", "enable") for call in self.ctl_calls()))
            self.assertEqual(list(self.units.iterdir()), [])

    def exercise_active_migration(self, fallback=False):
        self.seed_units(self.legacy, links=True)
        (self.legacy / "unrelated.service").write_text("unrelated")
        self.configure(manager="active", machine_bus_failure=fallback)
        self.run_installer()
        self.assert_installed()
        user_calls = self.ctl_calls(user=True)
        self.assertEqual([call for call in user_calls if call[0] == "stop"], [["stop", unit] for unit in UNITS])
        for unit in UNITS:
            self.assertIn(["show", unit, "--property=ActiveState", "--value"], user_calls)
            self.assertFalse(os.path.lexists(self.legacy / unit))
            for target in ("default.target.wants", "fixture.target.requires"):
                self.assertFalse(os.path.lexists(self.legacy / target / unit))
        self.assertEqual((self.legacy / "unrelated.service").read_text(), "unrelated")
        starts = [i for i, call in enumerate(self.calls) if call[0] == "systemctl" and "enable" in call]
        stops = [i for i, call in enumerate(self.calls) if call[0] == "systemctl" and "--user" in call and "stop" in call]
        self.assertLess(max(stops), min(starts))
        self.assertIn(["daemon-reload"], user_calls)
        self.assertEqual(any(call[0] == "runuser" and "env" in call for call in self.calls), fallback)

    def test_active_legacy_migration_stops_before_system_start_and_removes_links(self):
        self.exercise_active_migration()

    def test_active_migration_falls_back_to_existing_user_bus(self):
        self.exercise_active_migration(fallback=True)

    def test_active_loaded_only_legacy_units_are_stopped(self):
        self.configure(manager="active", user_loaded=list(UNITS), fragments={unit: "" for unit in UNITS})
        self.run_installer()
        self.assertEqual([call for call in self.ctl_calls(user=True) if call[0] == "stop"],
                         [["stop", unit] for unit in UNITS])
        self.assert_installed()

    def test_active_user_with_unloaded_legacy_files_migrates_without_stops(self):
        self.seed_units(self.legacy, links=True)
        self.configure(manager="active", user_not_found=list(UNITS))
        self.run_installer()
        self.assert_installed()
        self.assertFalse(any(call[0] == "stop" for call in self.ctl_calls(user=True)))
        self.assertIn(["daemon-reload"], self.ctl_calls(user=True))
        for unit in UNITS:
            self.assertFalse(os.path.lexists(self.legacy / unit))
            for target in ("default.target.wants", "fixture.target.requires"):
                self.assertFalse(os.path.lexists(self.legacy / target / unit))

    def test_inactive_file_only_migration_does_not_contact_user_bus(self):
        for manager in ("inactive", "failed"):
            with self.subTest(manager=manager):
                self.seed_units(self.legacy, links=True)
                self.configure(manager=manager)
                self.run_installer()
                self.assert_installed()
                self.assert_no_user_bus()
                for unit in UNITS:
                    self.assertFalse(os.path.lexists(self.legacy / unit))
                    for target in ("default.target.wants", "fixture.target.requires"):
                        self.assertFalse(os.path.lexists(self.legacy / target / unit))

    def test_running_user_bus_failure_fails_closed_even_without_legacy_files(self):
        self.configure(manager="active", machine_bus_failure=True, bus_failure=True)
        for with_files in (False, True):
            with self.subTest(with_files=with_files):
                if with_files:
                    self.seed_units(self.legacy, links=True)
                    self.seed_units(self.units)
                before = self.snapshot()
                result = self.run_installer(success=False)
                self.assertIn("avoid duplicate jobs", result.stderr)
                self.assertEqual(before, self.snapshot())
                self.assert_no_changes()

    def test_unknown_or_uninspectable_user_manager_fails_closed(self):
        self.seed_units(self.legacy, links=True)
        before = self.snapshot()
        for settings in ({"manager": "activating"}, {"manager": "deactivating"},
                         {"manager": "unknown"}, {"fail_calls": [
                             ["systemctl", "show", "user@1234.service", "--property=ActiveState", "--value"]]}):
            with self.subTest(settings=settings):
                self.configure(manager="inactive", fail_calls=[])
                self.configure(**settings)
                self.run_installer(success=False)
                self.assertEqual(before, self.snapshot())
                self.assert_no_changes()

    def test_legacy_stop_failure_keeps_files_and_never_starts_system_units(self):
        self.seed_units(self.legacy, links=True)
        self.seed_units(self.units)
        before = self.snapshot()
        for unit in UNITS:
            with self.subTest(unit=unit):
                self.configure(manager="active", stop_failures=[["user", unit]], user_stopped=[])
                result = self.run_installer(success=False)
                self.assertIn("Could not stop legacy " + unit, result.stderr)
                self.assertEqual(before, self.snapshot())
                self.assertFalse(any(call[0] in ("stop", "disable", "enable", "daemon-reload") for call in self.ctl_calls()))
                self.assertNotIn(["daemon-reload"], self.ctl_calls(user=True))

    def test_legacy_stop_must_be_verified_inactive(self):
        self.seed_units(self.legacy, links=True)
        before = self.snapshot()
        self.configure(manager="active", active_after_stop={TIMER: "active"})
        result = self.run_installer(success=False)
        self.assertIn("is still active", result.stderr)
        self.assertEqual(before, self.snapshot())
        self.assertFalse(any(call[0] in ("enable", "stop", "disable") for call in self.ctl_calls()))

    def test_system_stop_failure_prevents_overwrite_or_remove(self):
        self.seed_units(self.units)
        before = self.snapshot()
        for args in ((), ("--remove",)):
            for unit in UNITS:
                with self.subTest(args=args, unit=unit):
                    self.configure(stop_failures=[["system", unit]])
                    self.run_installer(*args, success=False)
                    self.assertEqual(before, self.snapshot())
                    self.assertFalse(any(call[0] in ("enable", "daemon-reload") for call in self.ctl_calls()))

    def test_unknown_units_symlinks_and_dropins_are_preserved(self):
        custom = self.root / "custom.service"
        custom.write_text("custom outside selected unit directories")
        for directory in (self.units, self.legacy):
            for kind in ("unknown", "wrong-timer", "wrong-exec", "symlink", "dropin"):
                with self.subTest(directory=directory, kind=kind):
                    unit = TIMER if kind == "wrong-timer" else CHECKIN
                    path = directory / (unit + ".d" if kind == "dropin" else unit)
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
                    self.run_installer(success=False)
                    self.assertEqual(before, self.snapshot())
                    self.assert_no_changes()
                    if directory == self.units:
                        self.run_installer("--remove", success=False)
                        self.assertEqual(before, self.snapshot())
                        self.assert_no_changes()
                    if path.is_dir() and not path.is_symlink():
                        shutil.rmtree(path)
                    else:
                        path.unlink()
        self.assertEqual(custom.read_text(), "custom outside selected unit directories")

    def test_custom_legacy_enablement_entries_fail_closed(self):
        self.seed_units(self.legacy)
        target = self.legacy / "default.target.wants"
        target.mkdir()
        link = target / TIMER
        custom = self.legacy / "custom.timer"
        custom.write_text("custom timer")
        for kind in ("regular-file", "wrong-target", "dangling-wrong-target"):
            with self.subTest(kind=kind):
                if kind == "regular-file":
                    link.write_text("not a symlink")
                else:
                    link.symlink_to(custom if kind == "wrong-target" else self.legacy / "missing.timer")
                before = self.snapshot()
                self.run_installer(success=False)
                self.assertEqual(before, self.snapshot())
                self.assert_no_changes()
                link.unlink()

    def test_loaded_legacy_custom_fragment_or_dropins_fail_closed(self):
        self.seed_units(self.legacy, links=True)
        before = self.snapshot()
        for settings in ({"fragments": {CHECKIN: str(self.root / "custom.service")}},
                         {"dropins": {CHECKIN: str(self.legacy / "custom.conf")}}):
            with self.subTest(settings=settings):
                self.configure(manager="active", fragments={}, dropins={})
                self.configure(**settings)
                self.run_installer(success=False)
                self.assertEqual(before, self.snapshot())
                self.assert_no_changes()

    def test_invalid_config_never_changes_existing_services(self):
        self.seed_units(self.units)
        self.seed_units(self.legacy, links=True)
        self.configure(manager="active")
        for text in ("{invalid", "[]", '{"timezone":"Not/A_Zone"}',
                     '{"telegram":[]}', '{"timezone":null}'):
            with self.subTest(config=text):
                self.config.write_text(text)
                before = self.snapshot()
                result = self.run_installer(success=False)
                self.assertIn("Configuration validation failed", result.stderr)
                self.assertEqual(before, self.snapshot())
                self.assert_no_changes()
                self.assert_no_user_bus()

    def test_invalid_calendar_never_changes_existing_services(self):
        self.seed_units(self.units)
        self.seed_units(self.legacy, links=True)
        self.configure(manager="active", bad_calendar=True)
        before = self.snapshot()
        result = self.run_installer("--on-calendar", "not a calendar", success=False)
        self.assertIn("Invalid calendar", result.stderr)
        self.assertIn(["systemd-analyze", "calendar", "not a calendar Asia/Taipei"], self.calls)
        self.assertEqual(before, self.snapshot())
        self.assert_no_changes()

    def test_missing_config_and_runtime_permission_failures_are_non_destructive(self):
        self.seed_units(self.units)
        self.seed_units(self.legacy, links=True)
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
        self.seed_units(self.legacy, links=True)
        (self.project / "telegram-state.json").write_text("fixture state")
        (self.home / "browser-profile").mkdir()
        (self.home / "browser-profile/Cookies").write_text("fixture cookies")
        (self.legacy / "unrelated.service").write_text("custom user service")
        (self.units / "unrelated.service").write_text("custom system service")
        preserved = {path: path.read_bytes() for path in (
            self.config, self.project / "telegram-state.json", self.home / "browser-profile/Cookies",
            self.legacy / "unrelated.service", self.units / "unrelated.service",
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

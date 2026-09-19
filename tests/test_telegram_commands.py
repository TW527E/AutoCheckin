import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

import telegram_bot

import agentrouter_checkin as app


class RecordingNotifier(telegram_bot.TelegramNotifier):
    """Notifier that records replies instead of talking to Telegram."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.replies: list[tuple[str, str]] = []

    def _reply(self, chat_id, text, reply_markup=None):
        self.replies.append((str(chat_id), text))

    def _answer_callback(self, callback_id, text, show_alert=False):
        pass


class CommandTestCase(unittest.TestCase):
    chat_id = "-100999"

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.directory = Path(self._temp.name)

    def make_notifier(self, checkin_trigger=None, token="123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"):
        config = {"telegram": {"bot_token": token, "chat_id": self.chat_id}}
        return RecordingNotifier(config, self.directory / "telegram_state.json", checkin_trigger)

    def send_command(self, notifier, text, chat_id=None):
        notifier._handle_message({"chat": {"id": chat_id or self.chat_id}, "text": text})
        return notifier.replies

    def replies_for(self, notifier, text):
        return " ".join(reply for _, reply in self.send_command(notifier, text))


class TestCommandTests(CommandTestCase):
    def test_test_command_reports_the_enabled_notifications(self):
        notifier = self.make_notifier()
        notifier.state["notifications"].update({"success": True, "error": False, "skipped": False})
        replies = self.send_command(notifier, "/test")
        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0][0], self.chat_id)
        self.assertIn("Telegram 通知測試", replies[0][1])
        self.assertIn("簽到成功", replies[0][1])
        self.assertNotIn("錯誤", replies[0][1])

    def test_test_command_is_delivered_even_with_every_notification_off(self):
        notifier = self.make_notifier()
        notifier.state["notifications"] = dict.fromkeys(telegram_bot.NOTIFICATION_TYPES, False)
        text = self.replies_for(notifier, "/test")
        self.assertIn("Telegram 通知測試", text)
        self.assertIn("全部關閉", text)

    def test_admin_chat_also_gets_the_notification_target_verified(self):
        notifier = self.make_notifier()
        notifier.admin_chat_ids = {"4242"}
        replies = self.send_command(notifier, "/test", chat_id="4242")
        self.assertEqual(sorted(chat for chat, _ in replies), sorted(["4242", self.chat_id]))

    def test_commands_accept_the_bot_suffix_and_alias(self):
        notifier = self.make_notifier()
        self.assertIn("Telegram 通知測試", self.replies_for(notifier, "/test@MyCheckinBot"))
        self.assertIn("Telegram 通知測試", self.replies_for(notifier, "/ping"))

    def test_unlisted_chats_are_ignored(self):
        notifier = self.make_notifier()
        self.assertEqual(self.send_command(notifier, "/test", chat_id="-555"), [])

    def test_help_lists_the_checkin_commands(self):
        notifier = self.make_notifier()
        text = self.replies_for(notifier, "/help")
        self.assertIn("/checkin force", text)

    def test_commands_are_registered_with_telegram(self):
        self.assertEqual([command["command"] for command in telegram_bot.BOT_COMMANDS][:2], ["checkin", "test"])


class CheckinCommandTests(CommandTestCase):
    def test_checkin_command_runs_without_force_by_default(self):
        calls = []
        notifier = self.make_notifier(checkin_trigger=lambda force: calls.append(force) or "started")
        text = self.replies_for(notifier, "/checkin")
        self.assertEqual(calls, [False])
        self.assertIn("started", text)

    def test_force_argument_is_forwarded(self):
        calls = []
        notifier = self.make_notifier(checkin_trigger=lambda force: calls.append(force) or "started")
        for argument in ("force", "FORCE", "--force", "-f"):
            with self.subTest(argument=argument):
                self.replies_for(notifier, f"/checkin {argument}")
        self.assertEqual(calls, [True, True, True, True])

    def test_unknown_argument_shows_usage_without_running(self):
        calls = []
        notifier = self.make_notifier(checkin_trigger=lambda force: calls.append(force) or "started")
        text = self.replies_for(notifier, "/checkin tomorrow")
        self.assertEqual(calls, [])
        self.assertIn("未知的參數：tomorrow", text)

    def test_trigger_error_is_reported(self):
        def failing_trigger(force):
            raise RuntimeError("no browser")

        notifier = self.make_notifier(checkin_trigger=failing_trigger)
        self.assertIn("無法啟動簽到：no browser", self.replies_for(notifier, "/checkin"))

    def test_without_a_trigger_the_command_explains_itself(self):
        notifier = self.make_notifier(checkin_trigger=None)
        self.assertIn("未啟用簽到觸發功能", self.replies_for(notifier, "/checkin"))


class CheckinTriggerTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.directory = Path(self._temp.name)
        self.config = copy.deepcopy(app.DEFAULT_CONFIG)
        self.config["profile_dir"] = self.directory
        self.state_path = self.directory / "telegram_state.json"

    def write_state(self, value: str) -> None:
        app.checkin_state_path(self.config).write_text(value + "\n", encoding="utf-8")

    def test_records_the_last_sign_in_date(self):
        self.assertIsNone(app.read_checkin_state(self.config))
        self.write_state("2026-09-18")
        self.assertEqual(app.read_checkin_state(self.config), "2026-09-18")

    def test_already_checked_in_today_is_reported_without_running(self):
        app.checkin_state_path(self.config).parent.mkdir(parents=True, exist_ok=True)
        self.write_state(app.datetime.now(app.ZoneInfo(self.config["timezone"])).date().isoformat())
        trigger = app.CheckinTrigger(self.config, self.state_path)
        with patch.object(app, "checkin") as checkin:
            message = trigger(False)
        self.assertIn("已經簽到", message)
        checkin.assert_not_called()

    def test_force_ignores_todays_record(self):
        self.write_state(app.datetime.now(app.ZoneInfo(self.config["timezone"])).date().isoformat())
        trigger = app.CheckinTrigger(self.config, self.state_path)
        with patch.object(app, "checkin", return_value=0) as checkin:
            message = trigger(True)
        self.assertIn("已開始執行簽到", message)
        trigger.thread.join(timeout=10)
        checkin.assert_called_once()
        self.assertTrue(checkin.call_args.args[1])

    def test_a_running_checkin_is_not_started_twice(self):
        trigger = app.CheckinTrigger(self.config, self.state_path)
        running = MagicMock()
        running.is_alive.return_value = True
        trigger.thread = running
        with patch.object(app, "checkin") as checkin:
            message = trigger(False)
        self.assertIn("仍在執行中", message)
        checkin.assert_not_called()
        self.assertIs(trigger.thread, running)

    def test_trigger_uses_its_own_notifier(self):
        trigger = app.CheckinTrigger(self.config, self.state_path)
        captured = {}

        def capture(config, force, notifier):
            captured["notifier"] = notifier
            return 0

        with patch.object(app, "checkin", side_effect=capture):
            trigger(False)
            trigger.thread.join(timeout=10)
        self.assertEqual(captured["notifier"].state_path, self.state_path)

    def test_triggered_checkin_failure_is_reported_on_stderr(self):
        trigger = app.CheckinTrigger(self.config, self.state_path)
        output = io.StringIO()
        with patch.object(app, "checkin", side_effect=RuntimeError("boom")), redirect_stderr(output):
            trigger(False)
            trigger.thread.join(timeout=10)
        self.assertIn("boom", output.getvalue())


class CheckinStateReuseTests(unittest.TestCase):
    def test_checkin_skips_when_today_is_recorded(self):
        with tempfile.TemporaryDirectory() as directory:
            config = copy.deepcopy(app.DEFAULT_CONFIG)
            config["profile_dir"] = directory
            today = app.datetime.now(app.ZoneInfo(config["timezone"])).date().isoformat()
            app.checkin_state_path(config).write_text(today + "\n", encoding="utf-8")
            output = io.StringIO()
            with redirect_stdout(output), patch("playwright.sync_api.sync_playwright") as playwright:
                self.assertEqual(app.checkin(config, force=False), 0)
            playwright.assert_not_called()
            self.assertIn("Already checked in today", output.getvalue())


if __name__ == "__main__":
    unittest.main()

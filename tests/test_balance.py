import copy
import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

from playwright.sync_api import Error as PlaywrightError

import agentrouter_checkin as app


def make_page(url: str = "https://agentrouter.org/console", evaluate_result=None) -> MagicMock:
    page = MagicMock()
    page.url = url
    if evaluate_result is not None:
        page.evaluate.return_value = evaluate_result
    return page


class FormatQuotaTests(unittest.TestCase):
    def test_currency_display_matches_the_site_rounding(self):
        self.assertEqual(app.format_quota(25_000_000, 500_000, True), "$50.00")
        self.assertEqual(app.format_quota(12_500_000, 500_000, True), "$25.00")
        self.assertEqual(app.format_quota(1_234_567, 500_000, True), "$2.47")

    def test_raw_display_uses_the_site_suffixes(self):
        for quota, expected in ((2_500_000_000, "2.5B"), (25_000_000, "25.0M"), (25_000, "25.0k"), (1_234, "1234")):
            with self.subTest(quota=quota):
                self.assertEqual(app.format_quota(quota, 500_000, False), expected)

    def test_currency_display_falls_back_when_the_unit_is_unusable(self):
        self.assertEqual(app.format_quota(1_234, 0, True), "1234")


class ReadBalanceTests(unittest.TestCase):
    payload = {"quota": 12_500_000, "quota_per_unit": 500_000, "display_in_currency": True}

    def test_reports_the_balance_from_the_site_api(self):
        self.assertEqual(app.read_balance(make_page(evaluate_result=self.payload)), "$25.00")

    def test_uses_the_default_unit_when_status_is_unavailable(self):
        payload = {"quota": 12_500_000, "quota_per_unit": None, "display_in_currency": None}
        self.assertEqual(app.read_balance(make_page(evaluate_result=payload)), "$25.00")

    def test_ignores_a_non_numeric_quota(self):
        with redirect_stderr(io.StringIO()) as error:
            self.assertIsNone(app.read_balance(make_page(evaluate_result={"quota": "25"})))
        self.assertIn("no usable quota", error.getvalue())

    def test_reports_and_ignores_an_api_failure(self):
        page = make_page()
        page.evaluate.side_effect = PlaywrightError("net::ERR_FAILED")
        with redirect_stderr(io.StringIO()) as error:
            self.assertIsNone(app.read_balance(page))
        self.assertIn("could not read the account balance", error.getvalue())

    def test_skips_pages_outside_the_site(self):
        page = make_page("https://github.com/login")
        self.assertIsNone(app.read_balance(page))
        page.evaluate.assert_not_called()


class BalanceScriptTests(unittest.TestCase):
    def test_requests_are_signed_with_the_stored_account_id(self):
        # The server answers 401 without this header, so losing it would silently
        # drop the balance from every notification rather than fail loudly.
        self.assertIn("New-API-User", app.BALANCE_SCRIPT)
        self.assertIn('localStorage.getItem("user")', app.BALANCE_SCRIPT)


class CheckinBalanceTests(unittest.TestCase):
    payload = {"quota": 12_500_000, "quota_per_unit": 500_000, "display_in_currency": True}

    def run_checkin(self, page: MagicMock, notifier: MagicMock) -> int:
        with tempfile.TemporaryDirectory() as directory:
            config = copy.deepcopy(app.DEFAULT_CONFIG)
            config["profile_dir"] = directory
            with patch("playwright.sync_api.sync_playwright") as playwright, redirect_stdout(io.StringIO()):
                launch = playwright.return_value.__enter__.return_value.chromium.launch_persistent_context
                launch.return_value.pages = [page]
                return app.checkin(config, force=True, notifier=notifier)

    def test_existing_session_notification_includes_the_balance(self):
        notifier = MagicMock()
        self.assertEqual(self.run_checkin(make_page(evaluate_result=self.payload), notifier), 0)
        notifier.send.assert_called_once()
        self.assertEqual(notifier.send.call_args.args[0], "success")
        message = notifier.send.call_args.args[1]
        self.assertIn("簽到成功（既有登入狀態）", message)
        self.assertIn("餘額：$25.00", message)

    def test_password_login_notification_includes_the_balance(self):
        notifier = MagicMock()
        page = make_page("https://agentrouter.org/login", evaluate_result=self.payload)
        with tempfile.TemporaryDirectory() as directory:
            config = copy.deepcopy(app.DEFAULT_CONFIG)
            config.update(login_method="password", username="user", password="secret", profile_dir=directory)
            with patch("playwright.sync_api.sync_playwright") as playwright, patch.object(
                app, "start_password_login"
            ), patch.object(app, "wait_until_signed_in", return_value=True), redirect_stdout(io.StringIO()):
                launch = playwright.return_value.__enter__.return_value.chromium.launch_persistent_context
                launch.return_value.pages = [page]
                self.assertEqual(app.checkin(config, force=True, notifier=notifier), 0)
        message = notifier.send.call_args.args[1]
        self.assertIn("登入方式：password", message)
        self.assertIn("餘額：$25.00", message)

    def test_checkin_still_succeeds_when_the_balance_is_unavailable(self):
        notifier = MagicMock()
        page = make_page()
        page.evaluate.side_effect = PlaywrightError("net::ERR_FAILED")
        with redirect_stderr(io.StringIO()):
            self.assertEqual(self.run_checkin(page, notifier), 0)
        message = notifier.send.call_args.args[1]
        self.assertIn("簽到成功（既有登入狀態）", message)
        self.assertNotIn("餘額", message)


if __name__ == "__main__":
    unittest.main()

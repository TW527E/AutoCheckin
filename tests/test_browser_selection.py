import copy
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

from playwright.sync_api import Error as PlaywrightError

import agentrouter_checkin as app


class BrowserConfigTests(unittest.TestCase):
    def test_existing_config_defaults_to_chromium(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text('{}', encoding="utf-8")
            config = app.load_config(path)
        self.assertEqual(config["browser"], "chromium")
        app.validate_config(config)

    def test_generated_config_includes_browser(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            app.write_example_config(path)
            self.assertEqual(json.loads(path.read_text())["browser"], "chromium")

    def test_browser_validation(self):
        for browser in ("chrome", "chromium", "firefox", "", None, [], {}):
            with self.subTest(browser=browser):
                config = copy.deepcopy(app.DEFAULT_CONFIG)
                config["browser"] = browser
                if browser in ("chrome", "chromium"):
                    app.validate_config(config)
                else:
                    with self.assertRaisesRegex(ValueError, "browser must be"):
                        app.validate_config(config)

    def test_cli_rejects_unsupported_browser(self):
        with patch("sys.argv", ["checkin", "--browser", "firefox"]), redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as result:
                app.parse_args()
        self.assertEqual(result.exception.code, 2)

    def test_cli_precedence_and_headless_independence(self):
        cases = (
            ([], "chrome", False),
            (["--headless"], "chrome", True),
            (["--browser", "chromium"], "chromium", False),
            (["--browser", "chrome", "--no-headless"], "chrome", False),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({"browser": "chrome"}), encoding="utf-8")
            for extra, browser, headless in cases:
                with self.subTest(extra=extra), patch.dict(os.environ, {}, clear=True), patch(
                    "sys.argv", ["checkin", "--config", str(path), "--no-telegram-poll", *extra]
                ), patch.object(app, "TelegramNotifier"), patch.object(app, "checkin", return_value=0) as checkin:
                    self.assertEqual(app.main(), 0)
                    config = checkin.call_args.args[0]
                    self.assertEqual(config["browser"], browser)
                    self.assertEqual(config["headless"], headless)


class BrowserLaunchTests(unittest.TestCase):
    def test_launch_options_for_each_browser_and_mode(self):
        for browser in ("chrome", "chromium"):
            for headless in (False, True):
                with self.subTest(browser=browser, headless=headless), tempfile.TemporaryDirectory() as directory:
                    config = copy.deepcopy(app.DEFAULT_CONFIG)
                    config.update(browser=browser, headless=headless, profile_dir=directory)
                    with patch("playwright.sync_api.sync_playwright") as playwright, redirect_stdout(io.StringIO()):
                        launch = playwright.return_value.__enter__.return_value.chromium.launch_persistent_context
                        context = launch.return_value
                        page = MagicMock()
                        page.url = "https://agentrouter.org/console"
                        context.pages = [page]
                        self.assertEqual(app.checkin(config, force=True), 0)
                    expected = {
                        "user_data_dir": directory,
                        "headless": headless,
                        "viewport": {"width": 1280, "height": 900},
                    }
                    if browser == "chrome":
                        expected["channel"] = "chrome"
                    launch.assert_called_once_with(**expected)
                    context.close.assert_called_once()
                    self.assertTrue((Path(directory) / "last_checkin_date.txt").exists())

    def test_chrome_launch_failure_has_guidance_and_no_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            config = copy.deepcopy(app.DEFAULT_CONFIG)
            config.update(browser="chrome", profile_dir=directory)
            notifier = MagicMock()
            error = io.StringIO()
            with patch("playwright.sync_api.sync_playwright") as playwright, redirect_stderr(error):
                launch = playwright.return_value.__enter__.return_value.chromium.launch_persistent_context
                launch.side_effect = PlaywrightError("Chrome executable not found")
                self.assertEqual(app.checkin(config, force=True, notifier=notifier), 2)
            launch.assert_called_once()
            self.assertIn("Google Chrome", error.getvalue())
            self.assertIn("Chrome executable not found", error.getvalue())
            notifier.send.assert_called_once()
            self.assertEqual(notifier.send.call_args.args[0], "error")
            self.assertFalse((Path(directory) / "last_checkin_date.txt").exists())

    def test_chromium_failure_keeps_existing_exception_behavior(self):
        with tempfile.TemporaryDirectory() as directory:
            config = copy.deepcopy(app.DEFAULT_CONFIG)
            config["profile_dir"] = directory
            with patch("playwright.sync_api.sync_playwright") as playwright:
                launch = playwright.return_value.__enter__.return_value.chromium.launch_persistent_context
                launch.side_effect = PlaywrightError("Chromium executable not found")
                with self.assertRaisesRegex(PlaywrightError, "Chromium executable not found"):
                    app.checkin(config, force=True)
            launch.assert_called_once()


if __name__ == "__main__":
    unittest.main()

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from urllib.parse import parse_qsl, urlparse

import telegram_bot

TOKEN = "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class FakeOpener:
    """Stands in for the opener so tests can inspect the outgoing request."""

    def __init__(self, payload=None):
        self.payload = payload if payload is not None else {"ok": True, "result": {}}
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        return FakeResponse(self.payload)


class NotifierTestCase(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.directory = self._temp.name

    def make_notifier(self, token: str = TOKEN, directory: str | None = None, telegram: dict | None = None):
        config = {"telegram": {"bot_token": token, "chat_id": "-100123", **(telegram or {})}}
        state_path = Path(directory or self.directory) / "telegram_state.json"
        return telegram_bot.TelegramNotifier(config, state_path)

    @staticmethod
    def sent_chat_id(request) -> str:
        return dict(parse_qsl(request.data.decode()))["chat_id"]


class TokenValidationTests(NotifierTestCase):
    def test_token_cannot_carry_url_delimiters(self):
        for token in (
            "123456789:abc@evil.example",
            "123456789:abc/../../redirect",
            "123456789:abc?x=1",
            "123456789:abc#fragment",
            "1234 5678:abc",
            "https://evil.example/",
        ):
            with self.subTest(token=token):
                with self.assertRaisesRegex(ValueError, "must not contain URL delimiters"):
                    self.make_notifier(token)

    def test_an_empty_token_keeps_the_notifier_unconfigured(self):
        notifier = self.make_notifier("")
        self.assertFalse(notifier.configured)
        opener = FakeOpener()
        notifier.opener = opener
        self.assertFalse(notifier.send("success", "ignored"))
        self.assertEqual(opener.requests, [])


class RequestTargetTests(NotifierTestCase):
    def test_request_goes_to_the_botfather_token_endpoint(self):
        notifier = self.make_notifier()
        opener = FakeOpener()
        notifier.opener = opener
        self.assertTrue(notifier.send("success", "簽到成功"))
        self.assertEqual(
            opener.requests[0].full_url,
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        )
        self.assertEqual(opener.requests[0].get_method(), "POST")

    def test_no_token_can_move_the_request_off_the_telegram_host(self):
        # Bypasses the constructor check on purpose: whatever the token, the
        # request must stay on the one host this client may reach. The host is
        # fixed by the URL prefix, so a token can only ever change the path.
        for token in (
            "123456789:abc@evil.example",
            "123456789:abc/../../../@evil.example",
            "..",
            "@evil.example/x",
            "123456789:a?x=1",
            "123456789:a#f",
            "http://evil.example",
        ):
            with self.subTest(token=token):
                notifier = self.make_notifier()
                notifier.token = token
                opener = FakeOpener()
                notifier.opener = opener
                notifier.send("success", "hi")
                target = urlparse(opener.requests[0].full_url)
                self.assertEqual(target.scheme, "https")
                self.assertEqual(target.hostname, "api.telegram.org")


class ErrorRoutingTests(NotifierTestCase):
    def test_errors_route_to_the_error_chat_when_set(self):
        notifier = self.make_notifier(telegram={"error_chat_id": "555"})
        opener = FakeOpener()
        notifier.opener = opener
        self.assertTrue(notifier.send("error", "簽到失敗"))
        self.assertEqual(self.sent_chat_id(opener.requests[0]), "555")

    def test_success_still_goes_to_the_main_chat_when_error_chat_is_set(self):
        notifier = self.make_notifier(telegram={"error_chat_id": "555"})
        opener = FakeOpener()
        notifier.opener = opener
        self.assertTrue(notifier.send("success", "簽到成功"))
        self.assertEqual(self.sent_chat_id(opener.requests[0]), "-100123")

    def test_errors_fall_back_to_the_main_chat_without_an_error_chat(self):
        notifier = self.make_notifier()
        opener = FakeOpener()
        notifier.opener = opener
        self.assertTrue(notifier.send("error", "簽到失敗"))
        self.assertEqual(self.sent_chat_id(opener.requests[0]), "-100123")


class NotifierBehaviourTests(NotifierTestCase):
    def test_api_error_does_not_propagate_from_send(self):
        notifier = self.make_notifier()
        notifier.opener = FakeOpener({"ok": False, "description": "chat not found"})
        with redirect_stdout(io.StringIO()) as output:
            self.assertFalse(notifier.send("success", "簽到成功"))
        self.assertIn("chat not found", output.getvalue())


if __name__ == "__main__":
    unittest.main()

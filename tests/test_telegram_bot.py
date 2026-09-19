import io
import json
import socket
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request

import telegram_bot

TOKEN = "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"
PUBLIC_IP = "149.154.167.220"


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
        self.requests: list[Request] = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        return FakeResponse(self.payload)


def addrinfo(*addresses):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443)) for address in addresses]


class NotifierTestCase(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.directory = self._temp.name

    def make_notifier(self, token: str = TOKEN, directory: str | None = None):
        config = {"telegram": {"bot_token": token, "chat_id": "-100123"}}
        state_path = Path(directory or self.directory) / "telegram_state.json"
        return telegram_bot.TelegramNotifier(config, state_path)


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

    def test_request_is_refused_before_any_connection_when_the_url_is_foreign(self):
        # The token cannot produce this today because the URL prefix fixes the
        # host; the check is asserted here so it cannot silently stop guarding.
        notifier = self.make_notifier()
        opener = FakeOpener()
        notifier.opener = opener
        foreign = urlparse("https://evil.example/botx/sendMessage")
        with patch.object(telegram_bot, "urlparse", return_value=foreign):
            with self.assertRaisesRegex(ValueError, "Refusing to send"):
                notifier._request("sendMessage", {"chat_id": "-100123", "text": "hi"})
        self.assertEqual(opener.requests, [])


class AddressValidationTests(unittest.TestCase):
    def test_public_addresses_are_accepted(self):
        with patch.object(telegram_bot.socket, "getaddrinfo", return_value=addrinfo(PUBLIC_IP)):
            self.assertEqual(telegram_bot.public_addresses("api.telegram.org"), [PUBLIC_IP])

    def test_ipv4_is_preferred_over_ipv6(self):
        with patch.object(
            telegram_bot.socket, "getaddrinfo", return_value=addrinfo("2001:67c:4e8:f004::9", PUBLIC_IP)
        ):
            self.assertEqual(telegram_bot.public_addresses("api.telegram.org"), [PUBLIC_IP, "2001:67c:4e8:f004::9"])

    def test_non_public_addresses_are_refused(self):
        for address in ("127.0.0.1", "10.0.0.5", "192.168.1.10", "169.254.169.254", "::1", "fd00::1"):
            with self.subTest(address=address):
                with self.assertRaisesRegex(ValueError, "non-public address"):
                    with patch.object(telegram_bot.socket, "getaddrinfo", return_value=addrinfo(address)):
                        telegram_bot.public_addresses("api.telegram.org")

    def test_a_mixed_answer_is_refused(self):
        # A rebinding attempt that adds a private address to a public answer.
        with self.assertRaisesRegex(ValueError, "non-public address"):
            with patch.object(telegram_bot.socket, "getaddrinfo", return_value=addrinfo(PUBLIC_IP, "127.0.0.1")):
                telegram_bot.public_addresses("api.telegram.org")

    def test_resolution_failure_is_reported(self):
        with patch.object(telegram_bot.socket, "getaddrinfo", side_effect=socket.gaierror("nope")):
            with self.assertRaisesRegex(ValueError, "Cannot resolve"):
                telegram_bot.public_addresses("api.telegram.org")

    def test_connections_are_pinned_to_the_validated_addresses(self):
        with patch.object(telegram_bot, "public_addresses", return_value=[PUBLIC_IP]):
            connection = telegram_bot.PinnedHTTPSHandler._pinned("api.telegram.org", timeout=5)
        self.assertIsInstance(connection, telegram_bot.PinnedHTTPSConnection)
        self.assertEqual(connection.pinned_addresses, [PUBLIC_IP])
        self.assertEqual(connection.host, "api.telegram.org")

    def test_connect_falls_back_to_the_next_validated_address(self):
        connection = telegram_bot.PinnedHTTPSConnection("api.telegram.org", ["2001:67c:4e8:f004::9", PUBLIC_IP])
        connection._context = MagicMock()
        attempts = []

        def dial(target, timeout=None, source_address=None):
            attempts.append(target[0])
            if target[0] != PUBLIC_IP:
                raise OSError("network unreachable")
            return MagicMock()

        with patch.object(telegram_bot.socket, "create_connection", side_effect=dial):
            connection.connect()
        self.assertEqual(attempts, ["2001:67c:4e8:f004::9", PUBLIC_IP])

    def test_connect_reports_the_last_failure(self):
        connection = telegram_bot.PinnedHTTPSConnection("api.telegram.org", [PUBLIC_IP])
        connection._context = MagicMock()
        with patch.object(telegram_bot.socket, "create_connection", side_effect=OSError("unreachable")):
            with self.assertRaisesRegex(OSError, "unreachable"):
                connection.connect()

    def test_pinning_is_skipped_when_a_proxy_carries_the_request(self):
        handler = telegram_bot.PinnedHTTPSHandler()
        request = Request("https://api.telegram.org/botx/sendMessage")
        with patch.object(telegram_bot, "proxy_configured", return_value=True), patch.object(
            telegram_bot, "public_addresses", side_effect=AssertionError("must not resolve locally")
        ), patch.object(telegram_bot.HTTPSHandler, "https_open", return_value="delegated"):
            self.assertEqual(handler.https_open(request), "delegated")


class RedirectTests(unittest.TestCase):
    def request(self):
        return Request(f"https://api.telegram.org/bot{TOKEN}/sendMessage", data=b"x")

    def test_a_redirect_to_another_host_is_refused(self):
        with self.assertRaises(HTTPError) as raised:
            telegram_bot.SameHostRedirects().redirect_request(
                self.request(), None, 302, "Found", {}, "https://evil.example/collect"
            )
        raised.exception.close()

    def test_a_redirect_within_the_bot_api_host_is_followed(self):
        redirect = telegram_bot.SameHostRedirects().redirect_request(
            self.request(), None, 302, "Found", {}, f"https://api.telegram.org/bot{TOKEN}/getMe"
        )
        self.assertEqual(redirect.full_url, f"https://api.telegram.org/bot{TOKEN}/getMe")


class NotifierBehaviourTests(NotifierTestCase):
    def test_polling_is_skipped_when_commands_are_disabled(self):
        notifier = self.make_notifier()
        notifier.poll_enabled = False
        opener = FakeOpener()
        notifier.opener = opener
        notifier.poll_commands()
        self.assertEqual(opener.requests, [])

    def test_api_error_does_not_propagate_from_send(self):
        notifier = self.make_notifier()
        notifier.opener = FakeOpener({"ok": False, "description": "chat not found"})
        with redirect_stdout(io.StringIO()) as output:
            self.assertFalse(notifier.send("success", "簽到成功"))
        self.assertIn("chat not found", output.getvalue())


if __name__ == "__main__":
    unittest.main()

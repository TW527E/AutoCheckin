"""Telegram notifications and command handling for the check-in runner."""

from __future__ import annotations

import http.client
import ipaddress
import json
import os
import re
import socket
import stat
import time
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener, getproxies

TELEGRAM_API_HOST = "api.telegram.org"
TELEGRAM_API_PORT = 443
# The bot token is interpolated into the request path, so keep it free of URL
# delimiters; a token containing "@" or "/" would move the request elsewhere.
BOT_TOKEN_ALLOWED_CHARS = re.compile(r"\A[A-Za-z0-9_.:-]+\Z")


def public_addresses(host: str, port: int = TELEGRAM_API_PORT) -> list[str]:
    """Resolve host once and refuse anything outside the public internet."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError(f"Cannot resolve {host}: {exc}") from exc
    addresses = [ipaddress.ip_address(info[4][0]) for info in infos]
    if not addresses:
        raise ValueError(f"Refusing to contact {host}: it resolved to no address")
    for address in addresses:
        if not address.is_global:
            raise ValueError(f"Refusing to contact {host}: it resolves to the non-public address {address}")
    # IPv4 first, because it is the safer default where IPv6 is unroutable; the
    # connection still falls back to the remaining validated addresses.
    return sorted({str(address) for address in addresses}, key=lambda value: ":" in value)


def proxy_configured() -> bool:
    """True when a proxy carries the request, which makes it the egress point."""
    proxies = getproxies()
    return bool(proxies.get("https") or proxies.get("http"))


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection dialling addresses that were validated just before use.

    Pinning the checked addresses means a later DNS answer cannot move the
    connection between the check and the handshake, while the hostname still
    drives SNI and certificate verification.
    """

    def __init__(self, host: str, addresses: list[str], **kwargs: Any):
        super().__init__(host, **kwargs)
        self.pinned_addresses = addresses

    def connect(self) -> None:
        last_error: OSError | None = None
        for address in self.pinned_addresses:
            try:
                self.sock = socket.create_connection((address, self.port), self.timeout, self.source_address)
            except OSError as exc:
                last_error = exc
                continue
            self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)
            return
        raise last_error if last_error is not None else OSError(f"No validated address for {self.host}")


class PinnedHTTPSHandler(HTTPSHandler):
    """HTTPS handler whose connections are validated and pinned."""

    def https_open(self, req):
        if proxy_configured():
            # A proxy resolves the name itself and may legitimately be local, so
            # pinning would break it; the URL checks still apply either way.
            return super().https_open(req)
        return self.do_open(self._pinned, req)

    @staticmethod
    def _pinned(host: str, **kwargs: Any) -> PinnedHTTPSConnection:
        hostname = urlparse(f"//{host}").hostname or host
        return PinnedHTTPSConnection(hostname, public_addresses(hostname), **kwargs)


class SameHostRedirects(HTTPRedirectHandler):
    """Refuse a redirect that would carry the request to another host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlparse(newurl).hostname != TELEGRAM_API_HOST:
            raise HTTPError(newurl, code, "Refusing a redirect away from the Telegram API host", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def telegram_opener():
    """Opener limited to HTTPS requests to the pinned Telegram Bot API host."""
    return build_opener(PinnedHTTPSHandler(), SameHostRedirects())


NOTIFICATION_TYPES = ("success", "error", "skipped")
DEFAULT_NOTIFICATIONS = {"success": True, "error": True, "skipped": False}
NOTIFICATION_LABELS = {
    "success": "簽到成功",
    "error": "錯誤",
    "skipped": "例行跳過",
}
BOT_COMMANDS = [
    {"command": "checkin", "description": "立即執行簽到"},
    {"command": "test", "description": "發送測試通知"},
    {"command": "toggle", "description": "切換通知設定"},
    {"command": "status", "description": "查看通知狀態"},
    {"command": "help", "description": "顯示指令說明"},
]


class TelegramNotifier:
    def __init__(
        self,
        config: dict[str, Any],
        state_path: Path,
        checkin_trigger: Callable[[bool], str] | None = None,
    ):
        telegram = config.get("telegram", {})
        if not isinstance(telegram, dict):
            telegram = {}
        # Supplied by the runner so a Telegram command can start a check-in. It
        # returns the text to answer with and must not block the listener.
        self.checkin_trigger = checkin_trigger
        self.token = str(telegram.get("bot_token") or os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
        if self.token and not BOT_TOKEN_ALLOWED_CHARS.match(self.token):
            raise ValueError(
                "telegram.bot_token must not contain URL delimiters such as '/', '@', '?' or whitespace"
            )
        self.chat_id = str(telegram.get("chat_id") or os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
        # Errors and check-in failures go here instead of the main chat, so a
        # noisy group stays quiet and problems reach the operator's own DM.
        self.error_chat_id = str(telegram.get("error_chat_id") or os.environ.get("TELEGRAM_ERROR_CHAT_ID") or "").strip()
        self.admin_chat_ids = {str(value) for value in telegram.get("admin_chat_ids", [])}
        self.poll_enabled = bool(telegram.get("poll_commands", True))
        self.state_path = state_path.expanduser()
        self.opener = telegram_opener()
        self.state = self._load_state(telegram.get("notifications", {}))

    @property
    def configured(self) -> bool:
        return bool(self.token and self.chat_id)

    def _load_state(self, configured_notifications: Any) -> dict[str, Any]:
        notifications = dict(DEFAULT_NOTIFICATIONS)
        if isinstance(configured_notifications, dict):
            for name in NOTIFICATION_TYPES:
                if name in configured_notifications:
                    notifications[name] = bool(configured_notifications[name])
        state: dict[str, Any] = {"notifications": notifications, "update_offset": 0}
        if not self.state_path.exists():
            return state
        try:
            loaded = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            print(f"Warning: could not read Telegram state file {self.state_path}")
            return state
        if not isinstance(loaded, dict):
            return state
        saved_notifications = loaded.get("notifications")
        if isinstance(saved_notifications, dict):
            for name in NOTIFICATION_TYPES:
                if name in saved_notifications:
                    state["notifications"][name] = bool(saved_notifications[name])
        if isinstance(loaded.get("update_offset"), int) and loaded["update_offset"] >= 0:
            state["update_offset"] = loaded["update_offset"]
        return state

    def _save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(self.state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if os.name != "nt":
            self.state_path.chmod(stat.S_IRUSR | stat.S_IWUSR)

    def _request(self, method: str, payload: dict[str, Any], timeout: int = 15) -> Any:
        body = urlencode({key: json.dumps(value) if isinstance(value, (list, dict)) else value for key, value in payload.items()}).encode()
        # The token is percent-encoded so it stays inside its own path segment
        # (":" stays literal because Telegram expects the raw "id:secret" form),
        # and the result is checked against the one host this client may reach.
        url = f"https://{TELEGRAM_API_HOST}/bot{quote(self.token, safe=':')}/{method}"
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname != TELEGRAM_API_HOST:
            raise ValueError(f"Refusing to send a Telegram request to {url}")
        request = Request(
            url,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": "AutoCheckin/1.0"},
        )
        with self.opener.open(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
        if not result.get("ok"):
            raise RuntimeError(result.get("description", f"Telegram API call failed: {method}"))
        return result.get("result")

    def send(self, notification_type: str, text: str) -> bool:
        if not self.configured or notification_type not in NOTIFICATION_TYPES:
            return False
        if not self.state["notifications"].get(notification_type, False):
            return False
        prefix = {"success": "✅", "error": "❌"}.get(notification_type)
        if prefix:
            text = f"{prefix} {text}"
        target_chat_id = self.error_chat_id if notification_type == "error" and self.error_chat_id else self.chat_id
        try:
            self._request("sendMessage", {"chat_id": target_chat_id, "text": text})
            return True
        except Exception as exc:
            print(f"Warning: Telegram notification failed: {exc}")
            return False

    def register_commands(self) -> None:
        try:
            self._request("setMyCommands", {"commands": BOT_COMMANDS})
            self._request("setChatMenuButton", {"menu_button": {"type": "commands"}})
        except Exception as exc:
            print(f"Warning: Telegram command registration failed: {exc}")

    def poll_commands(self, timeout: int = 0, force: bool = False) -> None:
        if not self.configured or (not self.poll_enabled and not force):
            return
        updates = self._request(
            "getUpdates",
            {
                "offset": self.state["update_offset"],
                "timeout": timeout,
                "allowed_updates": ["message", "channel_post", "callback_query"],
            },
            timeout=max(15, timeout + 15),
        )
        changed = False
        for update in updates or []:
            if isinstance(update, dict) and isinstance(update.get("update_id"), int):
                self.state["update_offset"] = update["update_id"] + 1
                changed = True
            message = (update.get("message") or update.get("channel_post")) if isinstance(update, dict) else None
            if isinstance(message, dict):
                self._handle_message(message)
            callback_query = update.get("callback_query") if isinstance(update, dict) else None
            if isinstance(callback_query, dict):
                self._handle_callback_query(callback_query)
        if changed:
            self._save_state()

    def _handle_message(self, message: dict[str, Any]) -> None:
        chat = message.get("chat") or {}
        source_chat_id = str(chat.get("id", ""))
        if source_chat_id != self.chat_id and source_chat_id not in self.admin_chat_ids:
            return
        text = str(message.get("text") or "").strip()
        if not text.startswith("/"):
            return
        parts = text.split()
        command = parts[0].split("@", 1)[0].lower()
        if command == "/toggle":
            self._show_notification_menu(source_chat_id)
        elif command == "/status":
            self._show_notification_menu(source_chat_id)
        elif command == "/checkin":
            self._handle_checkin_request(source_chat_id, parts[1:])
        elif command in {"/test", "/ping"}:
            self._send_test_notification(source_chat_id)
        elif command in {"/start", "/help"}:
            self._reply(source_chat_id, self.help_text())

    def _handle_checkin_request(self, chat_id: str, arguments: list[str]) -> None:
        if self.checkin_trigger is None:
            self._reply(chat_id, "此服務未啟用簽到觸發功能，請直接執行簽到程式。")
            return
        force = False
        for argument in arguments:
            if argument.lower() in {"force", "--force", "-f"}:
                force = True
            else:
                self._reply(chat_id, f"未知的參數：{argument}\n用法：/checkin 或 /checkin force")
                return
        try:
            self._reply(chat_id, self.checkin_trigger(force))
        except Exception as exc:
            self._reply(chat_id, f"❌ 無法啟動簽到：{exc}")

    def _send_test_notification(self, chat_id: str) -> None:
        # Delivered regardless of the notification toggles, because the point is
        # to prove the delivery path works.
        message = self.test_message()
        if chat_id != self.chat_id:
            self._reply(self.chat_id, message)
        self._reply(chat_id, message)

    def test_message(self) -> str:
        enabled = [
            NOTIFICATION_LABELS[name]
            for name in NOTIFICATION_TYPES
            if self.state["notifications"].get(name, False)
        ]
        return (
            "🔔 Telegram 通知測試\n"
            "看到這則訊息代表 Bot 可以正常發送通知。\n"
            f"目前已開啟的通知：{'、'.join(enabled) if enabled else '（全部關閉）'}"
        )

    def _show_notification_menu(self, chat_id: str) -> None:
        self._reply(chat_id, "通知狀態（點擊按鈕切換）：", self._notification_keyboard())

    def _notification_keyboard(self) -> dict[str, Any]:
        notifications = self.state["notifications"]
        rows = []
        for notification_type in NOTIFICATION_TYPES:
            state = "✅" if notifications.get(notification_type, False) else "❌"
            rows.append(
                [
                    {
                        "text": f"{state} {NOTIFICATION_LABELS[notification_type]}",
                        "callback_data": f"toggle:{notification_type}",
                    }
                ]
            )
        all_enabled = all(notifications.get(name, False) for name in NOTIFICATION_TYPES)
        rows.append(
            [
                {
                    "text": f"{'✅' if all_enabled else '❌'} 全部通知",
                    "callback_data": "toggle:all",
                }
            ]
        )
        return {"inline_keyboard": rows}

    def _handle_callback_query(self, callback_query: dict[str, Any]) -> None:
        callback_id = str(callback_query.get("id") or "")
        message = callback_query.get("message") or {}
        chat = message.get("chat") or {}
        source_chat_id = str(chat.get("id", ""))
        sender_id = str((callback_query.get("from") or {}).get("id", ""))
        if not self._is_authorized(source_chat_id, sender_id):
            self._answer_callback(callback_id, "沒有權限變更通知設定。", show_alert=True)
            return
        data = str(callback_query.get("data") or "")
        parts = data.split(":")
        if len(parts) != 2 or parts[0] != "toggle":
            self._answer_callback(callback_id, "無效的操作。", show_alert=True)
            return
        notification_type = parts[1]
        if notification_type != "all" and notification_type not in NOTIFICATION_TYPES:
            self._answer_callback(callback_id, "未知的通知類型。", show_alert=True)
            return
        if notification_type == "all":
            enabled = not all(self.state["notifications"].get(name, False) for name in NOTIFICATION_TYPES)
            for name in NOTIFICATION_TYPES:
                self.state["notifications"][name] = enabled
        else:
            enabled = not self.state["notifications"].get(notification_type, False)
            self.state["notifications"][notification_type] = enabled
        self._save_state()
        changed_name = "全部通知" if notification_type == "all" else NOTIFICATION_LABELS[notification_type]
        state = "已開啟" if enabled else "已關閉"
        self._answer_callback(callback_id, f"{state} {changed_name}")
        message_id = message.get("message_id")
        if source_chat_id and isinstance(message_id, int):
            try:
                self._request(
                    "editMessageText",
                    {
                        "chat_id": source_chat_id,
                        "message_id": message_id,
                        "text": "通知狀態（點擊按鈕切換）：",
                        "reply_markup": self._notification_keyboard(),
                    },
                )
            except Exception as exc:
                print(f"Warning: Telegram menu update failed: {exc}")

    def _is_authorized(self, chat_id: str, sender_id: str = "") -> bool:
        return chat_id == self.chat_id or chat_id in self.admin_chat_ids or sender_id in self.admin_chat_ids

    def _answer_callback(self, callback_id: str, text: str, show_alert: bool = False) -> None:
        if not callback_id:
            return
        try:
            self._request(
                "answerCallbackQuery",
                {"callback_query_id": callback_id, "text": text, "show_alert": show_alert},
            )
        except Exception as exc:
            print(f"Warning: Telegram callback reply failed: {exc}")

    def _reply(self, chat_id: str, text: str, reply_markup: dict[str, Any] | None = None) -> None:
        if not self.configured:
            return
        try:
            payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
            if reply_markup is not None:
                payload["reply_markup"] = reply_markup
            self._request("sendMessage", payload)
        except Exception as exc:
            print(f"Warning: Telegram command reply failed: {exc}")

    def help_text(self) -> str:
        return (
            "指令說明：\n"
            "/checkin 立即執行簽到（今日已簽到會跳過）\n"
            "/checkin force 忽略今日紀錄，強制重新簽到\n"
            "/test 發送測試通知\n"
            "/toggle 顯示並切換通知\n"
            "/status 顯示目前狀態\n"
            "/help 顯示此說明"
        )

    def listen_forever(self) -> None:
        if not self.configured:
            raise ValueError("Telegram bot_token and chat_id are required for --telegram-listen")
        self.register_commands()
        print("Telegram command listener started. Press Ctrl-C to stop.")
        try:
            while True:
                try:
                    self.poll_commands(timeout=30, force=True)
                except Exception as exc:
                    print(f"Warning: Telegram command polling failed: {exc}")
                    time.sleep(5)
        except KeyboardInterrupt:
            print("Telegram command listener stopped.")

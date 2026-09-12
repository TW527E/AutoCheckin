"""Telegram notifications and command handling for the check-in runner."""

from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

NOTIFICATION_TYPES = ("success", "error", "skipped")
DEFAULT_NOTIFICATIONS = {"success": True, "error": True, "skipped": False}
NOTIFICATION_LABELS = {
    "success": "簽到成功",
    "error": "錯誤",
    "skipped": "例行跳過",
}
BOT_COMMANDS = [
    {"command": "toggle", "description": "切換通知設定"},
    {"command": "status", "description": "查看通知狀態"},
    {"command": "help", "description": "顯示指令說明"},
]


class TelegramNotifier:
    def __init__(self, config: dict[str, Any], state_path: Path):
        telegram = config.get("telegram", {})
        if not isinstance(telegram, dict):
            telegram = {}
        self.token = str(telegram.get("bot_token") or os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
        self.chat_id = str(telegram.get("chat_id") or os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
        self.admin_chat_ids = {str(value) for value in telegram.get("admin_chat_ids", [])}
        self.poll_enabled = bool(telegram.get("poll_commands", True))
        self.state_path = state_path.expanduser()
        self.api_url = f"https://api.telegram.org/bot{self.token}"
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
        request = Request(
            f"{self.api_url}/{method}",
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": "AutoCheckin/1.0"},
        )
        with urlopen(request, timeout=timeout) as response:
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
        try:
            self._request("sendMessage", {"chat_id": self.chat_id, "text": text})
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
        elif command in {"/status", "/notify_status", "/notifications"}:
            self._show_notification_menu(source_chat_id)
        elif command in {"/start", "/help"}:
            self._reply(source_chat_id, self.help_text())

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

    def status_text(self) -> str:
        enabled = self.state["notifications"]
        return "通知狀態：\n" + "\n".join(
            f"{NOTIFICATION_LABELS[name]}：{'開啟' if enabled.get(name, False) else '關閉'}"
            for name in NOTIFICATION_TYPES
        )

    def help_text(self) -> str:
        return "通知設定指令：\n/toggle 顯示並切換通知\n/status 顯示目前狀態\n/help 顯示此說明"

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

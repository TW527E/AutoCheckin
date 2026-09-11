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
        try:
            self._request("sendMessage", {"chat_id": self.chat_id, "text": text})
            return True
        except Exception as exc:
            print(f"Warning: Telegram notification failed: {exc}")
            return False

    def poll_commands(self, timeout: int = 0, force: bool = False) -> None:
        if not self.configured or (not self.poll_enabled and not force):
            return
        updates = self._request(
            "getUpdates",
            {
                "offset": self.state["update_offset"],
                "timeout": timeout,
                "allowed_updates": ["message", "channel_post"],
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
        if command in {"/notify_on", "/notifications_on"}:
            self._set_notification(parts, True, source_chat_id)
        elif command in {"/notify_off", "/notifications_off"}:
            self._set_notification(parts, False, source_chat_id)
        elif command in {"/notify_status", "/notifications"}:
            self._reply(source_chat_id, self.status_text())

    def _set_notification(self, parts: list[str], enabled: bool, reply_chat_id: str) -> None:
        if len(parts) != 2 or parts[1].lower() not in NOTIFICATION_TYPES:
            self._reply(reply_chat_id, "用法：/notify_on|/notify_off success|error|skipped")
            return
        notification_type = parts[1].lower()
        self.state["notifications"][notification_type] = enabled
        self._save_state()
        state = "已開啟" if enabled else "已關閉"
        self._reply(reply_chat_id, f"{state} {notification_type} 通知。")

    def _reply(self, chat_id: str, text: str) -> None:
        if not self.configured:
            return
        try:
            self._request("sendMessage", {"chat_id": chat_id, "text": text})
        except Exception as exc:
            print(f"Warning: Telegram command reply failed: {exc}")

    def status_text(self) -> str:
        enabled = self.state["notifications"]
        return "通知狀態：" + ", ".join(
            f"{name}={'on' if enabled.get(name, False) else 'off'}" for name in NOTIFICATION_TYPES
        )

    def listen_forever(self) -> None:
        if not self.configured:
            raise ValueError("Telegram bot_token and chat_id are required for --telegram-listen")
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

#!/usr/bin/env python3
"""Automatically sign in to AgentRouter using GitHub OAuth or credentials."""

from __future__ import annotations

import argparse
import copy
import json
import os
import stat
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

from telegram_bot import TelegramNotifier

LOGIN_URL = "https://agentrouter.org/login"
LOGOUT_URL = "https://agentrouter.org/api/user/logout"
PROGRAM_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = PROGRAM_DIR / "config.json"
DEFAULT_PROFILE_DIR = Path.home() / ".agentrouter-checkin" / "chromium-profile"
LOGIN_METHODS = {"github", "password"}

DEFAULT_CONFIG: dict[str, Any] = {
    "login_method": "github",
    "username": "",
    "password": "",
    "headless": False,
    "timeout": 180,
    "profile_dir": str(DEFAULT_PROFILE_DIR),
    "skip_if_checked_in": True,
    "telegram": {
        "bot_token": "",
        "chat_id": "",
        "admin_chat_ids": [],
        "poll_commands": True,
        "notifications": {
            "success": True,
            "error": True,
            "skipped": False,
        },
    },
}


def is_login_page(page) -> bool:
    return "/login" in (page.url or "")


def login_controls_visible(page) -> bool:
    selectors = (
        'input[name="username"]',
        'input[name="password"]',
        'button:has-text("Continue with GitHub")',
        'button:has-text("Sign in with Email or Username")',
        'button:has-text("Other login options")',
    )
    for selector in selectors:
        try:
            if page.locator(selector).first.is_visible():
                return True
        except Exception:
            continue
    return False


def is_authenticated_page(page) -> bool:
    url = page.url or ""
    if not url.startswith("https://agentrouter.org"):
        return False
    if "/login" not in url:
        return True
    # Some AgentRouter deployments complete authentication without changing
    # the URL. In that case disappearance of the login form is the success
    # signal; a failed password remains on the same form.
    return not login_controls_visible(page)


def wait_until_signed_in(page, timeout_seconds: int) -> bool:
    deadline = time.monotonic() + timeout_seconds
    authenticated_checks = 0
    while time.monotonic() < deadline:
        if is_authenticated_page(page):
            authenticated_checks += 1
            if authenticated_checks >= 3:
                return True
        else:
            authenticated_checks = 0
        time.sleep(0.5)
    return False


def write_example_config(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if os.name != "nt":
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def load_config(path: Path) -> dict[str, Any]:
    config = copy.deepcopy(DEFAULT_CONFIG)
    if not path.exists():
        return config
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read config file {path}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ValueError(f"Config file {path} must contain a JSON object")
    config.update(loaded)
    return config


def validate_config(config: dict[str, Any]) -> None:
    if config.get("login_method") not in LOGIN_METHODS:
        raise ValueError("login_method must be either 'github' or 'password'")
    if config["login_method"] == "password":
        if not config.get("username"):
            raise ValueError("username is required when login_method is 'password'")
        if not config.get("password"):
            raise ValueError("password is required when login_method is 'password'")
    if not isinstance(config.get("timeout"), int) or config["timeout"] <= 0:
        raise ValueError("timeout must be a positive integer")
    for key in ("headless", "skip_if_checked_in"):
        if not isinstance(config.get(key), bool):
            raise ValueError(f"{key} must be true or false")
    telegram = config.get("telegram", {})
    if not isinstance(telegram, dict):
        raise ValueError("telegram must be an object")
    token = str(telegram.get("bot_token") or os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_id = str(telegram.get("chat_id") or os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    if token and not chat_id:
        raise ValueError("telegram.chat_id is required when a Telegram bot token is configured")
    if not isinstance(telegram.get("admin_chat_ids", []), list):
        raise ValueError("telegram.admin_chat_ids must be a list")
    if not isinstance(telegram.get("poll_commands", True), bool):
        raise ValueError("telegram.poll_commands must be true or false")
    notifications = telegram.get("notifications", {})
    if not isinstance(notifications, dict):
        raise ValueError("telegram.notifications must be an object")
    for key in ("success", "error", "skipped"):
        if key in notifications and not isinstance(notifications[key], bool):
            raise ValueError(f"telegram.notifications.{key} must be true or false")


def warn_about_config_permissions(path: Path, config: dict[str, Any]) -> None:
    telegram = config.get("telegram", {})
    has_token = isinstance(telegram, dict) and bool(telegram.get("bot_token"))
    if os.name == "nt" or not path.exists() or not (config.get("password") or has_token):
        return
    if path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        print(
            f"Warning: {path} contains credentials and is accessible by other users. Run: chmod 600 {path}",
            file=sys.stderr,
        )


def click_button(page, text: str, timeout_ms: int = 15_000) -> None:
    button = page.locator("button").filter(has_text=text).first
    button.wait_for(state="visible", timeout=timeout_ms)
    button.click()


def start_github_login(page) -> None:
    click_button(page, "Continue with GitHub")
    print("GitHub login started. Complete any GitHub login, 2FA, or consent step in the browser window.")


def start_password_login(page, username: str, password: str) -> None:
    click_button(page, "Sign in with Email or Username")
    username_field = page.locator('input[name="username"]')
    password_field = page.locator('input[name="password"]')
    username_field.wait_for(state="visible", timeout=15_000)
    username_field.fill(username)
    password_field.fill(password)
    page.get_by_role("button", name="Continue", exact=True).click()
    print("AgentRouter password login submitted.")


def checkin(config: dict[str, Any], force: bool, notifier: TelegramNotifier | None = None) -> int:
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(
            "Playwright is not installed. Run the platform launcher first so it can install dependencies.",
            file=sys.stderr,
        )
        if notifier:
            notifier.send("error", "AgentRouter 簽到錯誤：Playwright 尚未安裝。")
        return 2

    profile_dir = Path(str(config["profile_dir"])).expanduser()
    profile_dir.mkdir(parents=True, exist_ok=True)
    state_file = profile_dir / "last_checkin_date.txt"
    today = date.today().isoformat()
    if (
        config["skip_if_checked_in"]
        and not force
        and state_file.exists()
        and state_file.read_text(encoding="utf-8").strip() == today
    ):
        print(f"Already checked in today ({today}); skipping duplicate login.")
        if notifier:
            notifier.send("skipped", f"AgentRouter 今日已簽到，略過重複登入。\n日期：{today}")
        return 0

    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=config["headless"],
            viewport={"width": 1280, "height": 900},
        )
        page = context.pages[0] if context.pages else context.new_page()
        try:
            page.goto(LOGOUT_URL, wait_until="domcontentloaded", timeout=15_000)
        except Exception:
            pass
        page.goto(LOGIN_URL, wait_until="domcontentloaded")
        if not is_login_page(page):
            print(f"Login page redirected to an authenticated page: {page.url}")
            state_file.write_text(today + "\n", encoding="utf-8")
            if notifier:
                notifier.send("success", f"AgentRouter 簽到成功（既有登入狀態）。\n日期：{today}\n網址：{page.url}")
            context.close()
            return 0
        try:
            if config["login_method"] == "password":
                start_password_login(page, config["username"], config["password"])
            else:
                start_github_login(page)
        except PlaywrightTimeoutError:
            print("Could not find the expected login controls; the site layout may have changed.", file=sys.stderr)
            if notifier:
                notifier.send("error", "AgentRouter 簽到錯誤：找不到預期的登入控制項，網站版面可能已變更。")
            context.close()
            return 2
        if not wait_until_signed_in(page, config["timeout"]):
            try:
                body_text = page.locator("body").inner_text(timeout=2_000)
                diagnostic = " ".join(body_text.split())[:300]
            except Exception:
                diagnostic = "(page text unavailable)"
            print(
                f"Login did not finish within {config['timeout']} seconds. "
                "Check the credentials, CAPTCHA, 2FA, or the visible error message. "
                f"Current URL: {page.url}. Page text: {diagnostic}",
                file=sys.stderr,
            )
            if notifier:
                notifier.send(
                    "error",
                    f"AgentRouter 簽到錯誤：登入未在 {config['timeout']} 秒內完成。\n目前網址：{page.url}\n頁面訊息：{diagnostic}",
                )
            context.close()
            return 1
        page.wait_for_load_state("domcontentloaded")
        page.wait_for_timeout(1500)
        state_file.write_text(today + "\n", encoding="utf-8")
        print(f"Signed in successfully with {config['login_method']}: {page.url}")
        if notifier:
            notifier.send("success", f"AgentRouter 簽到成功。\n日期：{today}\n登入方式：{config['login_method']}\n網址：{page.url}")
        context.close()
        return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sign in to AgentRouter using GitHub OAuth or a password")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(os.environ.get("AGENTROUTER_CONFIG", DEFAULT_CONFIG_PATH)),
        help=f"JSON config file (default: {DEFAULT_CONFIG_PATH})",
    )
    parser.add_argument("--init-config", action="store_true", help="Create a config file and exit")
    parser.add_argument("--login-method", choices=sorted(LOGIN_METHODS), help="Override config login_method")
    parser.add_argument("--username", help="Override config username")
    parser.add_argument("--password", help="Override config password (environment variable is safer)")
    parser.add_argument("--profile-dir", type=Path, help="Override config profile_dir")
    parser.add_argument("--timeout", type=int, help="Override config timeout")
    parser.add_argument("--headless", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--force", action="store_true", help="Run even if this machine already checked in today")
    parser.add_argument("--telegram-listen", action="store_true", help="Run the Telegram command listener continuously")
    parser.add_argument("--no-telegram-poll", action="store_true", help="Do not poll Telegram during a check-in run")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config_path = args.config.expanduser()
    if args.init_config:
        if config_path.exists():
            print(f"Config already exists: {config_path}", file=sys.stderr)
            return 2
        write_example_config(config_path)
        print(f"Created config: {config_path}")
        return 0
    notifier: TelegramNotifier | None = None
    try:
        config = load_config(config_path)
        telegram = config.setdefault("telegram", {})
        if args.telegram_listen and not isinstance(telegram, dict):
            raise ValueError("telegram must be an object")
        if isinstance(telegram, dict):
            if os.environ.get("TELEGRAM_BOT_TOKEN"):
                telegram["bot_token"] = os.environ["TELEGRAM_BOT_TOKEN"]
            if os.environ.get("TELEGRAM_CHAT_ID"):
                telegram["chat_id"] = os.environ["TELEGRAM_CHAT_ID"]
        overrides = {
            "login_method": args.login_method,
            "username": args.username or os.environ.get("AGENTROUTER_USERNAME"),
            "password": args.password or os.environ.get("AGENTROUTER_PASSWORD"),
            "profile_dir": str(args.profile_dir) if args.profile_dir else None,
            "timeout": args.timeout,
            "headless": args.headless,
        }
        config.update({key: value for key, value in overrides.items() if value is not None})
        notifier = TelegramNotifier(config, config_path.parent / "telegram_state.json")
        validate_config(config)
    except ValueError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        if notifier:
            notifier.send("error", f"AgentRouter 簽到設定錯誤：{exc}")
        return 2
    warn_about_config_permissions(config_path, config)
    assert notifier is not None
    if args.telegram_listen:
        try:
            notifier.listen_forever()
            return 0
        except Exception as exc:
            print(f"Telegram listener error: {exc}", file=sys.stderr)
            return 1
    try:
        if not args.no_telegram_poll:
            try:
                notifier.poll_commands()
            except Exception as exc:
                print(f"Warning: Telegram command polling failed: {exc}", file=sys.stderr)
        return checkin(config, args.force, notifier)
    except Exception as exc:
        print(f"Unexpected check-in error: {exc}", file=sys.stderr)
        notifier.send("error", f"AgentRouter 簽到發生未預期錯誤：{exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

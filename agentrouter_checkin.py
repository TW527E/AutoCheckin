#!/usr/bin/env python3
"""Automatically sign in to AgentRouter with a persisted GitHub session.

The first run opens a visible Chromium window so the user can complete GitHub
login/2FA if necessary. The browser profile is reused on later runs.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date
from pathlib import Path

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


LOGIN_URL = "https://agentrouter.org/login"
LOGOUT_URL = "https://agentrouter.org/api/user/logout"


def is_login_page(page) -> bool:
    return "/login" in (page.url or "")


def is_authenticated_page(page) -> bool:
    url = page.url or ""
    return url.startswith("https://agentrouter.org") and "/login" not in url


def wait_until_signed_in(page, timeout_seconds: int) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if is_authenticated_page(page):
            return True
        time.sleep(1)
    return is_authenticated_page(page)


def checkin(profile_dir: Path, timeout_seconds: int, headless: bool, force: bool) -> int:
    profile_dir.mkdir(parents=True, exist_ok=True)
    state_file = profile_dir / "last_checkin_date.txt"
    today = date.today().isoformat()
    if not force and state_file.exists() and state_file.read_text(encoding="utf-8").strip() == today:
        print(f"Already checked in today ({today}); skipping duplicate login.")
        return 0

    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=headless,
            # A normal desktop-sized viewport makes the login UI reliable.
            viewport={"width": 1280, "height": 900},
        )
        page = context.pages[0] if context.pages else context.new_page()
        # Clear the AgentRouter session so the OAuth callback is reached on
        # every run. A failed/changed logout endpoint is harmless.
        try:
            page.goto(LOGOUT_URL, wait_until="domcontentloaded", timeout=15_000)
        except Exception:
            pass

        # Always visit the login page. This is intentional: AgentRouter's
        # daily check-in is triggered by the login callback, not by merely
        # opening an already-authenticated homepage.
        page.goto(LOGIN_URL, wait_until="domcontentloaded")

        # Some deployments redirect an already authenticated user away from
        # /login. In that case there is nothing else for the script to do.
        if not is_login_page(page):
            print(f"Login page redirected to an authenticated page: {page.url}")
            context.close()
            return 0

        github_button = page.locator("button").filter(has_text="Continue with GitHub").first
        try:
            github_button.wait_for(state="visible", timeout=15_000)
            github_button.click()
        except PlaywrightTimeoutError:
            print("Could not find the GitHub login button; the site layout may have changed.", file=sys.stderr)
            context.close()
            return 2

        print("GitHub login started. Complete any GitHub login, 2FA, or consent step in the browser window.")
        if not wait_until_signed_in(page, timeout_seconds):
            print(f"Login did not finish within {timeout_seconds} seconds.", file=sys.stderr)
            context.close()
            return 1

        # Give the SPA a moment to finish the post-login check-in request.
        page.wait_for_load_state("domcontentloaded")
        page.wait_for_timeout(1500)
        state_file.write_text(today + "\n", encoding="utf-8")
        print(f"Signed in successfully: {page.url}")
        context.close()
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Sign in to AgentRouter using a saved GitHub browser session")
    parser.add_argument(
        "--profile-dir",
        type=Path,
        default=Path.home() / ".agentrouter-checkin" / "chromium-profile",
        help="Directory used to persist the browser session",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=180,
        help="Seconds to wait for interactive GitHub login (default: 180)",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run without showing a browser (only use after the first interactive login)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Run again even if this machine already checked in today",
    )
    args = parser.parse_args()
    return checkin(args.profile_dir.expanduser(), args.timeout, args.headless, args.force)


if __name__ == "__main__":
    raise SystemExit(main())

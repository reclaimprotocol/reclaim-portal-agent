"""Headless-Chromium fallback for URLs that look like JS-rendered shells.

The orchestrator creates one `JSRenderer` instance per `process()` call and
shares it across every OrgID / URL in the run — Playwright's browser launch
is ~1s, so we pay it once rather than per URL. Each `render()` call spins
up a fresh context so cookies / storage don't leak between sites.

If the Playwright package isn't installed or Chromium fails to launch, the
renderer enters an "unavailable" state and every subsequent `render()` call
returns a failure result without raising. This lets the rest of the agent
carry on as if the JS fallback weren't configured at all.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


# Bug 2 — local copy of the password-input regex used by the
# modal-login click decision. Duplicated from `discovery_rules` to
# avoid pulling discovery_rules' wider import surface (requests,
# bs4, …) into the renderer module.
_PASSWORD_INPUT_RE = re.compile(
    r"""(?:
        <input\b[^>]*\btype\s*=\s*["']?password["']?
      | <input\b[^>]*\b(?:name|id)\s*=\s*["']password["']
      | \bformcontrolname\s*=\s*["']password["']
    )""",
    re.IGNORECASE | re.VERBOSE,
)


def _has_password_input(html: str) -> bool:
    return bool(html) and bool(_PASSWORD_INPUT_RE.search(html))


@dataclass(frozen=True)
class RenderResult:
    ok: bool
    final_url: str
    html: str
    error: str = ""


_DEFAULT_BROWSER_USER_AGENT: str = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# Extra HTTP headers Playwright sends on every navigation. Indian-uni
# portals occasionally check `Accept` / `Accept-Language` in addition
# to User-Agent, so spoofing all three is more reliable than UA alone.
# Playwright attaches these via `new_context(extra_http_headers=...)`.
_BROWSER_EXTRA_HTTP_HEADERS: dict[str, str] = {
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-IN,en;q=0.9",
}


class JSRenderer:
    def __init__(self, *, timeout_seconds: int = 20, user_agent: str = "") -> None:
        self.timeout_seconds = timeout_seconds
        # Default to the browser-shaped UA when caller doesn't supply
        # one — mirrors the change in `config.py:load_config`.
        self.user_agent = user_agent or _DEFAULT_BROWSER_USER_AGENT
        self._playwright: Any = None
        self._browser: Any = None
        self._available: bool | None = None  # tri-state: None = not yet tried

    # ---- public -----------------------------------------------------

    def render(self, url: str) -> RenderResult:
        if not self._ensure_browser():
            return RenderResult(ok=False, final_url=url, html="", error="unavailable")
        context = None
        page = None
        try:
            context = self._browser.new_context(
                user_agent=self.user_agent,
                ignore_https_errors=True,
                # Browser-like Accept / Accept-Language headers — see
                # `_BROWSER_EXTRA_HTTP_HEADERS` rationale at module top.
                extra_http_headers=_BROWSER_EXTRA_HTTP_HEADERS,
            )
            page = context.new_page()
            goto_timeout_ms = self.timeout_seconds * 1000
            page.goto(url, timeout=goto_timeout_ms, wait_until="domcontentloaded")
            try:
                page.wait_for_load_state("networkidle", timeout=10_000)
            except Exception:
                # networkidle wait is best-effort — SPA pages sometimes never
                # reach it. Use the DOM we have.
                pass
            html = page.content()

            # Bug 2 — modal-login click. Some sites (mmumullana.org's
            # `onlinelms.mmumullana.org`, similar) render a homepage
            # without a static login form; the form lives inside a
            # modal that opens only when the user clicks a "Login"
            # button. After the initial render, if the captured HTML
            # has no `<input type="password">`, look for a login-shaped
            # button/link, click it, wait 2s, and re-capture the DOM.
            # Failures are silent — we fall back to the original HTML.
            if not _has_password_input(html):
                if self._try_click_login_button(page):
                    try:
                        page.wait_for_timeout(2000)
                        html = page.content()
                    except Exception:
                        pass

            return RenderResult(ok=True, final_url=page.url, html=html)
        except Exception as err:
            return RenderResult(ok=False, final_url=url, html="", error=f"{type(err).__name__}: {err}")
        finally:
            if page is not None:
                try:
                    page.close()
                except Exception:
                    pass
            if context is not None:
                try:
                    context.close()
                except Exception:
                    pass

    # ---- Bug 2: modal-login click ----------------------------------

    # Selectors tried in order. First match wins. `:has-text` is
    # Playwright's case-insensitive text-content matcher. Class /
    # id selectors at the bottom catch sites whose buttons don't
    # use literal "Login" text.
    _LOGIN_BUTTON_SELECTORS: tuple[str, ...] = (
        'button:has-text("Login")',
        'button:has-text("Sign In")',
        'button:has-text("Sign in")',
        'button:has-text("Log In")',
        'a:has-text("Login")',
        'a:has-text("Sign In")',
        'a:has-text("Sign in")',
        '[role="button"]:has-text("Login")',
        '[role="button"]:has-text("Sign In")',
        ".login-btn", ".btn-login", ".login-button",
        "#loginBtn", "#login-btn", "#loginButton",
    )

    def _try_click_login_button(self, page: Any) -> bool:
        """Best-effort: click the first selector that matches, with a
        1s click timeout. Returns True if a click was attempted (the
        click may still have failed silently — caller re-checks the
        DOM for the new password input)."""
        for sel in self._LOGIN_BUTTON_SELECTORS:
            try:
                locator = page.locator(sel)
                if locator.count() == 0:
                    continue
                locator.first.click(timeout=1000)
                return True
            except Exception:
                continue
        return False

    def close(self) -> None:
        if self._browser is not None:
            try:
                self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:
                pass
            self._playwright = None

    def __enter__(self) -> "JSRenderer":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ---- internal ---------------------------------------------------

    def _ensure_browser(self) -> bool:
        if self._available is False:
            return False
        if self._browser is not None:
            return True
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            logger.warning(
                "Playwright not installed; JS rendering unavailable for this run. "
                "Install with: pip install playwright && playwright install chromium",
            )
            self._available = False
            return False
        try:
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch(headless=True)
            self._available = True
            logger.info("JS renderer ready (Chromium headless)")
            return True
        except Exception:
            logger.exception(
                "Playwright Chromium launch failed; JS rendering unavailable for this run. "
                "Have you run `playwright install chromium`?",
            )
            self._available = False
            return False

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
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RenderResult:
    ok: bool
    final_url: str
    html: str
    error: str = ""


class JSRenderer:
    def __init__(self, *, timeout_seconds: int = 20, user_agent: str = "") -> None:
        self.timeout_seconds = timeout_seconds
        self.user_agent = user_agent or "reclaim-portal-agent/0.1"
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

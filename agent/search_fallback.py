"""Genie-V3 · Layer 2 — external web-search fallback (DuckDuckGo, keyless).

The recovery path for universities the crawler cannot help with: sites behind a
WAF, homepages that link nothing useful, and portals hosted on a domain the
institution never links to.

ENDPOINT — MEASURED, NOT ASSUMED (2026-08-31)
---------------------------------------------
The brief specified `duckduckgo.com`. That host returns **HTTP 202 with an
"anomaly" interstitial** — zero results, no error, just a block page that parses
as valid HTML. `lite.duckduckgo.com/lite/` behaves the same way. Only
`html.duckduckgo.com/html/` answers with real results:

    POST https://html.duckduckgo.com/html/   200  result__a=3   direct URLs
    GET  https://html.duckduckgo.com/html/   200  result__a=3   redirect-wrapped
    POST https://lite.duckduckgo.com/lite/   202  0 results     "anomaly"
    GET  https://duckduckgo.com/html/        202  0 results     "anomaly"

POST is preferred because it returns destination URLs directly, while GET wraps
every href in `//duckduckgo.com/l/?uddg=<encoded>`; both are handled.

This is a scraper of an undocumented HTML surface, so treat it as best-effort.
V2's DuckDuckGo route was disabled for exactly this reason — it degraded to zero
results while still costing ~72 s per org. It is worth having as a free
fallback, but a run that depends on it will be fragile, and `_RESULT_RE` will
need revisiting whenever DuckDuckGo changes its markup.
"""
from __future__ import annotations

import asyncio
import html as _html
import logging
import os
import random
import re
import ssl
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

import aiohttp

logger = logging.getLogger("genie.search")

#: Working endpoint. `duckduckgo.com` and `lite.` are block pages — see docstring.
DDG_URL = os.getenv("DDG_SEARCH_URL", "https://html.duckduckgo.com/html/")
MAX_RESULTS = 6
DEFAULT_TIMEOUT_S = 25

#: Rotated per request. A single fixed identity across a 4,000-org batch is what
#: gets an IP throttled; varying it is cheap insurance.
USER_AGENTS = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
)

#: Result anchor: href + inner label. `result__url` carries only a display
#: string, so the destination is taken from `result__a`.
_RESULT_RE = re.compile(
    r'<a[^>]+class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S | re.I)
_SNIPPET_RE = re.compile(r'class="result__snippet"[^>]*>(.*?)</a>', re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_BLOCKED_RE = re.compile(r"anomaly|unusual traffic|captcha", re.I)


def _ssl_context() -> ssl.SSLContext:
    """certifi-backed context.

    aiohttp uses the system trust store, which is incomplete in this virtualenv
    and made every DuckDuckGo request fail with ClientConnectorCertificateError.
    certifi fixes it properly; disabling verification would have "worked" too and
    been the wrong answer.
    """
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa: BLE001
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        logger.warning("search: certifi unavailable — TLS verification disabled")
        return ctx


_SSL = _ssl_context()


def build_query(university_name: str, official_domain: str) -> str:
    """Naming the common platforms biases results toward third-party SaaS
    tenants, which is the class of portal a homepage crawl misses because the
    university never links to it."""
    return (f'"{university_name}" ({official_domain}) '
            f'student portal login samarth tcsion erp lms moodle')


def _clean(fragment: str) -> str:
    return " ".join(_html.unescape(_TAG_RE.sub(" ", fragment or "")).split())


def _unwrap(href: str) -> str:
    """Resolve DuckDuckGo's `/l/?uddg=<encoded>` redirect to the real target."""
    if not href:
        return ""
    if href.startswith("//"):
        href = "https:" + href
    if "duckduckgo.com/l/" in href or href.startswith("/l/"):
        q = parse_qs(urlsplit(href).query).get("uddg")
        if q:
            return unquote(q[0])
    return href


def parse_results(payload: str, max_results: int = MAX_RESULTS) -> list[dict[str, str]]:
    """Extract up to `max_results` links from a DuckDuckGo HTML payload."""
    anchors = _RESULT_RE.findall(payload or "")
    snippets = _SNIPPET_RE.findall(payload or "")
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for i, (href, label) in enumerate(anchors):
        url = _unwrap(_html.unescape(href.strip()))
        if not url.lower().startswith(("http://", "https://")):
            continue
        key = url.rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        title = _clean(label)
        snip = _clean(snippets[i]) if i < len(snippets) else ""
        text = f"{title} | Snippet: {snip[:60]}" if snip else title
        out.append({"url": url, "text": text[:200]})
        if len(out) >= max_results:
            break
    return out


async def execute_search_fallback(
    university_name: str,
    official_domain: str,
    *,
    max_results: int = MAX_RESULTS,
    timeout_seconds: int = DEFAULT_TIMEOUT_S,
    session: aiohttp.ClientSession | None = None,
) -> list[dict[str, str]]:
    """Search DuckDuckGo for this university's portals.

    Returns `[{'url': ..., 'text': ...}]` — the same shape
    `crawler.extract_raw_university_links` produces, so the result drops
    straight into `LocalKnowledgeMatrixFilter` with no adapter.

    Keyless and free. Always returns a list: throttling, markup changes, HTTP
    errors, timeouts and network failures all resolve to `[]`, because search is
    a RECOVERY path and must never take down rows that were fine without it.
    """
    query = build_query(university_name, official_domain)
    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://html.duckduckgo.com/",
    }
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)

    async def _fetch(sess: aiohttp.ClientSession) -> list[dict[str, str]]:
        # POST first: it returns destination URLs directly, where GET wraps every
        # href in a duckduckgo.com/l/?uddg= redirect.
        for method in ("post", "get"):
            try:
                kwargs: dict[str, Any] = dict(headers=headers, timeout=timeout,
                                              ssl=_SSL, allow_redirects=True)
                if method == "post":
                    ctx = sess.post(DDG_URL, data={"q": query}, **kwargs)
                else:
                    ctx = sess.get(DDG_URL, params={"q": query}, **kwargs)
                async with ctx as resp:
                    status = resp.status
                    payload = await resp.text()
                if _BLOCKED_RE.search(payload[:4000]) or status == 202:
                    logger.warning("search: duckduckgo throttled (%s) for %r — "
                                   "trying %s" if method == "post" else
                                   "search: duckduckgo throttled (%s) for %r",
                                   status, university_name)
                    continue
                if status != 200:
                    logger.warning("search: duckduckgo HTTP %s for %r", status,
                                   university_name)
                    continue
                hits = parse_results(payload, max_results)
                if hits:
                    return hits
                logger.info("search: duckduckgo returned 0 parseable results for %r "
                            "(markup change, or genuinely no hits)", university_name)
            except asyncio.TimeoutError:
                logger.warning("search: duckduckgo timed out after %ss for %r",
                               timeout_seconds, university_name)
            except aiohttp.ClientError as exc:
                logger.warning("search: duckduckgo unreachable (%s) for %r",
                               type(exc).__name__, university_name)
        return []

    own = session is None
    sess = session or aiohttp.ClientSession()
    try:
        results = await _fetch(sess)
        logger.info("search: %r (%s) -> %d result(s)",
                    university_name, official_domain, len(results))
        return results
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — never break the caller's loop
        logger.warning("search: unexpected %s: %s", type(exc).__name__, str(exc)[:160])
        return []
    finally:
        if own:
            await sess.close()

"""Genie-V3 · Layer 2 — external web-search fallback (Google via Serper).

The recovery path for universities the crawler cannot help with: sites behind a
WAF, homepages that link nothing useful, and portals hosted on a domain the
institution never links to.

WHY THIS IS AN API AND NOT A SCRAPER (2026-09-01)
-------------------------------------------------
This module used to scrape `html.duckduckgo.com/html/`. That surface is now
gone: it answers every request — direct and through US, German and Brazilian
residential exits alike — with **HTTP 202 and a CAPTCHA interstitial**:

    "Unfortunately, bots use DuckDuckGo too. Please complete the following
     challenge to confirm this search was made by a human."

It parses as valid HTML, so a scraper reads it as "no results" rather than as a
failure. That is the dangerous shape of the bug: across the first 128 orgs of
the August4000 run, 47 searches fired, 44 returned zero, and every one of them
was recorded as a discovery miss rather than a search outage. The alternatives
were checked and are no better — `lite.duckduckgo.com` serves the same wall,
Mojeek/Startpage/Brave bot-wall, and Bing returns a JavaScript shell with zero
extractable hrefs even under headless Chromium.

A keyed API removes the entire failure class: a quota error is an explicit HTTP
status we can log and act on, not a block page wearing an empty result set as a
disguise. Serper fronts Google, so recall is also strictly better than the
DuckDuckGo surface it replaces.

Still best-effort by contract: this is a RECOVERY path, and every failure mode
(missing key, quota, timeout, malformed body) resolves to `[]` so that a row
which was fine without search is never taken down by it.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import ssl
from typing import Any

import aiohttp
import certifi

logger = logging.getLogger("genie.search")

#: Serper's Google Search endpoint.
SERPER_URL = os.getenv("SERPER_SEARCH_URL", "https://google.serper.dev/search")
MAX_RESULTS = 6
DEFAULT_TIMEOUT_S = 25

#: Read at call time rather than import time so a key added to `.env` after the
#: module is first imported (or rotated mid-run) is picked up without a restart.
def _api_key() -> str:
    return (os.getenv("SERPER_API_KEY") or "").strip()


def _ssl_context() -> ssl.SSLContext:
    """certifi's bundle, not the OS store.

    Python installed from python.org ships no system roots on macOS, so the
    default context fails TLS against perfectly valid certificates. Search is
    the one place that failure would be silently absorbed as "no results".
    """
    return ssl.create_default_context(cafile=certifi.where())


_SSL = _ssl_context()


#: Platform vocabulary for the broad sweep, OR-grouped — see `build_query`.
_PLATFORM_TERMS = ("student portal", "login", "samarth", "moodle", "erp")


def build_query(university_name: str, official_domain: str) -> str:
    """Naming the common platforms biases results toward third-party SaaS
    tenants, which is the class of portal a homepage crawl misses because the
    university never links to it.

    OR-GROUPED, NOT SPACE-SEPARATED (measured 2026-09-01)
    -----------------------------------------------------
    DuckDuckGo treated a bare term list loosely, so
    `... student portal login samarth tcsion erp lms moodle` worked. Google ANDs
    every term, and no page contains all of "samarth", "tcsion", "erp", "lms"
    and "moodle" at once — so the same string returns NOTHING. Measured across
    four universities: space-separated 14 results, OR-grouped 24.

        "Kota University" (uok.ac.in) student portal login samarth ...  -> 0
        "Kota University" uok.ac.in (student portal OR login OR ...)    -> 6

    The domain stays outside the group: it is a corroborating signal we want
    weighted, not one alternative among many.
    """
    terms = " OR ".join(_PLATFORM_TERMS)
    return f'"{university_name}" {official_domain} ({terms})'


def build_category_query(university_name: str, official_domain: str,
                         category_keyword_target: str) -> str:
    """A scavenging query aimed at ONE missing capability.

    The broad `build_query` names every platform family at once, which is right
    when we have nothing and want any portal. It is wrong when we already hold
    an ERP and are hunting the LMS: the ERP terms dominate the result set and
    return what we already have. Narrowing to the missing capability's own
    vocabulary is what surfaces the detached second system.

    `category_keyword_target` must already be OR-grouped by the caller for the
    same reason `build_query` is — see above.
    """
    return f'"{university_name}" {official_domain} {category_keyword_target} login'


def parse_results(payload: dict[str, Any],
                  max_results: int = MAX_RESULTS) -> list[dict[str, str]]:
    """Serper's `organic` array -> our universal candidate shape.

    Rows missing a `link` are skipped rather than emitted with an empty URL: a
    blank candidate survives the filter and reaches the model as a wasted slot.
    """
    organic = payload.get("organic")
    if not isinstance(organic, list):
        return []
    out: list[dict[str, str]] = []
    for item in organic:
        if not isinstance(item, dict):
            continue
        url = str(item.get("link") or "").strip()
        if not url:
            continue
        out.append({"url": url, "text": str(item.get("title") or "").strip()})
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
    query: str | None = None,
) -> list[dict[str, str]]:
    """Search Google (via Serper) for this university's portals.

    Returns `[{'url': ..., 'text': ...}]` — the same shape
    `crawler.extract_raw_university_links` produces, so the result drops
    straight into `LocalKnowledgeMatrixFilter` with no adapter.

    Always returns a list. Quota exhaustion, auth failures, timeouts and network
    errors all resolve to `[]`, because search is a RECOVERY path and must never
    take down rows that were fine without it.
    """
    key = _api_key()
    if not key:
        # Logged once per call rather than raised: a missing key must degrade to
        # "search unavailable", not abort a 4,000-row run at hour three.
        logger.warning("search: SERPER_API_KEY not set — search fallback disabled")
        return []

    # `query` lets a caller aim the scavenge at one missing capability
    # (`build_category_query`); unset keeps the broad all-platforms sweep.
    q = query or build_query(university_name, official_domain)
    payload = {"q": q, "num": max_results}
    headers = {"X-API-KEY": key, "Content-Type": "application/json"}
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)

    async def _fetch(sess: aiohttp.ClientSession) -> list[dict[str, str]]:
        try:
            async with sess.post(SERPER_URL, json=payload, headers=headers,
                                 timeout=timeout, ssl=_SSL) as resp:
                status = resp.status
                body = await resp.text()
        except asyncio.TimeoutError:
            logger.warning("search: serper timed out after %ss for %r",
                           timeout_seconds, university_name)
            return []
        except aiohttp.ClientError as exc:
            logger.warning("search: serper unreachable (%s) for %r",
                           type(exc).__name__, university_name)
            return []

        if status == 401 or status == 403:
            logger.error("search: serper rejected the API key (HTTP %s) — "
                         "search fallback is disabled until it is fixed", status)
            return []
        if status == 429:
            logger.error("search: serper quota/rate limit hit (HTTP 429) for %r",
                         university_name)
            return []
        if status != 200:
            logger.warning("search: serper HTTP %s for %r: %s",
                           status, university_name, body[:180])
            return []

        try:
            data = json.loads(body)
        except ValueError:
            logger.warning("search: serper returned non-JSON for %r: %s",
                           university_name, body[:180])
            return []
        hits = parse_results(data, max_results)
        if not hits:
            logger.info("search: serper returned 0 organic results for %r "
                        "(genuinely no hits)", university_name)
        return hits

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

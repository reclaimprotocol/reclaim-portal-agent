"""Genie-V3 · Layer 3 — Post-Inference HTTP Hook.

The model returns URLs it believes are student portals. This module is the
cheap, factual check that runs immediately after: does that address actually
answer? A confidently-worded portal URL that 404s is worse than no answer,
because it ships to a customer as verified fact.

WHAT COUNTS AS ALIVE — AND WHY 401/403/429 DO
---------------------------------------------
A WAF challenging us proves a server is there and responding; it says nothing
about whether the portal exists. V2 treated those as dead and produced a run of
false "unreachable — remove" verdicts on portals that were perfectly healthy,
which is why they are explicitly TRUE here.

    2xx / 3xx          -> True   (answering, possibly after redirects)
    401 / 403 / 429    -> True   (WAF/auth wall: the server is alive)
    405                -> retry with GET; many university stacks reject HEAD
    404 / 5xx / other  -> False
    timeout / network  -> False

TWO THINGS THIS DOES BEYOND A BARE HEAD REQUEST
-----------------------------------------------
Both exist because without them the check reports live portals as dead:

  * TLS verification is off. University TLS is frequently misconfigured
    (expired, self-signed, wrong SAN) and aiohttp aborts on that before it ever
    sees a status code.
  * Requests are routed through the residential proxy by ccTLD, the same rule
    the crawler uses. Verifying a geo-blocked Brazilian portal from an Indian
    exit is how you turn a working portal into a dead one.
"""
from __future__ import annotations

import asyncio
import logging
import os
import ssl
from typing import Any, Iterable, Sequence

import aiohttp

from agent import proxy as _proxy

logger = logging.getLogger("genie.guardrails")

#: Status codes that prove the host is answering even though it refuses us.
WAF_ALIVE_CODES = frozenset({401, 403, 429})
#: Server said "not that verb" — retry with GET before believing it.
METHOD_NOT_ALLOWED = 405

USER_AGENT = os.getenv(
    "GENIE_UA",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
)

#: aiohttp validates certificates by default; university TLS often fails that
#: check while the site itself is fine. We are testing reachability, not trust.
_INSECURE_SSL = ssl.create_default_context()
_INSECURE_SSL.check_hostname = False
_INSECURE_SSL.verify_mode = ssl.CERT_NONE


def _as_url(url_path: str) -> str:
    raw = (url_path or "").strip()
    if not raw:
        raise ValueError("verify_portal_endpoint() needs a URL")
    if "://" not in raw:
        raw = "https://" + raw.lstrip("/")
    return raw


def _proxy_url(url: str, country_hint: str = "") -> str | None:
    """Residential proxy URL with credentials inline, or None for direct.

    aiohttp takes the proxy as a single URL string, so the username/password
    from `agent.proxy` are embedded rather than passed as BasicAuth.
    """
    cfg = _proxy.playwright_proxy(country_hint, url)
    if not cfg:
        return None
    server = cfg["server"].replace("http://", "").replace("https://", "")
    return f"http://{cfg['username']}:{cfg['password']}@{server}"


def _classify(status: int) -> bool:
    if 200 <= status < 400:
        return True
    if status in WAF_ALIVE_CODES:
        return True
    return False


async def verify_portal_endpoint_detailed(
    url_path: str,
    timeout_seconds: int = 5,
    *,
    session: aiohttp.ClientSession | None = None,
    use_proxy: bool = True,
    country_hint: str = "",
    retry_timeout_seconds: int | None = None,
) -> tuple[bool, int, str]:
    """(alive, http_status, note) for `url_path`.

    Same logic as `verify_portal_endpoint`, but keeps the status code so callers
    that log or persist it do not have to probe the endpoint a second time.

    Args:
        url_path: absolute URL (scheme added when absent).
        timeout_seconds: total per-attempt budget.
        session: reuse an open `ClientSession` — batch callers should, since a
            fresh session per URL pays TCP + TLS setup every time.
        use_proxy: route via the residential proxy chosen by ccTLD.
        country_hint: org country, used when the URL carries no ccTLD.
        retry_timeout_seconds: when set, a TIMEOUT (only) is retried once with
            this longer budget. Off by default to keep the contract exact — but
            see the note in the module docs: slow is not the same as dead, and
            V2 re-checks at a longer timeout flipped 26 of 52 "unreachable"
            portals to working.

    Never raises: any aiohttp or network anomaly resolves to False so one bad
    URL cannot break the async processing loop.
    """
    try:
        url = _as_url(url_path)
    except ValueError as exc:
        logger.warning("guardrails: %s", exc)
        return False, 0, "bad url"

    proxy = _proxy_url(url, country_hint) if use_proxy else None
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    headers = {"User-Agent": USER_AGENT}

    async def _attempt(sess: aiohttp.ClientSession,
                       budget: aiohttp.ClientTimeout) -> tuple[bool, int, str]:
        # 1) HEAD — cheapest possible probe.
        try:
            async with sess.head(url, allow_redirects=True, timeout=budget,
                                 headers=headers, proxy=proxy,
                                 ssl=_INSECURE_SSL) as resp:
                status = resp.status
            if status != METHOD_NOT_ALLOWED:
                alive = _classify(status)
                logger.debug("guardrails: HEAD %s -> %s (alive=%s)", url, status, alive)
                return alive, status, "head"
        except asyncio.TimeoutError:
            raise
        except aiohttp.ClientError as exc:
            # Some servers reject HEAD at the connection layer rather than with
            # a 405, so a client error is not yet proof of death — fall through
            # to GET and let that decide.
            logger.debug("guardrails: HEAD %s failed (%s) — trying GET",
                         url, type(exc).__name__)

        # 2) GET — 405, or a HEAD the server mishandled.
        async with sess.get(url, allow_redirects=True, timeout=budget,
                            headers=headers, proxy=proxy,
                            ssl=_INSECURE_SSL) as resp:
            status = resp.status
            await resp.release()
        alive = _classify(status)
        logger.debug("guardrails: GET %s -> %s (alive=%s)", url, status, alive)
        return alive, status, "get"

    own_session = session is None
    sess = session or aiohttp.ClientSession(timeout=timeout)
    try:
        try:
            return await _attempt(sess, timeout)
        except asyncio.TimeoutError:
            if retry_timeout_seconds:
                logger.info("guardrails: %s timed out at %ss — retrying at %ss",
                            url, timeout_seconds, retry_timeout_seconds)
                try:
                    return await _attempt(
                        sess, aiohttp.ClientTimeout(total=retry_timeout_seconds))
                except Exception as exc:  # noqa: BLE001
                    logger.info("guardrails: %s dead on retry (%s)", url, type(exc).__name__)
                    return False, 0, f"retry {type(exc).__name__}"
            logger.info("guardrails: %s timed out after %ss — treated as dead",
                        url, timeout_seconds)
            return False, 0, "timeout"
        except aiohttp.ClientError as exc:
            logger.info("guardrails: %s unreachable (%s)", url, type(exc).__name__)
            return False, 0, type(exc).__name__
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — never break the caller's loop
            logger.warning("guardrails: %s unexpected %s: %s",
                           url, type(exc).__name__, str(exc)[:160])
            return False, 0, type(exc).__name__
    finally:
        if own_session:
            await sess.close()


async def verify_many(
    urls: Sequence[str],
    timeout_seconds: int = 5,
    *,
    concurrency: int = 20,
    use_proxy: bool = True,
    country_hint: str = "",
    retry_timeout_seconds: int | None = None,
) -> dict[str, bool]:
    """Verify a batch through ONE session, bounded by `concurrency`."""
    sem = asyncio.Semaphore(max(1, concurrency))
    results: dict[str, bool] = {}
    connector = aiohttp.TCPConnector(limit=max(1, concurrency), ssl=_INSECURE_SSL)
    async with aiohttp.ClientSession(connector=connector) as sess:
        async def one(u: str) -> None:
            async with sem:
                results[u] = await verify_portal_endpoint(
                    u, timeout_seconds, session=sess, use_proxy=use_proxy,
                    country_hint=country_hint,
                    retry_timeout_seconds=retry_timeout_seconds,
                )
        await asyncio.gather(*(one(u) for u in urls), return_exceptions=True)
    return results


async def verify_portal_endpoint(
    url_path: str,
    timeout_seconds: int = 5,
    *,
    session: aiohttp.ClientSession | None = None,
    use_proxy: bool = True,
    country_hint: str = "",
    retry_timeout_seconds: int | None = None,
) -> bool:
    """True if `url_path` is a live endpoint, False if dead or unreachable.

    The Layer 3 contract: 2xx/3xx alive; 401/403/429 alive (a WAF answering is
    still a server answering); 405 retried with GET; 404/5xx/timeout/network
    dead. Never raises — one bad URL must not break the async processing loop.
    """
    alive, _status, _note = await verify_portal_endpoint_detailed(
        url_path, timeout_seconds, session=session, use_proxy=use_proxy,
        country_hint=country_hint, retry_timeout_seconds=retry_timeout_seconds,
    )
    return alive

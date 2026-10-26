"""Genie-V3 · Layer 2 — asynchronous link extraction via crawl4ai.

Replaces V2's `_own_site_links()`, which fetched the homepage with plain
`requests` and kept only anchors matching a hardcoded multilingual hint list.
Two consequences we measured: JS-rendered navigation was invisible, and the
hint regex ran BEFORE the model saw anything, so a portal linked as "Sistema
Acadêmico" was discarded with no chance of being overruled.

This module renders the page in a real headless Chromium and returns EVERY
anchor. Filtering and judgement move downstream to Layer 1, where the model can
actually reason about them.

Two crawl4ai behaviours worth knowing, both verified on 2026-08-30 rather than
assumed — get either wrong and this module silently under-delivers:

1.  `wait_for` takes a CSS selector by default. The literal string
    `'javascript'` is therefore treated as a selector for a `<javascript>`
    element, which never exists: the crawl waits out the whole page timeout and
    returns `success=False`. Measured on buet.ac.bd — 30.3 s and a hard failure
    versus 4.2 s and a clean result for the `js:` predicate form. `WAIT_FOR_JS`
    below is the correct idiom for "wait until scripts have finished".

2.  `result.links` is NOT the full anchor set — it is crawl4ai's scored/filtered
    view. On buet.ac.bd it returned 3 links while the rendered DOM held 50
    `<a href>` elements. Since link extraction is this module's entire purpose,
    we parse `result.html` ourselves and treat `result.links` as advisory only.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote, urljoin, urlsplit

import aiohttp
from bs4 import BeautifulSoup
from crawl4ai import AsyncWebCrawler, BrowserConfig, CacheMode, CrawlerRunConfig

from agent import proxy as _proxy
from agent.languages import profile_for_country

logger = logging.getLogger("genie.crawler")

#: The correct way to express "wait until the page's JavaScript has settled".
#: A bare selector string here is a silent 30-second failure — see module docs.
WAIT_FOR_JS = "js:() => document.readyState === 'complete'"

DEFAULT_PAGE_TIMEOUT_MS = 30_000
STATIC_PAGE_LIMIT_BYTES = 1024 * 1024
STATIC_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

#: Schemes that are anchors in markup but not navigable links.
_NON_HTTP_PREFIXES = ("mailto:", "tel:", "javascript:", "data:", "file:", "sms:", "#")
_DISCOVERY_PAGE_RE = re.compile(
    r"(?:student|current.?student|campus.?life|education|academic|service|"
    r"information.?technology|\bict\b|library|portal|support|"
    r"在学生|学生|学務|教育|情報|サービス|図書館|履修)", re.I)
_DISCOVERY_NOISE_RE = re.compile(
    r"(?:/news/|/event/|/press/|/research/|/admission|/prospective|"
    r"\.(?:pdf|docx?|xlsx?|pptx?|zip)(?:$|[?#]))", re.I)


def _browser_config(proxy_config: dict[str, str] | None = None, **overrides: Any) -> BrowserConfig:
    """Headless Chromium with image loading disabled, optionally proxied.

    `text_mode=True` is crawl4ai's switch for suppressing image (and other heavy
    asset) downloads. University homepages are image-dense and we never look at
    a pixel — we want the DOM — so this is pure latency saved.

    `proxy_config` takes crawl4ai's `ProxyConfig(server, username, password)`,
    which is exactly the shape `agent.proxy.playwright_proxy()` already returns,
    so V2's residential-proxy setup drops straight in.
    """
    cfg = dict(
        browser_type="chromium",
        headless=True,
        text_mode=True,        # <- images/heavy assets not downloaded
        light_mode=True,       # disables background features we don't use
        ignore_https_errors=True,   # university TLS is frequently misconfigured
        enable_stealth=True,   # mask webdriver/navigator automation fingerprints
        avoid_ads=True,        # skip ad frames/overlays that hide real nav links
        verbose=False,
    )
    if proxy_config:
        cfg["proxy_config"] = proxy_config
    cfg.update(overrides)
    return BrowserConfig(**cfg)


# --------------------------------------------------------------------------- #
#  Residential proxy                                                           #
# --------------------------------------------------------------------------- #
def exit_country(url_or_domain: str, country_hint: str = "") -> str | None:
    """ISO2 exit country this URL would use, or None for a direct connection.

    Reuses `agent.proxy`: the country comes from an explicit organisation hint
    first and the URL's ccTLD second. Dynamic mode therefore also routes gTLD
    portals correctly when their sheet row contains a country.
    """
    return _proxy.active_country(country_hint, url_or_domain)


def proxy_for(url_or_domain: str, country_hint: str = "") -> dict[str, str] | None:
    """crawl4ai `proxy_config` for this URL, or None for direct."""
    return _proxy.playwright_proxy(country_hint, url_or_domain)


def _aiohttp_proxy(url_or_domain: str, country_hint: str = "") -> str | None:
    """aiohttp proxy URL matching the browser crawler's country routing."""
    candidates = _aiohttp_proxy_candidates(url_or_domain, country_hint)
    return candidates[0][1] if candidates else None


def _aiohttp_proxy_candidates(
    url_or_domain: str, country_hint: str = ""
) -> list[tuple[str, str]]:
    """Ordered provider/proxy-URL pairs for the static fast crawl."""
    output: list[tuple[str, str]] = []
    for provider, cfg in proxy_candidates_for(url_or_domain, country_hint):
        server = cfg["server"].replace("http://", "").replace("https://", "")
        user = quote(cfg["username"], safe="")
        password = quote(cfg["password"], safe="")
        output.append((provider, f"http://{user}:{password}@{server}"))
    return output


def _aiohttp_proxy_failure(exc: Exception, proxy_url: str | None) -> bool:
    if not proxy_url:
        return False
    return isinstance(exc, (
        asyncio.TimeoutError,
        aiohttp.ClientHttpProxyError,
        aiohttp.ClientProxyConnectionError,
        aiohttp.ClientConnectorError,
    )) or _proxy.is_proxy_failure(exc)


def proxy_candidates_for(
    url_or_domain: str, country_hint: str = ""
) -> list[tuple[str, dict[str, str]]]:
    """Ordered provider/config pairs for proxy-transport failover."""
    return _proxy.playwright_proxy_candidates(country_hint, url_or_domain)


async def probe_proxy(sample_url: str = "https://x.edu.br/") -> tuple[bool, str]:
    """(reachable, detail) for the residential proxy this URL would use.

    Run this before any batch. The proxy FAILS CLOSED: when the provider
    rejects our IP (`401 ip_blacklisted`) every proxied fetch dies, and a
    crawler that can't tell "blocked" from "dead" will report perfectly healthy
    universities as having no portal. In V2 that produced dozens of false
    removals before we started probing first.
    """
    candidates = _proxy.requests_proxy_candidates("", sample_url)
    if not candidates:
        return True, f"direct (no proxy for {sample_url})"
    failures: list[str] = []
    import requests
    for provider, candidate in candidates:
        try:
            r = requests.get(
                "https://api.ipify.org", proxies=candidate, timeout=25)
            if r.status_code == 200 and r.text.strip():
                return True, (
                    f"exit ip {r.text.strip()} ({exit_country(sample_url)}) "
                    f"via {provider}"
                )
            failures.append(f"{provider}: HTTP {r.status_code}")
            if r.status_code not in {407, 500, 502, 503, 504}:
                break
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{provider}: {type(exc).__name__}")
            if not _proxy.is_proxy_failure(exc):
                break
    return False, "; ".join(failures)


# --------------------------------------------------------------------------- #
#  infrastructure_block.json — Layer 5 memory                                  #
# --------------------------------------------------------------------------- #
BLOCK_FILE = Path(os.getenv("GENIE_BLOCK_FILE",
                            Path(__file__).resolve().parents[1] / "infrastructure_block.json"))

#: Error signatures that mean "a firewall refused US", as opposed to "this site
#: is broken". The distinction matters: a blocked domain may be perfectly alive
#: and worth retrying from another exit, while a dead one is just dead. Only
#: genuine blocks are recorded, or the file becomes a junk drawer of timeouts.
_BLOCK_SIGNATURES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("cloudflare_challenge", re.compile(r"cloudflare|just a moment|cf-chl|__cf_bm|attention required", re.I)),
    ("captcha",              re.compile(r"captcha|recaptcha|hcaptcha|are you (a )?human", re.I)),
    ("anti_bot",             re.compile(r"anti-?bot|bot (detection|protection)|automated (traffic|request)", re.I)),
    ("waf_403",              re.compile(r"\b403\b|forbidden|access denied|not authorized|akamai|imperva|incapsula", re.I)),
    ("rate_limited",         re.compile(r"\b429\b|too many requests|rate.?limit", re.I)),
)
# Failures that are NOT blocks — never record these.
_NOT_A_BLOCK = re.compile(
    r"ERR_NAME_NOT_RESOLVED|ERR_CONNECTION_REFUSED|ERR_CONNECTION_RESET|"
    r"ERR_ADDRESS_UNREACHABLE|getaddrinfo|NXDOMAIN|SSL|CERT_", re.I)

_block_lock = asyncio.Lock()


def classify_block(error: str) -> str | None:
    """Block kind for this error string, or None when it isn't a block."""
    if not error or _NOT_A_BLOCK.search(error):
        return None
    for kind, rx in _BLOCK_SIGNATURES:
        if rx.search(error):
            return kind
    return None


def load_blocks() -> dict[str, Any]:
    """Current contents of infrastructure_block.json ({} if absent/corrupt)."""
    try:
        return json.loads(BLOCK_FILE.read_text() or "{}")
    except FileNotFoundError:
        return {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("crawler: could not read %s (%s) — treating as empty",
                       BLOCK_FILE, type(exc).__name__)
        return {}


def _atomic_write(data: dict[str, Any]) -> None:
    """Write via temp file + os.replace so a crash can't truncate the memory.

    Read-modify-write is done under an asyncio lock, which covers concurrency
    WITHIN a process. V2's sharded batch runners are separate PROCESSES, and a
    single shared state file got clobbered by concurrent writers — so each
    shard should point GENIE_BLOCK_FILE at its own path and merge afterwards,
    exactly as the done-files do.
    """
    BLOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(BLOCK_FILE.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh, indent=1, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, BLOCK_FILE)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def host_of(domain_or_url: str) -> str:
    host = urlsplit(_as_url(domain_or_url)).netloc.lower()
    return host[4:] if host.startswith("www.") else host


async def record_block(domain: str, kind: str, detail: str = "",
                       exit_country_code: str | None = None,
                       stealth_tried: bool = False) -> dict[str, Any]:
    """Record (or increment) a firewall block against `domain`. Returns the entry."""
    host = host_of(domain)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    async with _block_lock:
        blocks = load_blocks()
        entry = blocks.get(host) or {
            "domain": host, "first_seen": now, "attempts": 0, "resolved": None,
        }
        entry.update({
            "block_type": kind,
            "detail": (detail or "")[:300],
            "last_seen": now,
            "attempts": int(entry.get("attempts", 0)) + 1,
            "exit_country": exit_country_code,
            "stealth_tried": bool(entry.get("stealth_tried")) or stealth_tried,
            "resolved": None,          # a fresh block clears any previous resolution
        })
        blocks[host] = entry
        _atomic_write(blocks)
    logger.warning("crawler: BLOCKED %s (%s) — recorded, attempt %d",
                   host, kind, entry["attempts"])
    return entry


async def clear_block(domain: str, resolved_via: str) -> None:
    """Mark a previously blocked domain as reachable again.

    The entry is kept rather than deleted: 'this host blocks the direct exit but
    answers through a BR exit' is exactly the knowledge Layer 5 should retain.
    """
    host = host_of(domain)
    async with _block_lock:
        blocks = load_blocks()
        if host not in blocks:
            return
        blocks[host]["resolved"] = resolved_via
        blocks[host]["resolved_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        _atomic_write(blocks)
    logger.info("crawler: %s reachable again via %s — block marked resolved", host, resolved_via)


def is_blocked(domain: str) -> dict[str, Any] | None:
    """The unresolved block entry for this domain, or None."""
    entry = load_blocks().get(host_of(domain))
    if entry and not entry.get("resolved"):
        return entry
    return None


def _run_config(page_timeout_ms: int, wait_for: str, **overrides: Any) -> CrawlerRunConfig:
    """Bypass cache so we observe real-time routing, and wait for JS to settle."""
    cfg = dict(
        cache_mode=CacheMode.BYPASS,   # capture live redirects/routing shifts
        wait_for=wait_for,
        page_timeout=page_timeout_ms,
        exclude_all_images=True,       # belt-and-braces with text_mode
        scan_full_page=True,           # lazy-loaded footers hold the login links
        verbose=False,
    )
    cfg.update(overrides)
    return CrawlerRunConfig(**cfg)


def _as_url(domain_or_url: str) -> str:
    """Accept 'buet.ac.bd', 'buet.ac.bd/', or a full URL; return a fetchable URL."""
    raw = (domain_or_url or "").strip()
    if not raw:
        raise ValueError("extract_raw_university_links() needs a domain or URL")
    if "://" not in raw:
        raw = "https://" + raw.lstrip("/")
    return raw


def _www_variant(url: str) -> str | None:
    """Return the conventional ``www`` hostname, if this URL has one.

    A few university edge configurations serve their public site only at the
    ``www`` host.  Chromium can report a generic navigation error for the bare
    hostname even when a normal browser would reach the ``www`` variant.  This
    is deliberately a retry URL, never a substituted reported portal URL.
    """
    parts = urlsplit(url)
    host = parts.hostname or ""
    if not host or host.startswith("www."):
        return None
    port = f":{parts.port}" if parts.port else ""
    return parts._replace(netloc=f"www.{host}{port}").geturl()


def _clean_text(node: Any) -> str:
    """Anchor label, whitespace-collapsed. Falls back to title/aria-label/img alt
    so icon-only links (a very common way to link a portal) are not blank."""
    txt = " ".join((node.get_text(" ") or "").split())
    if txt:
        return txt[:200]
    for attr in ("title", "aria-label"):
        val = (node.get(attr) or "").strip()
        if val:
            return val[:200]
    img = node.find("img")
    if img:
        alt = (img.get("alt") or img.get("title") or "").strip()
        if alt:
            return alt[:200]
    return ""


def _anchors_from_html(html: str, base_url: str) -> list[dict[str, str]]:
    """Every <a href> in the rendered DOM, absolutised and de-duplicated.

    We parse the raw HTML rather than use `result.links`: crawl4ai's link view is
    scored/filtered and returned 3 of 50 anchors on our test page.
    """
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    soup = BeautifulSoup(html or "", "html.parser")
    for a in soup.find_all("a", href=True):
        href = (a["href"] or "").strip()
        if not href or href.lower().startswith(_NON_HTTP_PREFIXES):
            continue
        url = urljoin(base_url, href)
        if not url.lower().startswith(("http://", "https://")):
            continue
        key = url.rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        out.append({"url": url, "text": _clean_text(a)})
    return out


async def extract_static_university_links(
    domain: str,
    *,
    timeout_seconds: int = 6,
    country_hint: str = "",
) -> list[dict[str, str]]:
    """Fetch homepage anchors without Chromium for the batch fast pass.

    Most public university homepages render their navigation server-side. A
    bounded HTTP GET extracts those links in milliseconds/seconds and avoids
    launching a separate browser process. Returning ``[]`` lets the caller
    choose between search rescue and a deeper Chromium pass.
    """
    try:
        url = _as_url(domain)
    except ValueError:
        return []
    timeout = aiohttp.ClientTimeout(total=max(1, timeout_seconds))
    try:
        attempts = [url]
        www_url = _www_variant(url)
        if www_url:
            attempts.append(www_url)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for attempt in attempts:
                routes = _aiohttp_proxy_candidates(attempt, country_hint) \
                    or [("direct", None)]
                for index, (provider, proxy_url) in enumerate(routes):
                    try:
                        async with session.get(
                            attempt,
                            allow_redirects=True,
                            headers={"User-Agent": STATIC_USER_AGENT},
                            proxy=proxy_url,
                            ssl=False,
                        ) as response:
                            if not 200 <= response.status < 400:
                                break
                            content_type = (response.headers.get("Content-Type") or "").lower()
                            if content_type and "html" not in content_type:
                                break
                            body = await response.content.read(STATIC_PAGE_LIMIT_BYTES)
                            html = body.decode(response.charset or "utf-8", errors="ignore")
                            links = _anchors_from_html(html, str(response.url))
                            logger.info(
                                "crawler: static %s -> %d links via %s",
                                attempt, len(links), provider,
                            )
                            if links:
                                return links
                        break
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:  # noqa: BLE001
                        if (index + 1 < len(routes)
                                and _aiohttp_proxy_failure(exc, proxy_url)):
                            logger.info(
                                "crawler: static %s proxy failed via %s; retrying via %s",
                                attempt, provider, routes[index + 1][0],
                            )
                            continue
                        logger.info("crawler: static %s skipped via %s (%s)",
                                    attempt, provider, type(exc).__name__)
                        break
        return []
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — browser fallback owns recovery
        logger.info("crawler: static %s skipped (%s)", url, type(exc).__name__)
        return []


async def _crawl_once(
    crawler: AsyncWebCrawler, url: str, run_cfg: CrawlerRunConfig
) -> tuple[list[dict[str, str]], str]:
    """(links, error). Empty links + '' means the page genuinely had none."""
    try:
        result = await crawler.arun(url=url, config=run_cfg)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # Crawl4AI can raise before it creates a result.
        return [], f"{type(exc).__name__}: {str(exc)[:180]}"
    if not getattr(result, "success", False):
        return [], str(getattr(result, "error_message", "") or "unsuccessful")[:200]
    base = getattr(result, "url", None) or url
    return _anchors_from_html(getattr(result, "html", "") or "", base), ""


async def extract_raw_university_links(
    domain: str,
    *,
    page_timeout_ms: int = DEFAULT_PAGE_TIMEOUT_MS,
    wait_for: str = WAIT_FOR_JS,
    crawler: AsyncWebCrawler | None = None,
    use_proxy: bool = True,
    country_hint: str = "",
    fallback_direct: bool = True,
    retry_failures: bool = True,
    _via: str = "",
) -> list[dict[str, str]]:
    """Render `domain` in headless Chromium and return every hyperlink on it.

    Args:
        domain: bare domain ('buet.ac.bd') or full URL. Scheme is added if absent.
        page_timeout_ms: hard per-page cap.
        wait_for: crawl4ai wait predicate. Defaults to `WAIT_FOR_JS`; pass a CSS
            selector only if you know the element exists, or the crawl will burn
            the full timeout and fail.
        crawler: an already-open `AsyncWebCrawler` to reuse. Batch callers should
            pass one — spinning up a browser per university dominates runtime.
            NOTE its proxy is fixed at construction, so a shared crawler must
            only be used for domains sharing one exit country (see
            `extract_many`, which groups for you).
        use_proxy: route through the residential proxy whose exit country comes
            from the domain's ccTLD. Ignored when `crawler` is supplied.
        country_hint: org country name, used when the domain carries no ccTLD
            (a .com/.org university still needs a correct exit).
        fallback_direct: on a proxied failure, retry once directly.
        retry_failures: run the identical, ``www`` and proxy/direct recovery
            attempts. Fast batch discovery disables these expensive retries;
            unresolved rows are sent to the normal deep pass instead.

    Returns:
        `[{'url': ..., 'text': ...}]`, absolutised and de-duplicated. Always a
        list: every failure path returns `[]` rather than raising, so one dead
        university can never take down a batch run.
    """
    try:
        url = _as_url(domain)
    except ValueError as exc:
        logger.error("crawler: %s", exc)
        return []

    run_cfg = _run_config(page_timeout_ms, wait_for)
    proxy_candidates = (
        proxy_candidates_for(url, country_hint)
        if (use_proxy and crawler is None) else []
    )
    pcfg = proxy_candidates[0][1] if proxy_candidates else None
    # `_via` carries the exit of a SHARED browser, whose proxy was fixed by the
    # caller and is invisible here. Without it a proxied group logs "direct",
    # which is precisely the confusion that has cost us hours diagnosing the
    # fail-closed proxy before.
    cc = (exit_country(url, country_hint) if pcfg else None) or (_via or None)

    try:
        provider = ""
        if crawler is not None:
            links, err = await _crawl_once(crawler, url, run_cfg)
        else:
            attempts = proxy_candidates or [("direct", None)]
            links, err = [], ""
            for index, (provider, candidate) in enumerate(attempts):
                try:
                    async with AsyncWebCrawler(config=_browser_config(candidate)) as own:
                        links, err = await _crawl_once(own, url, run_cfg)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    err = f"{type(exc).__name__}: {str(exc)[:200]}"
                if not err:
                    break
                has_fallback = index + 1 < len(attempts)
                if not (has_fallback and _proxy.is_proxy_failure(err)):
                    break
                next_provider = attempts[index + 1][0]
                logger.warning(
                    "crawler: %s proxy transport failed via %s; retrying via %s",
                    url, provider, next_provider,
                )

        if not err:
            route = f"{provider}:{cc}" if provider and provider != "direct" else (cc or "direct")
            logger.info("crawler: %s -> %d links via %s", url, len(links), route)
            if is_blocked(url):
                await clear_block(url, route)
            return links

        logger.warning("crawler: %s via %s -> %s", url, cc or "direct", err)
        # Chromium/Crawl4AI can occasionally abort a navigation before it
        # returns a CrawlResult (notably under a long-running local batch).
        # That is an infrastructure failure, not evidence that a university
        # has no portal. Retry the identical request once before search rescue
        # is considered.  This keeps a bounded cost while preventing a flaky
        # browser process from becoming a false negative.
        if not retry_failures:
            kind = classify_block(err)
            if kind:
                await record_block(url, kind, err, cc, stealth_tried=True)
            return []

        logger.info("crawler: %s -> retrying once after transient crawl error", url)
        if crawler is not None:
            retry_links, retry_err = await _crawl_once(crawler, url, run_cfg)
        else:
            async with AsyncWebCrawler(config=_browser_config(pcfg)) as retry_crawler:
                retry_links, retry_err = await _crawl_once(retry_crawler, url, run_cfg)
        if not retry_err:
            logger.info("crawler: %s -> %d links on retry via %s",
                        url, len(retry_links), cc or "direct")
            if is_blocked(url):
                await clear_block(url, f"{cc or 'direct'}")
            return retry_links
        logger.warning("crawler: %s -> retry also failed: %s", url, retry_err)
        err = retry_err

        # A university can publish its web presence only on ``www.example``.
        # Try that conventional alias once when the bare host could not be
        # rendered.  Do this before OpenAI search so the primary source stays
        # the institution's own DOM whenever it is available.
        www_url = _www_variant(url)
        if www_url:
            logger.info("crawler: %s -> retrying conventional www hostname", url)
            if crawler is not None:
                www_links, www_err = await _crawl_once(crawler, www_url, run_cfg)
            else:
                async with AsyncWebCrawler(config=_browser_config(pcfg)) as www_crawler:
                    www_links, www_err = await _crawl_once(www_crawler, www_url, run_cfg)
            if not www_err:
                logger.info("crawler: %s -> %d links through %s",
                            www_url, len(www_links), cc or "direct")
                if is_blocked(url):
                    await clear_block(url, f"{cc or 'direct'} via www")
                return www_links
            logger.warning("crawler: %s -> www retry also failed: %s", url, www_err)
            err = www_err
        kind = classify_block(err)
        if kind:
            await record_block(url, kind, err, cc, stealth_tried=True)

        # The proxy fails CLOSED: a blacklisted IP or a dead exit makes a live
        # site look dead. V2 shipped dozens of false "unreachable — remove"
        # verdicts that way. One direct retry distinguishes "site is down" from
        # "our exit is down", and costs a single page load.
        if pcfg and fallback_direct and crawler is None:
            logger.info("crawler: %s -> retrying direct (proxy path failed)", url)
            async with AsyncWebCrawler(config=_browser_config(None)) as plain:
                links, err2 = await _crawl_once(plain, url, run_cfg)
            if not err2:
                logger.info("crawler: %s -> %d links DIRECT (proxy exit %s was the problem)",
                            url, len(links), cc)
                # Reachable, just not through that exit — worth remembering.
                await clear_block(url, "direct")
                return links
            logger.warning("crawler: %s -> direct retry also failed: %s", url, err2)
            kind2 = classify_block(err2)
            if kind2:
                await record_block(url, kind2, err2, None, stealth_tried=True)
        return []

    except asyncio.TimeoutError:
        logger.warning("crawler: %s -> timeout after %dms", url, page_timeout_ms)
        return []
    except asyncio.CancelledError:
        # Never swallow cancellation — it is how a batch shard is shut down.
        raise
    except Exception as exc:  # noqa: BLE001 — network/browser errors must not kill the run
        logger.warning("crawler: %s -> %s: %s", url, type(exc).__name__, str(exc)[:200])
        return []


def _targeted_depth_pages(
    homepage_links: list[dict[str, str]], root_url: str, country_hint: str,
    limit: int,
) -> list[str]:
    """Choose a bounded multilingual frontier for one-level exploration."""
    root_host = host_of(root_url)
    native_terms = tuple(profile_for_country(country_hint).portal_terms)
    scored: list[tuple[int, str]] = []
    seen: set[str] = set()
    for item in homepage_links:
        url = str(item.get("url") or "").strip()
        text = str(item.get("text") or "").strip()
        if not url or url in seen or host_of(url) != root_host:
            continue
        if _DISCOVERY_NOISE_RE.search(url):
            continue
        blob = f"{url} {text}"
        score = 0
        if _DISCOVERY_PAGE_RE.search(blob):
            score += 4
        lowered = blob.lower()
        if any(term.lower() in lowered for term in native_terms if term):
            score += 4
        path = urlsplit(url).path or "/"
        if path.count("/") <= 3:
            score += 1
        if score:
            seen.add(url)
            scored.append((score, url))
    scored.sort(key=lambda row: (-row[0], len(row[1])))
    return [url for _score, url in scored[:limit]]


async def extract_targeted_university_links(
    domain: str,
    *,
    page_timeout_ms: int = DEFAULT_PAGE_TIMEOUT_MS,
    country_hint: str = "",
    max_depth_pages: int = 6,
    retry_failures: bool = True,
) -> list[dict[str, str]]:
    """Homepage plus a small evidence-driven depth-1 crawl.

    Crawling an entire university site is unbounded and mostly returns news.
    This frontier visits only a few same-host student/IT/library/service pages,
    which is where universities commonly publish the actual portal or SSO link.
    """
    homepage = await extract_raw_university_links(
        domain, page_timeout_ms=page_timeout_ms, country_hint=country_hint,
        retry_failures=retry_failures)
    pages = _targeted_depth_pages(homepage, domain, country_hint, max_depth_pages)
    if not pages:
        return homepage

    pcfg = proxy_for(domain, country_hint)
    run_cfg = _run_config(page_timeout_ms, WAIT_FOR_JS)
    sem = asyncio.Semaphore(2)
    discovered: list[dict[str, str]] = []
    async with AsyncWebCrawler(config=_browser_config(pcfg)) as crawler:
        async def visit(url: str) -> None:
            async with sem:
                links, err = await _crawl_once(crawler, url, run_cfg)
            if err:
                logger.info("crawler: depth-1 %s skipped (%s)", url, err[:120])
                return
            logger.info("crawler: depth-1 %s -> %d links", url, len(links))
            discovered.extend(links)

        await asyncio.gather(*(visit(url) for url in pages))

    merged: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    for item in homepage + discovered:
        key = str(item.get("url") or "").rstrip("/")
        if key and key not in seen_urls:
            seen_urls.add(key)
            merged.append(item)
    return merged


async def extract_links_from_evidence_pages(
    pages: Iterable[str],
    *,
    country_hint: str = "",
    page_timeout_ms: int = 20_000,
    limit: int = 4,
) -> list[dict[str, str]]:
    """Render a few official guide/search pages and return their destinations.

    Search engines often cite an IT guide rather than the portal it links to.
    This bounded hop turns that official guide into evidence for the actual
    Moodle/SSO endpoint without recursively crawling the whole site.
    """
    chosen = list(dict.fromkeys(str(p).strip() for p in pages if str(p).strip()))[:limit]
    if not chosen:
        return []
    pcfg = proxy_for(chosen[0], country_hint)
    run_cfg = _run_config(page_timeout_ms, WAIT_FOR_JS)
    sem = asyncio.Semaphore(2)
    out: list[dict[str, str]] = []
    async with AsyncWebCrawler(config=_browser_config(pcfg)) as crawler:
        async def visit(url: str) -> None:
            async with sem:
                links, err = await _crawl_once(crawler, url, run_cfg)
            if not err:
                out.extend(links)
            else:
                logger.info("crawler: search-evidence %s skipped (%s)", url, err[:120])
        await asyncio.gather(*(visit(url) for url in chosen))

    merged: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in out:
        key = str(item.get("url") or "").rstrip("/")
        if key and key not in seen:
            seen.add(key)
            merged.append(item)
    return merged


async def extract_many(
    domains: Iterable[str],
    *,
    concurrency: int = 4,
    page_timeout_ms: int = DEFAULT_PAGE_TIMEOUT_MS,
    use_proxy: bool = True,
    country_hints: dict[str, str] | None = None,
) -> dict[str, list[dict[str, str]]]:
    """Crawl several domains, reusing browsers, bounded by `concurrency`.

    A browser's proxy is fixed when it is constructed, but our exit country is
    chosen PER DOMAIN from the ccTLD — so a single shared browser cannot serve a
    mixed-geography batch without sending Brazilian traffic out of a Philippine
    exit. Domains are therefore grouped by exit country and one browser is
    opened per group (`None` = targets with no inferred/configured geography,
    geo mode off, or an explicitly bypassed home country).

    A browser launch costs seconds; V2 batch runs on this 8-core/8 GB box were
    stable at ~10 concurrent workers and swapped beyond that, so keep
    `concurrency` modest.
    """
    hints = country_hints or {}
    groups: dict[str | None, list[str]] = {}
    for d in domains:
        try:
            u = _as_url(d)
        except ValueError:
            groups.setdefault(None, []).append(d)
            continue
        cc = exit_country(u, hints.get(d, "")) if use_proxy else None
        groups.setdefault(cc, []).append(d)

    results: dict[str, list[dict[str, str]]] = {}
    sem = asyncio.Semaphore(max(1, concurrency))

    for cc, batch in groups.items():
        sample = _as_url(batch[0]) if batch else ""
        pcfg = proxy_for(sample, hints.get(batch[0], "")) if cc else None
        logger.info("crawler: group %s -> %d domain(s)", cc or "direct", len(batch))
        async with AsyncWebCrawler(config=_browser_config(pcfg)) as crawler:
            async def one(d: str) -> None:
                async with sem:
                    links = await extract_raw_university_links(
                        d, page_timeout_ms=page_timeout_ms, crawler=crawler,
                        _via=(cc or "direct"),
                    )
                    # A shared proxied browser cannot fall back to direct, so do
                    # it here with a throwaway browser rather than lose the row.
                    if not links and pcfg:
                        links = await extract_raw_university_links(
                            d, page_timeout_ms=page_timeout_ms,
                            use_proxy=False, fallback_direct=False,
                        )
                    results[d] = links

            await asyncio.gather(*(one(d) for d in batch), return_exceptions=True)
    return results

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
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from crawl4ai import AsyncWebCrawler, BrowserConfig, CacheMode, CrawlerRunConfig

from agent import proxy as _proxy

logger = logging.getLogger("genie.crawler")

#: The correct way to express "wait until the page's JavaScript has settled".
#: A bare selector string here is a silent 30-second failure — see module docs.
WAIT_FOR_JS = "js:() => document.readyState === 'complete'"

DEFAULT_PAGE_TIMEOUT_MS = 30_000

#: Schemes that are anchors in markup but not navigable links.
_NON_HTTP_PREFIXES = ("mailto:", "tel:", "javascript:", "data:", "file:", "sms:", "#")


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

    Reuses `agent.proxy` unchanged: the country comes from the URL's ccTLD (or
    an explicit country name), and `.in` plus bare gTLDs go direct — we already
    egress from India, and a gTLD isn't locked to any country we could target.
    """
    return _proxy.active_country(country_hint, url_or_domain)


def proxy_for(url_or_domain: str, country_hint: str = "") -> dict[str, str] | None:
    """crawl4ai `proxy_config` for this URL, or None for direct."""
    return _proxy.playwright_proxy(country_hint, url_or_domain)


async def probe_proxy(sample_url: str = "https://x.edu.br/") -> tuple[bool, str]:
    """(reachable, detail) for the residential proxy this URL would use.

    Run this before any batch. The proxy FAILS CLOSED: when the provider
    rejects our IP (`401 ip_blacklisted`) every proxied fetch dies, and a
    crawler that can't tell "blocked" from "dead" will report perfectly healthy
    universities as having no portal. In V2 that produced dozens of false
    removals before we started probing first.
    """
    cfg = proxy_for(sample_url)
    if not cfg:
        return True, f"direct (no proxy for {sample_url})"
    try:
        import requests
        p = _proxy.requests_proxies("", sample_url)
        r = requests.get("https://api.ipify.org", proxies=p, timeout=25)
        if r.status_code == 200 and r.text.strip():
            return True, f"exit ip {r.text.strip()} ({exit_country(sample_url)})"
        return False, f"probe returned HTTP {r.status_code}: {r.text[:120]}"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {str(exc)[:160]}"


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


async def _crawl_once(
    crawler: AsyncWebCrawler, url: str, run_cfg: CrawlerRunConfig
) -> tuple[list[dict[str, str]], str]:
    """(links, error). Empty links + '' means the page genuinely had none."""
    result = await crawler.arun(url=url, config=run_cfg)
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
    pcfg = proxy_for(url, country_hint) if (use_proxy and crawler is None) else None
    # `_via` carries the exit of a SHARED browser, whose proxy was fixed by the
    # caller and is invisible here. Without it a proxied group logs "direct",
    # which is precisely the confusion that has cost us hours diagnosing the
    # fail-closed proxy before.
    cc = (exit_country(url, country_hint) if pcfg else None) or (_via or None)

    try:
        if crawler is not None:
            links, err = await _crawl_once(crawler, url, run_cfg)
        else:
            async with AsyncWebCrawler(config=_browser_config(pcfg)) as own:
                links, err = await _crawl_once(own, url, run_cfg)

        if not err:
            logger.info("crawler: %s -> %d links via %s", url, len(links), cc or "direct")
            if is_blocked(url):
                await clear_block(url, f"{cc or 'direct'}")
            return links

        logger.warning("crawler: %s via %s -> %s", url, cc or "direct", err)
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
    opened per group (`None` = the direct group: India and bare gTLDs).

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

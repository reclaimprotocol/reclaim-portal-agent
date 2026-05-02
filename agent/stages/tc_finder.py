"""Stage C.1 — T&C Finder.

For each portal URL discovered by Stage A, locate the T&C / Terms-of-Use
document via three escalating strategies:

1. **Per-portal link discovery** — fetch the portal page (Playwright if
   the portal was JS-rendered, else `HTTP_SESSION`), score every `<a>` for
   T&C-ness, take the highest-scoring valid link.
2. **Per-portal path probing** — if step 1 finds nothing, probe common
   T&C paths (`/terms`, `/privacy`, …) on the portal's base host.
3. **University-level fallback** — if step 2 finds nothing, repeat steps
   1 and 2 against the university's primary domain root.

PDFs are accepted (many T&Cs are PDFs).
"""
from __future__ import annotations

import io
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import TYPE_CHECKING, Any, Iterator
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from ..config import (
    HOMEPAGE_INDICATORS,
    SHARED_PLATFORM_DOMAINS,
    TC_FINDER_PARANOID_MODE,
    TC_HTML_ERROR_INDICATORS,
    TC_PAGE_MAX_ANCHOR_TAGS,
    TC_PAGE_MAX_BYTES,
    TC_PAGE_MIN_BYTES,
    TC_PARANOID_MIN_SIMILARITY,
    TC_PDF_KEYWORDS_NEEDED,
    TC_PDF_MIN_TEXT_LEN,
    TC_PDF_REQUIRED_KEYWORDS,
    TC_STRONG_TITLE_PHRASES,
    TC_TITLE_KEYWORDS,
    TC_URL_ERROR_PATH_PATTERNS,
    TC_URL_ERROR_QUERY_PARAMS,
    TOTAL_TC_BUDGET_SECONDS,
    UNIVERSITY_TC_FALLBACK_PATHS,
)
from . import discovery_rules

if TYPE_CHECKING:
    from ..pipeline import PipelineContext
    from .js_renderer import JSRenderer

logger = logging.getLogger(__name__)


# Anchor-text / href tokens scored for T&C-likelihood. Strongest phrases
# first so a quick "in" check returns the right bucket.
_TC_PHRASE_SCORES_5: tuple[str, ...] = (
    "terms of use", "terms of service", "terms and conditions",
)
_TC_TOKEN_SCORE_3_PRIMARY: str = "terms"
_TC_TOKEN_SCORE_3_TC: tuple[str, ...] = ("t&c", "t & c", "tnc")
_TC_TOKEN_SCORE_2: tuple[str, ...] = ("legal", "disclaimer")
_TC_TOKEN_SCORE_1: str = "privacy"

_TC_PROBE_PATHS: tuple[str, ...] = (
    "/terms",
    "/terms-of-use",
    "/terms-of-service",
    "/terms-and-conditions",
    "/legal",
    "/disclaimer",
    "/privacy",
    "/tnc",
)

_LINK_INELIGIBLE_PREFIXES: tuple[str, ...] = (
    "#", "mailto:", "tel:", "javascript:",
)

# Parallelism caps for the parallel probe / anchor-follow paths.
_TC_PROBE_MAX_WORKERS: int = 8
_TC_ANCHOR_MAX_WORKERS: int = 6


@dataclass
class _TCBudget:
    """Wall-clock budget for the entire Stage C pass (across all portals).
    When exceeded, remaining portals get an empty finding instead of being
    probed."""
    deadline_at: float
    tripped: bool = False

    def remaining(self) -> float:
        return max(0.0, self.deadline_at - time.monotonic())

    def expired(self) -> bool:
        return time.monotonic() >= self.deadline_at

    def trip(self, orgid: str, portal_url: str) -> None:
        if self.tripped:
            return
        self.tripped = True
        logger.warning(
            "[%s] tc-finder budget exceeded; skipping remaining probes (next was %s)",
            orgid, portal_url,
        )


# ============================================================ public API

def run(ctx: "PipelineContext") -> dict[str, Any]:
    """Pipeline entrypoint — runs `find_tc_for_portal` for every Stage A
    portal and returns the per-portal findings."""
    discovery_result = ctx.results.get("discovery") or {}
    portals: list[dict[str, Any]] = discovery_result.get("portals") or []
    domains: list[str] = discovery_result.get("domains") or []
    orgid = ctx.orgid
    js_renderer: "JSRenderer | None" = ctx.deps.get("js_renderer")

    findings: list[dict[str, Any]] = []
    if not portals:
        logger.info("[%s] tc-finder: no portals from Stage A; skipping", orgid)
        return {"tc_findings": findings}

    portal_urls = [p.get("url", "") for p in portals if p.get("url")]
    university_domain = infer_university_domain(portal_urls, domains)
    if university_domain:
        logger.info("[%s] tc-finder: inferred university domain = %s", orgid, university_domain)

    t0 = time.monotonic()
    budget = _TCBudget(deadline_at=t0 + TOTAL_TC_BUDGET_SECONDS)
    for portal in portals:
        portal_url = portal.get("url", "")
        if not portal_url:
            continue
        if budget.expired():
            budget.trip(orgid, portal_url)
            findings.append({"portal_url": portal_url, "tc_url": None, "source": None})
            continue
        t_portal = time.monotonic()
        finding = find_tc_for_portal(
            portal_url=portal_url,
            domains=domains,
            js_rendered_hint=bool(portal.get("js_rendered")),
            js_renderer=js_renderer,
            user_agent=ctx.deps.get("user_agent") or _default_user_agent(),
            http_timeout=int(ctx.deps.get("http_timeout") or 20),
            orgid=orgid,
            university_domain=university_domain,
            budget=budget,
        )
        logger.info(
            "[%s] phase=tc_portal portal=%s took=%.1fs found=%s",
            orgid, portal_url, time.monotonic() - t_portal,
            bool(finding.get("tc_url")),
        )
        findings.append(finding)
    logger.info(
        "[%s] tc-finder total took=%.1fs portals=%d budget_tripped=%s",
        orgid, time.monotonic() - t0, len(portals), budget.tripped,
    )
    return {"tc_findings": findings}


def find_tc_for_portal(
    *,
    portal_url: str,
    domains: list[str],
    js_rendered_hint: bool,
    js_renderer: "JSRenderer | None",
    user_agent: str,
    http_timeout: int,
    orgid: str = "",
    university_domain: str | None = None,
    budget: _TCBudget | None = None,
) -> dict[str, Any]:
    """Apply the 3-step discovery pipeline. Always returns a finding dict
    with `portal_url` set; `tc_url` is None when nothing was found.
    """
    base_finding = {"portal_url": portal_url, "tc_url": None, "source": None}

    # Build the set of allowed hosts: configured domains + every shared-platform root.
    effective_domains = _effective_domains_for(portal_url, domains)

    def _budget_ok() -> bool:
        return budget is None or not budget.expired()

    # ---- Step 1: per-portal link discovery ----
    if not _budget_ok():
        return base_finding
    body, _final_url = _fetch_body(
        portal_url, js_renderer=js_renderer, prefer_js=js_rendered_hint,
        user_agent=user_agent, http_timeout=http_timeout,
    )
    if body:
        url = _pick_top_tc_link(
            body, base_url=portal_url,
            allowed_domains=effective_domains,
            user_agent=user_agent, http_timeout=http_timeout,
        )
        if url:
            logger.info("[%s] tc-finder for %s: found %s via per-portal-link", orgid, portal_url, url)
            return {**base_finding, "tc_url": url, "source": "per-portal-link"}

    # ---- Step 2: per-portal path probing ----
    if not _budget_ok():
        return base_finding
    portal_root = _scheme_host(portal_url)
    url = _probe_tc_paths(
        portal_root,
        allowed_domains=effective_domains,
        user_agent=user_agent, http_timeout=http_timeout,
    )
    if url:
        logger.info("[%s] tc-finder for %s: found %s via per-portal-probe", orgid, portal_url, url)
        return {**base_finding, "tc_url": url, "source": "per-portal-probe"}

    # ---- Step 2.5: shared-platform parent fallback ----
    # When the portal lives on a known shared-platform tenant (e.g.
    # mu.samarth.edu.in is a tenant of samarth.edu.in, sppuapp.digitaluniversity.ac
    # is a tenant of digitaluniversity.ac), probe the platform's *parent*
    # roots with the curated path list. Catches platform-wide T&Cs that
    # every tenant inherits — Samarth's `samarth.edu.in/terms-and-conditions`
    # is the canonical example. Strict gate filters out non-T&C parent pages.
    if not _budget_ok():
        return base_finding
    portal_host = urlsplit(portal_url).netloc.lower().split(":")[0]
    for platform in discovery_rules.KNOWN_SHARED_PLATFORMS:
        on_this_platform = any(
            portal_host == root or portal_host.endswith("." + root)
            for root in platform["roots"]
        )
        if not on_this_platform:
            continue
        for root in platform["roots"]:
            if not _budget_ok():
                return base_finding
            platform_url = _probe_university_fallback_paths(
                f"https://{root}",
                allowed_domains=effective_domains,
                user_agent=user_agent, http_timeout=http_timeout,
            )
            if platform_url:
                logger.info(
                    "[%s] tc-finder for %s: found %s via shared-platform-parent (%s)",
                    orgid, portal_url, platform_url, platform["name"],
                )
                return {**base_finding, "tc_url": platform_url, "source": f"shared-platform-parent:{platform['name']}"}
        break  # portal can only be on one platform

    # ---- Step 3: university-level fallback (curated path list) ----
    # Try the inferred university domain first (most-common base across the
    # OrgID's portals, ignoring shared platforms), then fall back to the
    # SheerID-configured `domains` list. Each candidate gets walked through
    # `UNIVERSITY_TC_FALLBACK_PATHS` in order with content-keyword validation.
    # Homepage anchor-scoring on the uni root used to run here but caused
    # false positives (e.g. an lkouniv news article whose anchor text
    # contained "terms") — the curated list is more deterministic.
    candidates: list[str] = []
    if university_domain:
        candidates.append(university_domain)
    for d in domains:
        d_norm = d.lower().lstrip(".")
        if d_norm and d_norm not in candidates:
            candidates.append(d_norm)
    for uni_domain in candidates:
        if not _budget_ok():
            return base_finding
        uni_root = f"https://{uni_domain}"
        if _scheme_host(uni_root) == portal_root:
            continue
        url = _probe_university_fallback_paths(
            uni_root,
            allowed_domains=effective_domains,
            user_agent=user_agent, http_timeout=http_timeout,
        )
        if url:
            logger.info("[%s] tc-finder for %s: found %s via university-level", orgid, portal_url, url)
            return {**base_finding, "tc_url": url, "source": "university-level"}

    logger.info(
        "[%s] tc-finder for %s: no T&C found "
        "(tried per-portal links, per-portal probe, shared-platform-parent, university-level)",
        orgid, portal_url,
    )
    return base_finding


# ============================================================ scoring + parse

def _score_tc_anchor(href: str, text: str) -> int:
    href_l = (href or "").lower()
    text_l = (text or "").strip().lower()
    combined = href_l + " " + text_l

    if any(p in combined for p in _TC_PHRASE_SCORES_5):
        return 5
    if any(t in combined for t in _TC_TOKEN_SCORE_3_TC):
        return 3
    if _TC_TOKEN_SCORE_3_PRIMARY in combined:
        return 3
    if any(t in combined for t in _TC_TOKEN_SCORE_2):
        return 2
    if _TC_TOKEN_SCORE_1 in combined:
        return 1
    return 0


def _pick_top_tc_link(
    html: str,
    *,
    base_url: str,
    allowed_domains: list[str],
    user_agent: str,
    http_timeout: int,
    min_score: int = 2,
) -> str | None:
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")
    scored: list[tuple[str, int]] = []
    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href", "")
        if not href or any(href.startswith(p) for p in _LINK_INELIGIBLE_PREFIXES):
            continue
        text = anchor.get_text(strip=True) or ""
        s = _score_tc_anchor(href, text)
        if s < min_score:
            continue
        try:
            abs_url = urljoin(base_url, href)
        except Exception:
            continue
        scored.append((abs_url, s))

    # Highest score first; among ties, prefer earlier appearance.
    scored.sort(key=lambda x: -x[1])
    seen: set[str] = set()
    ordered: list[str] = []
    for url, _s in scored:
        if url in seen:
            continue
        seen.add(url)
        ordered.append(url)
    return _parallel_accept_first(
        ordered,
        allowed_domains=allowed_domains,
        user_agent=user_agent, http_timeout=http_timeout,
        max_workers=_TC_ANCHOR_MAX_WORKERS,
    )


def _probe_tc_paths(
    root: str,
    *,
    allowed_domains: list[str],
    user_agent: str,
    http_timeout: int,
) -> str | None:
    urls = [root.rstrip("/") + p for p in _TC_PROBE_PATHS]
    return _parallel_accept_first(
        urls,
        allowed_domains=allowed_domains,
        user_agent=user_agent, http_timeout=http_timeout,
        max_workers=_TC_PROBE_MAX_WORKERS,
    )


def _probe_university_fallback_paths(
    uni_root: str,
    *,
    allowed_domains: list[str],
    user_agent: str,
    http_timeout: int,
) -> str | None:
    """Probe `UNIVERSITY_TC_FALLBACK_PATHS` against the university root in
    parallel; the first path passing the full validation gate (strict +
    final accessibility + paranoid re-fetch), preferring earlier-listed
    paths on ties, wins.

    Order is preserved for ranking (terms → privacy → disclaimer → CMS
    → legacy) so the most-specific match still wins among parallel
    successes — `_parallel_accept_first` honours the input list order
    for tiebreaking."""
    base = uni_root.rstrip("/")
    urls = [base + p for p in UNIVERSITY_TC_FALLBACK_PATHS]
    return _parallel_accept_first(
        urls,
        allowed_domains=allowed_domains,
        user_agent=user_agent, http_timeout=http_timeout,
        max_workers=_TC_PROBE_MAX_WORKERS,
    )


def _parallel_accept_first(
    urls: list[str],
    *,
    allowed_domains: list[str],
    user_agent: str,
    http_timeout: int,
    max_workers: int,
) -> str | None:
    """Run `_accept_candidate` over `urls` concurrently and return the first
    accepted URL by *input order* (not completion order). Stops as soon as
    every URL up to and including the first accepted one has resolved.

    Order matters: callers list URLs from most-specific to least, so the
    earlier accepted URL is preferred on ties even if a later URL finished
    its accept-check first.
    """
    if not urls:
        return None
    results: dict[int, str | None] = {}
    if len(urls) == 1:
        return _accept_candidate(
            urls[0], allowed_domains=allowed_domains,
            user_agent=user_agent, http_timeout=http_timeout,
        )
    with ThreadPoolExecutor(max_workers=min(max_workers, len(urls))) as exe:
        futures = {
            exe.submit(
                _accept_candidate, url,
                allowed_domains=allowed_domains,
                user_agent=user_agent, http_timeout=http_timeout,
            ): idx
            for idx, url in enumerate(urls)
        }
        for fut in as_completed(futures):
            idx = futures[fut]
            try:
                results[idx] = fut.result()
            except Exception as err:
                logger.debug("[tc-finder] accept-check raised on %s: %s", urls[idx], err)
                results[idx] = None
    for idx in range(len(urls)):
        accepted = results.get(idx)
        if accepted:
            return accepted
    return None


# ============================================================ STRICT VALIDATION GATE
#
# Every candidate T&C URL — whether discovered from a portal-page anchor,
# a per-portal path probe, or the university-level fallback list — has to
# pass the same hard gate before it can be returned. The gate enforces:
#
#   1. HTTP final status 200 (after redirects).
#   2. Content-Type is text/html or application/pdf.
#   3. Final URL path/query has no error patterns
#      (custom.htm / aspxerrorpath / etc. — ASPX soft-404 routes can return
#      200 OK with error markup).
#   4. HTML body has no error indicators ("technical issue", "page not
#      found", "an error has occurred", etc.).
#   5. HTML body length in [TC_PAGE_MIN_BYTES, TC_PAGE_MAX_BYTES] OR a
#      strong T&C title (e.g. "Disclaimer - Delhi University") is present.
#   6. <title> or <h1> contains a TC_TITLE_KEYWORD (terms / conditions /
#      privacy / disclaimer / policy / tos / agreement / legal).
#   7. ≤ TC_PAGE_MAX_ANCHOR_TAGS anchors and no HOMEPAGE_INDICATORS
#      (notifications / tenders / grievance redressal / anti-ragging) —
#      bypassed when a strong T&C title is present (CMS-chrome-heavy
#      legitimate pages like DU's actual disclaimer fail these counts).
#   8. PDFs additionally: openable by pdfplumber, ≥ TC_PDF_MIN_TEXT_LEN
#      chars of text, ≥ TC_PDF_KEYWORDS_NEEDED required keywords present,
#      not password-protected.
#
# After the strict validator accepts, `_accept_candidate` runs:
#   * a final HEAD/GET accessibility check on the final URL, and
#   * (when `TC_FINDER_PARANOID_MODE`) a paranoid re-fetch + similarity
#     compare against the first fetch.
#
# Only after all of the above does a URL get returned to the caller.


@dataclass
class _ProbeResult:
    """Structured result of a single strict-validation attempt. Used both
    for the accept/reject decision and for the dry-run diagnostic trace."""
    url: str
    accepted: bool = False
    final_url: str | None = None
    http_status: int | None = None
    content_type: str | None = None
    body_len: int | None = None
    pdf_pages: int | None = None
    decision: str = ""              # "ACCEPT" | "REJECT"
    reason: str = ""                # gate that fired
    body: str = ""                  # captured for paranoid re-fetch
    is_pdf: bool = False
    pdf_bytes: bytes | None = None  # captured for paranoid re-fetch


# Module-level diagnostic trace. When inside `tc_probe_trace()`, every
# strict validation appends a structured `_ProbeResult` to the list so
# scripts/dry_run_tc_finder.py can render the full attempt trail without
# parsing log lines.
_PROBE_TRACE: list[_ProbeResult] | None = None


@contextmanager
def tc_probe_trace() -> Iterator[list[_ProbeResult]]:
    """Capture every probe attempt within the `with` block. The list is
    populated as `_validate_tc_url_strict` and `_accept_candidate` run.
    Cleared on exit. Not thread-safe — agent is single-threaded per OrgID."""
    global _PROBE_TRACE
    captured: list[_ProbeResult] = []
    prior = _PROBE_TRACE
    _PROBE_TRACE = captured
    try:
        yield captured
    finally:
        _PROBE_TRACE = prior


def _record(result: _ProbeResult) -> None:
    if _PROBE_TRACE is not None:
        _PROBE_TRACE.append(result)


def _accept_candidate(
    url: str,
    *,
    allowed_domains: list[str],
    user_agent: str,
    http_timeout: int,
    paranoid: bool = TC_FINDER_PARANOID_MODE,
) -> str | None:
    """Strict validate + final accessibility check + (optional) paranoid
    re-fetch. Returns the *final* URL after redirects on accept, else None.
    Every attempt is recorded into the active probe trace if any."""
    result = _validate_tc_url_strict(
        url, allowed_domains=allowed_domains,
        user_agent=user_agent, http_timeout=http_timeout,
    )
    _record(result)
    if not result.accepted:
        return None

    final_url = result.final_url or url

    # Final accessibility check — cheap HEAD-then-GET to confirm the URL
    # still resolves to 200. Catches transient races (URL was up during
    # validation, dropped during second probe). Logged separately.
    if not _final_accessibility_check(
        final_url, user_agent=user_agent, http_timeout=http_timeout,
    ):
        logger.debug("[tc-finder gate] REJECT %s: final accessibility check failed", final_url)
        rejected = _ProbeResult(
            url=final_url, accepted=False, final_url=final_url,
            decision="REJECT", reason="final accessibility check failed",
        )
        _record(rejected)
        return None

    # Paranoid re-fetch — re-GET, compare similarity. If the second fetch
    # diverges by > (1 - TC_PARANOID_MIN_SIMILARITY) of the first, reject.
    if paranoid:
        ok, reason = _paranoid_recheck(
            final_url, baseline=result,
            user_agent=user_agent, http_timeout=http_timeout,
        )
        if not ok:
            logger.debug("[tc-finder gate] REJECT %s: paranoid re-check %s", final_url, reason)
            rejected = _ProbeResult(
                url=final_url, accepted=False, final_url=final_url,
                decision="REJECT", reason=f"paranoid re-check: {reason}",
            )
            _record(rejected)
            return None

    return final_url


def _validate_tc_url_strict(
    url: str,
    *,
    allowed_domains: list[str],
    user_agent: str,
    http_timeout: int,
) -> _ProbeResult:
    """Hard validation gate. See module-level docstring for the full check
    list. Returns a populated `_ProbeResult` either way — `.accepted` is
    the boolean decision, `.reason` carries which gate fired."""
    pr = _ProbeResult(url=url)

    host = urlsplit(url).netloc.lower().split(":")[0]
    if not _host_on_allowed(host, allowed_domains):
        pr.decision, pr.reason = "REJECT", f"host {host!r} not on allowed list"
        logger.debug("[tc-finder gate] %s %s: %s", pr.decision, url, pr.reason)
        return pr

    # Pre-fetch URL pattern check — catches `/custom.htm?aspxerrorpath=...`
    # before we burn the request budget.
    if _url_has_error_pattern(url):
        pr.decision, pr.reason = "REJECT", "URL matches error pattern"
        logger.debug("[tc-finder gate] %s %s: %s", pr.decision, url, pr.reason)
        return pr

    try:
        resp = discovery_rules.HTTP_SESSION.get(
            url, headers={"User-Agent": user_agent},
            timeout=http_timeout, allow_redirects=True,
        )
    except requests.RequestException as err:
        pr.decision, pr.reason = "REJECT", f"fetch error: {err}"
        logger.debug("[tc-finder gate] %s %s: %s", pr.decision, url, pr.reason)
        return pr

    pr.http_status = resp.status_code
    pr.final_url = resp.url
    pr.content_type = (resp.headers.get("content-type") or "").lower()

    # Status: must be 200 (after redirects). 3xx already followed by requests.
    if resp.status_code != 200:
        pr.decision, pr.reason = "REJECT", f"HTTP {resp.status_code}"
        logger.debug("[tc-finder gate] %s %s: %s", pr.decision, url, pr.reason)
        return pr

    # Re-check error patterns against the FINAL URL after redirect (an ASPX
    # site might redirect /missing → /custom.htm?aspxerrorpath=...).
    if _url_has_error_pattern(resp.url):
        pr.decision, pr.reason = "REJECT", f"final URL matches error pattern ({resp.url})"
        logger.debug("[tc-finder gate] %s %s: %s", pr.decision, url, pr.reason)
        return pr

    is_pdf = "application/pdf" in pr.content_type or url.lower().split("?")[0].endswith(".pdf")
    pr.is_pdf = is_pdf

    if is_pdf:
        ok, reason, pages = _validate_pdf_content(resp.content)
        pr.pdf_pages = pages
        if not ok:
            pr.decision, pr.reason = "REJECT", f"PDF: {reason}"
            logger.debug("[tc-finder gate] %s %s: %s", pr.decision, url, pr.reason)
            return pr
        pr.pdf_bytes = resp.content
        pr.body_len = len(resp.content)
        pr.accepted = True
        pr.decision, pr.reason = "ACCEPT", f"PDF: {pages} pages, content keywords confirmed"
        logger.debug("[tc-finder gate] %s %s: %s", pr.decision, url, pr.reason)
        return pr

    # HTML branch.
    if "text/html" not in pr.content_type and "text/" not in pr.content_type and "application/xhtml" not in pr.content_type:
        pr.decision, pr.reason = "REJECT", f"Content-Type {pr.content_type!r} is not HTML/PDF"
        logger.debug("[tc-finder gate] %s %s: %s", pr.decision, url, pr.reason)
        return pr

    body = resp.text or ""
    pr.body = body
    pr.body_len = len(body)
    if not body.strip():
        pr.decision, pr.reason = "REJECT", "empty body"
        logger.debug("[tc-finder gate] %s %s: %s", pr.decision, url, pr.reason)
        return pr

    body_lower = body.lower()
    for indicator in TC_HTML_ERROR_INDICATORS:
        if indicator in body_lower:
            pr.decision, pr.reason = "REJECT", f"body contains error indicator {indicator!r}"
            logger.debug("[tc-finder gate] %s %s: %s", pr.decision, url, pr.reason)
            return pr

    soup = BeautifulSoup(body, "html.parser")
    title_text = (soup.title.string or "") if soup.title else ""
    h1_texts = [h.get_text(" ", strip=True) for h in soup.find_all("h1")[:5]]
    title_h1 = (title_text + " | " + " | ".join(h1_texts)).lower()

    strong_title = _has_strong_tc_title(title_text)

    # Size gate (bypassed only for strong-title pages with CMS chrome).
    if not strong_title:
        if pr.body_len < TC_PAGE_MIN_BYTES:
            pr.decision, pr.reason = "REJECT", f"body {pr.body_len} bytes < {TC_PAGE_MIN_BYTES} (empty/error)"
            logger.debug("[tc-finder gate] %s %s: %s", pr.decision, url, pr.reason)
            return pr
        if pr.body_len > TC_PAGE_MAX_BYTES:
            pr.decision, pr.reason = "REJECT", f"body {pr.body_len} bytes > {TC_PAGE_MAX_BYTES} (homepage-shaped)"
            logger.debug("[tc-finder gate] %s %s: %s", pr.decision, url, pr.reason)
            return pr

    if not any(kw in title_h1 for kw in TC_TITLE_KEYWORDS):
        pr.decision, pr.reason = "REJECT", f"title/h1 lacks T&C keyword (title/h1={title_h1[:120]!r})"
        logger.debug("[tc-finder gate] %s %s: %s", pr.decision, url, pr.reason)
        return pr

    if not strong_title:
        anchor_count = len(soup.find_all("a"))
        if anchor_count > TC_PAGE_MAX_ANCHOR_TAGS:
            pr.decision, pr.reason = "REJECT", f"{anchor_count} <a> tags > {TC_PAGE_MAX_ANCHOR_TAGS} (portal/homepage-shaped)"
            logger.debug("[tc-finder gate] %s %s: %s", pr.decision, url, pr.reason)
            return pr
        for indicator in HOMEPAGE_INDICATORS:
            if indicator in body_lower:
                pr.decision, pr.reason = "REJECT", f"homepage indicator {indicator!r} in body"
                logger.debug("[tc-finder gate] %s %s: %s", pr.decision, url, pr.reason)
                return pr

    pr.accepted = True
    pr.decision = "ACCEPT"
    pr.reason = (
        f"strong title={title_text.strip()[:80]!r}" if strong_title
        else f"title={title_text.strip()[:80]!r} body={pr.body_len}B"
    )
    logger.debug("[tc-finder gate] ACCEPT %s: %s", url, pr.reason)
    return pr


def _validate_pdf_content(content: bytes) -> tuple[bool, str, int | None]:
    """Returns (ok, reason, pages). PDF must open via pdfplumber, yield
    ≥ TC_PDF_MIN_TEXT_LEN chars of text, contain ≥ TC_PDF_KEYWORDS_NEEDED
    of TC_PDF_REQUIRED_KEYWORDS, and not be password-protected."""
    if not content:
        return False, "empty bytes", None
    try:
        import pdfplumber
    except ImportError:
        return False, "pdfplumber not installed", None
    try:
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            page_count = len(pdf.pages)
            if pdf.metadata and pdf.metadata.get("Encrypted"):
                return False, "PDF is password-protected", page_count
            text = "\n".join((p.extract_text() or "") for p in pdf.pages)
    except Exception as err:
        msg = str(err).lower()
        if "password" in msg or "encrypted" in msg:
            return False, "PDF is password-protected", None
        return False, f"pdfplumber failed: {err}", None
    if len(text) < TC_PDF_MIN_TEXT_LEN:
        return False, f"extracted text {len(text)} chars < {TC_PDF_MIN_TEXT_LEN}", page_count
    text_lower = text.lower()
    hits = sum(1 for kw in TC_PDF_REQUIRED_KEYWORDS if kw in text_lower)
    if hits < TC_PDF_KEYWORDS_NEEDED:
        return False, f"only {hits} required keywords (need ≥{TC_PDF_KEYWORDS_NEEDED})", page_count
    return True, f"{page_count} pages, {hits} keywords", page_count


def _final_accessibility_check(
    url: str, *, user_agent: str, http_timeout: int,
) -> bool:
    """One last GET (HEAD often blocked by Indian-uni servers) to confirm
    the URL still returns 200 with non-empty body. No content checks here —
    just liveness."""
    try:
        resp = discovery_rules.HTTP_SESSION.get(
            url, headers={"User-Agent": user_agent},
            timeout=http_timeout, allow_redirects=True,
        )
    except requests.RequestException:
        return False
    if resp.status_code != 200:
        return False
    if resp.content and len(resp.content) >= TC_PAGE_MIN_BYTES:
        return True
    return False


def _paranoid_recheck(
    url: str, *, baseline: _ProbeResult, user_agent: str, http_timeout: int,
) -> tuple[bool, str]:
    """Re-fetch `url`, run the strict validator again, and confirm the body
    is similar enough to the original baseline (≥ TC_PARANOID_MIN_SIMILARITY).
    Returns (ok, reason). For PDFs we compare byte length and a length-ratio
    heuristic; for HTML we use difflib SequenceMatcher on the first 4KB
    (cheap; full-body comparison is O(n²) on worst case)."""
    second = _validate_tc_url_strict(
        url,
        allowed_domains=[urlsplit(url).netloc.lower().split(":")[0]],
        user_agent=user_agent, http_timeout=http_timeout,
    )
    if not second.accepted:
        return False, f"second fetch did not validate ({second.reason})"
    if baseline.is_pdf and second.is_pdf:
        b1 = baseline.body_len or 0
        b2 = second.body_len or 0
        if b1 == 0 or b2 == 0:
            return False, "empty PDF on one of the fetches"
        ratio = min(b1, b2) / max(b1, b2)
        if ratio < TC_PARANOID_MIN_SIMILARITY:
            return False, f"PDF size diverged ({b1} vs {b2} bytes, ratio={ratio:.2f})"
        return True, f"PDF sizes consistent ({b1} vs {b2} bytes)"
    a = (baseline.body or "")[:4096]
    b = (second.body or "")[:4096]
    if not a or not b:
        return False, "empty body on one of the fetches"
    similarity = SequenceMatcher(None, a, b).ratio()
    if similarity < TC_PARANOID_MIN_SIMILARITY:
        return False, f"body similarity {similarity:.2f} < {TC_PARANOID_MIN_SIMILARITY:.2f}"
    return True, f"body similarity {similarity:.2f}"


def _url_has_error_pattern(url: str) -> bool:
    """True iff the URL's path or query indicates an error-handler endpoint
    (custom.htm, aspxerrorpath, etc.)."""
    parts = urlsplit(url)
    path_lower = (parts.path or "").lower()
    query_lower = (parts.query or "").lower()
    for pat in TC_URL_ERROR_PATH_PATTERNS:
        if pat in path_lower:
            return True
    for param in TC_URL_ERROR_QUERY_PARAMS:
        if param in query_lower:
            return True
    return False


def _has_strong_tc_title(title: str) -> bool:
    """True iff the title's primary phrase (before the first separator) is
    one of `TC_STRONG_TITLE_PHRASES`. Anchored matching — "Disclaimer - DU"
    passes, "Home - Delhi University" doesn't, "Welcome | Disclaimer link in
    footer" wouldn't either (because the primary phrase is "Welcome")."""
    if not title:
        return False
    primary = title.strip().lower()
    for sep in (" - ", " | ", " :: ", " : ", " — "):
        if sep in primary:
            primary = primary.split(sep, 1)[0].strip()
            break
    return primary in TC_STRONG_TITLE_PHRASES


# ============================================================ helpers

def _fetch_body(
    url: str,
    *,
    js_renderer: "JSRenderer | None",
    prefer_js: bool,
    user_agent: str,
    http_timeout: int,
) -> tuple[str, str]:
    """Return `(html, final_url)`. Empty string `html` = unreachable."""
    if prefer_js and js_renderer is not None:
        rendered = js_renderer.render(url)
        if rendered.ok:
            return rendered.html or "", rendered.final_url or url
        # Fall through to plain HTTP if rendering failed.
    try:
        resp = discovery_rules.HTTP_SESSION.get(
            url, headers={"User-Agent": user_agent},
            timeout=http_timeout, allow_redirects=True,
        )
        if 200 <= resp.status_code < 400:
            return resp.text or "", resp.url
    except requests.RequestException:
        pass
    return "", url


def _scheme_host(url: str) -> str:
    p = urlsplit(url)
    return f"{p.scheme.lower()}://{p.netloc.lower().split(':')[0]}"


def _host_on_allowed(host: str, allowed_domains: list[str]) -> bool:
    for d in allowed_domains:
        d = d.lower().lstrip(".")
        if host == d or host.endswith("." + d):
            return True
    return False


def _effective_domains_for(portal_url: str, domains: list[str]) -> list[str]:
    """Configured domains plus the portal's own host (so a T&C link on the
    portal's host is allowed even if the host is e.g. a Samarth tenant)."""
    out = list(domains)
    portal_host = urlsplit(portal_url).netloc.lower().split(":")[0]
    if portal_host and portal_host not in out:
        out.append(portal_host)
    for r in discovery_rules.all_platform_roots():
        if r not in out:
            out.append(r)
    return out


# Multi-part TLDs we know about (mostly Indian + UK ccTLDs since that's
# what the portal corpus skews to). Anything not listed falls back to the
# naive `last-2-parts` heuristic, which is correct for `.com`, `.org` etc.
_MULTIPART_TLDS: frozenset[str] = frozenset({
    "ac.in", "co.in", "edu.in", "gov.in", "org.in", "net.in", "nic.in",
    "ac.uk", "co.uk", "gov.uk", "org.uk",
    "ac.za", "co.za",
})


def _base_domain(host: str) -> str:
    """Best-effort eTLD+1 (or eTLD+2 for known multi-part suffixes).
    Avoids pulling in `tldextract` for one helper."""
    parts = host.lower().lstrip(".").split(".")
    if len(parts) <= 2:
        return ".".join(parts)
    last2 = ".".join(parts[-2:])
    if last2 in _MULTIPART_TLDS and len(parts) >= 3:
        return ".".join(parts[-3:])
    return last2


def infer_university_domain(
    portal_urls: list[str], configured_domains: list[str]
) -> str | None:
    """Pick the most likely university root, ignoring shared-platform tenants.

    Strategy:
      1. Extract the eTLD+1 from each portal host.
      2. Drop bases in `SHARED_PLATFORM_DOMAINS` (samarth, digitaluniversity,
         myloft, knimbus) — these tell us nothing about the owning university.
      3. Return the most frequent remaining base.
      4. If every portal lives on a shared platform, fall back to the first
         non-shared entry in `configured_domains`.
      5. Else None — caller should treat this as "no university root".
    """
    counts: dict[str, int] = {}
    for url in portal_urls:
        host = urlsplit(url).netloc.lower().split(":")[0]
        if not host:
            continue
        base = _base_domain(host)
        if base in SHARED_PLATFORM_DOMAINS:
            continue
        counts[base] = counts.get(base, 0) + 1
    if counts:
        return max(counts.items(), key=lambda kv: kv[1])[0]
    for d in configured_domains:
        d = d.lower().lstrip(".")
        if d and d not in SHARED_PLATFORM_DOMAINS:
            return d
    return None


def _default_user_agent() -> str:
    return "reclaim-portal-agent/0.1"

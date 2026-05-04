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
import json
import logging
import re
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
    EXTERNAL_DOMAIN_BLOCKLIST,
    GEMINI_SEARCH_ENABLED,
    HOMEPAGE_INDICATORS,
    KNOWN_SHARED_PLATFORM_PATTERNS,
    OPENROUTER_API_KEY,
    OPENROUTER_MODEL,
    SHARED_PLATFORM_DOMAINS,
    TC_FINDER_PARANOID_MODE,
    TC_HTML_ERROR_INDICATORS,
    TC_PAGE_MAX_ANCHOR_TAGS,
    TC_PAGE_MAX_BYTES,
    TC_PAGE_MIN_BYTES,
    TC_PARANOID_MIN_SIMILARITY,
    TC_PDF_MIN_TEXT_LEN,
    TC_PDF_PHRASES_NEEDED,
    TC_PDF_REJECTION_HEAD_CHARS,
    TC_PDF_REJECTION_SIGNALS,
    TC_PDF_REQUIRED_PHRASES,
    TC_STRONG_TITLE_PHRASES,
    TC_TITLE_KEYWORDS,
    TC_URL_ERROR_PATH_PATTERNS,
    TC_URL_ERROR_QUERY_PARAMS,
    TC_URL_REJECTION_PATTERNS,
    TOTAL_TC_BUDGET_SECONDS,
    UNIVERSITY_TC_FALLBACK_PATHS,
    load_config,
)
from . import discovery_rules

# Stage C — Samarth platform-level T&C URL. Used as a fallback when the
# university's own website yields no T&C and at least one portal for the
# OrgID is hosted on Samarth (samarth.edu.in / samarth.ac.in). Already
# cached in state.db with verdict=Yes by the analyzer, so no re-fetch
# happens when this URL is selected.
SAMARTH_PLATFORM_TC_URL: str = "https://samarth.edu.in/terms-and-conditions/"

if TYPE_CHECKING:
    from ..pipeline import PipelineContext
    from .js_renderer import JSRenderer

logger = logging.getLogger(__name__)


# ============================================================ Gemini T&C search

def gemini_tc_search(
    orgid: str,
    university_name: str,
    tc_domain: str,
    *,
    http_timeout: float = 30.0,
) -> list[str]:
    """Primary T&C URL search — ask OpenRouter Gemini directly for the
    Terms / Privacy / Disclaimer / Website-Policy URL on the
    university's own domain.

    Returns the raw URL strings Gemini surfaces, filtered to entries
    whose host substring contains `tc_domain` (so a Gemini hallucination
    pointing at an unrelated site is rejected before validation). The
    caller passes the result list to `_parallel_accept_first` so each
    URL is validated by the standard strict gate (status, body, T&C
    keywords, paranoid re-fetch) — Gemini gets no shortcut.

    Disabled / no API key / network failure → returns []. Caller falls
    through to the existing footer-scan + curated-path-probe phases.
    """
    if not GEMINI_SEARCH_ENABLED or not OPENROUTER_API_KEY:
        logger.debug(
            "[%s] Gemini T&C search disabled or no API key", orgid,
        )
        return []
    if not university_name or not tc_domain:
        return []

    prompt = (
        f"Find the Terms and Conditions, Privacy Policy, "
        f"Disclaimer, or Website Policy page URL for "
        f"{university_name} in India "
        f"(official website: {tc_domain}). "
        f"Look for pages with names like: terms, conditions, "
        f"privacy policy, disclaimer, website policy, "
        f"terms of use, legal, hyperlinking policy. "
        f"Return ONLY a JSON array of full URLs, nothing else. "
        f'Example: ["https://xyz.ac.in/privacy-policy", '
        f'"https://xyz.ac.in/terms-and-conditions"]'
    )

    try:
        response = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/reclaimprotocol",
            },
            json={
                "model": OPENROUTER_MODEL,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=http_timeout,
        )
        data = response.json()
    except Exception as err:
        logger.warning("[%s] Gemini T&C search failed: %s", orgid, err)
        return []

    if isinstance(data, dict) and "error" in data:
        logger.warning("[%s] OpenRouter T&C error: %s", orgid, data["error"])
        return []
    try:
        text = data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError, AttributeError):
        logger.warning(
            "[%s] Gemini T&C response missing choices/content: %r",
            orgid, data,
        )
        return []

    logger.info("[%s] Gemini T&C raw response: %s", orgid, text[:200])

    text = re.sub(r"```json|```", "", text).strip()
    match = re.search(r"\[.*?\]", text, re.DOTALL)
    if not match:
        logger.warning("[%s] Gemini T&C response had no JSON array", orgid)
        return []
    try:
        urls = json.loads(match.group())
    except json.JSONDecodeError as err:
        logger.warning(
            "[%s] Gemini T&C JSON parse failed: %s (raw=%r)",
            orgid, err, match.group()[:200],
        )
        return []
    if not isinstance(urls, list):
        return []
    valid = [
        u for u in urls
        if isinstance(u, str)
        and u.startswith(("http://", "https://"))
        and len(u) < 500
        and tc_domain in u
    ]
    logger.info("[%s] Gemini T&C search: %d valid URLs", orgid, len(valid))
    return valid


# Anchor-text / href tokens scored for T&C-likelihood. Strongest phrases
# first so a quick "in" check returns the right bucket.
_TC_PHRASE_SCORES_5: tuple[str, ...] = (
    "terms of use", "terms of service", "terms and conditions",
    "terms & conditions",
    "user agreement", "usage policy",
    "website policy", "website policies",
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
    """Pipeline entrypoint — find ONE T&C URL per OrgID using a strict
    priority order, then apply it to every Stage A portal.

    Priority:

      1. University website T&C — anchor scoring on the university homepage
         and on every portal page, plus curated path probing on the
         university's own domains (primary + ``extra_effective_domains``).
         "University-owned" excludes shared-platform tenants like
         ``samarth.edu.in``: a T&C link served by Samarth doesn't qualify
         as the university's own document, only as a platform fallback.
      2. Samarth platform fallback — when no university T&C was found AND
         at least one portal for this OrgID is hosted on
         ``samarth.edu.in`` / ``samarth.ac.in``, return
         ``SAMARTH_PLATFORM_TC_URL``. The analyzer caches this URL with
         verdict=Yes so no re-fetch happens.
      3. Otherwise leave ``tc_url`` = None and let the analyzer emit
         ``"Yes (No T&C Found)"``.

    The chosen URL is broadcast to every portal in ``tc_findings``. The
    University's own document is more specific and authoritative than
    Samarth's platform-level T&C — so a university whose Samarth tenant
    coexists with a homepage-footer PDF gets the PDF, not the platform
    blanket.
    """
    discovery_result = ctx.results.get("discovery") or {}
    portals: list[dict[str, Any]] = discovery_result.get("portals") or []
    domains: list[str] = discovery_result.get("domains") or []
    # `university_name` is stashed by Stage A on the discovery result;
    # threaded into `find_university_tnc` so the new Phase 0 Gemini T&C
    # search can build a name-anchored prompt without re-reading the
    # Universities sheet.
    university_name: str = str(
        discovery_result.get("university_name") or ""
    ).strip()
    orgid = ctx.orgid
    js_renderer: "JSRenderer | None" = ctx.deps.get("js_renderer")

    if not portals:
        logger.info("[%s] tc-finder: no portals from Stage A; skipping", orgid)
        return {"tc_findings": []}

    # Pull `extra_effective_domains` from the per-OrgID override so the
    # university-owned set matches what discovery used. SheerID's `domains`
    # alone misses HPU-Shimla / CCSU-style multi-domain universities.
    config = load_config()
    overrides = config.domain_overrides.get(orgid, {}) or {}
    extra_effective_domains = [
        str(x).lower().lstrip(".")
        for x in overrides.get("extra_effective_domains", [])
        if x
    ]

    portal_urls = [p.get("url", "") for p in portals if p.get("url")]
    # Fix 2 — `tc_domain` override is the highest-priority answer for
    # the university domain used by T&C lookup. When the override is
    # set, skip `infer_university_domain` entirely. Used for OrgIDs
    # whose portals all live on shared platforms (cognibot / knimbus /
    # samarth) — without the override, inference falls through to a
    # platform domain and the T&C finder probes the wrong site.
    tc_domain_override = (
        str(overrides["tc_domain"]).strip().lower().lstrip(".")
        if overrides.get("tc_domain") else ""
    )
    if tc_domain_override:
        university_domain = tc_domain_override
        logger.info(
            "[%s] tc-finder: tc_domain override = %s (skipping inference)",
            orgid, university_domain,
        )
    else:
        university_domain = infer_university_domain(
            portal_urls, domains, extra_effective_domains=extra_effective_domains,
        )
        if university_domain:
            logger.info("[%s] tc-finder: inferred university domain = %s", orgid, university_domain)

    user_agent = ctx.deps.get("user_agent") or _default_user_agent()
    http_timeout = int(ctx.deps.get("http_timeout") or 20)

    t0 = time.monotonic()
    budget = _TCBudget(deadline_at=t0 + TOTAL_TC_BUDGET_SECONDS)

    # ---- Phase 1 — university website T&C ----
    org_tc_url, org_tc_source = find_university_tnc(
        portals=portals,
        domains=domains,
        extra_effective_domains=extra_effective_domains,
        university_domain=university_domain,
        js_renderer=js_renderer,
        user_agent=user_agent,
        http_timeout=http_timeout,
        orgid=orgid,
        budget=budget,
        university_name=university_name,
    )

    # ---- Phase 2 — Samarth platform fallback ----
    if not org_tc_url:
        has_samarth = any(
            "samarth.edu.in" in (p.get("url") or "") or "samarth.ac.in" in (p.get("url") or "")
            for p in portals
        )
        if has_samarth:
            org_tc_url = SAMARTH_PLATFORM_TC_URL
            org_tc_source = "samarth-platform-fallback"
            logger.info(
                "[%s] tc-finder: no university T&C; using Samarth platform fallback %s",
                orgid, org_tc_url,
            )

    # ---- Phase 3 — broadcast result to every portal (or None) ----
    findings: list[dict[str, Any]] = [
        {
            "portal_url": p.get("url", ""),
            "tc_url": org_tc_url,
            "source": org_tc_source,
        }
        for p in portals if p.get("url")
    ]
    logger.info(
        "[%s] tc-finder total took=%.1fs portals=%d tc_url=%s source=%s budget_tripped=%s",
        orgid, time.monotonic() - t0, len(findings),
        org_tc_url, org_tc_source, budget.tripped,
    )
    return {"tc_findings": findings}


def find_university_tnc(
    *,
    portals: list[dict[str, Any]],
    domains: list[str],
    extra_effective_domains: list[str],
    university_domain: str | None,
    js_renderer: "JSRenderer | None",
    user_agent: str,
    http_timeout: int,
    orgid: str,
    budget: _TCBudget | None = None,
    university_name: str = "",
) -> tuple[str | None, str | None]:
    """Bug 36 — locate a T&C URL on the university's own website using a
    strict 2-phase order. Per-portal scanning is intentionally skipped:
    a Samarth-tenant portal page links to ``samarth.edu.in/terms-and-conditions/``
    which would otherwise win before the university website is consulted.

    "University-owned" = SheerID ``domains`` + per-OrgID
    ``extra_effective_domains``, with ``SHARED_PLATFORM_DOMAINS``
    excluded. Order: ``university_domain`` (the inferred primary, when
    available) first, then the rest.

    Phase 1 — Bug 35 footer scan: fetch each university homepage,
    isolate the ``<footer>`` subtree, score its anchors with the
    PDF-friendly footer scorer (PDFs in footer get a +1 / medium-score
    floor). Falls back to scoring the full page when no footer landmark
    exists.

    Phase 2 — curated path probe: walk ``UNIVERSITY_TC_FALLBACK_PATHS``
    against each university homepage in specificity order
    (terms → privacy → disclaimer → CMS variants → legacy `.html`).

    Returns ``(tc_url, source)`` on hit, ``(None, None)`` otherwise.

    Bug 37 — SheerID's ``domains`` column for some OrgIDs lists
    affiliated colleges (e.g. ``imsnoida.com`` under CCSU). Those are
    NOT this university's legal-document home, so the SheerID list is
    intentionally NOT used as a source for the T&C scan target set.
    Only the inferred ``university_domain`` (most-frequent portal-host
    base, ignoring shared platforms) and the per-OrgID
    ``extra_effective_domains`` override are admitted. The `domains`
    parameter is kept on the signature for back-compat and is ignored
    here.
    """
    del portals  # No longer used here — Samarth fallback owns Samarth portals (handled in run()).
    del domains  # Bug 37 — SheerID list may contain affiliated colleges; do not use.
    uni_domains: list[str] = []
    if university_domain and university_domain not in SHARED_PLATFORM_DOMAINS:
        uni_domains.append(university_domain)
    for d in extra_effective_domains:
        d_n = (d or "").lower().lstrip(".")
        if d_n and d_n not in uni_domains and d_n not in SHARED_PLATFORM_DOMAINS:
            uni_domains.append(d_n)
    if not uni_domains:
        logger.info("[%s] tc-finder Phase 1: no university-owned domains; skipping", orgid)
        return None, None

    def _budget_ok() -> bool:
        return budget is None or not budget.expired()

    # ---- Phase 0 — Gemini T&C URL search ------------------------------
    # Primary search before the static-fallback paths. Ask Gemini for
    # the university's T&C / privacy / disclaimer URL on its own
    # domain; validate every returned URL via the same strict
    # `_parallel_accept_first` gate the curated path-probe uses, so
    # Gemini gets no shortcut — a Gemini hit only wins if it passes
    # `_validate_tc_url_strict` + accessibility + paranoid re-fetch.
    # Disabled / no API key / Gemini empty / nothing validates →
    # silent fall-through to Phase 1 (homepage footer scan) and
    # Phase 2 (curated path probe).
    if (
        GEMINI_SEARCH_ENABLED
        and OPENROUTER_API_KEY
        and university_name
        and _budget_ok()
    ):
        primary_uni_domain = uni_domains[0]
        gemini_urls = gemini_tc_search(
            orgid=orgid,
            university_name=university_name,
            tc_domain=primary_uni_domain,
        )
        if gemini_urls and _budget_ok():
            accepted = _parallel_accept_first(
                gemini_urls,
                allowed_domains=uni_domains,
                user_agent=user_agent,
                http_timeout=http_timeout,
                max_workers=_TC_PROBE_MAX_WORKERS,
                js_renderer=js_renderer,
            )
            if accepted:
                logger.info(
                    "[%s] tc-finder Phase 0: found %s via Gemini search",
                    orgid, accepted,
                )
                return accepted, "gemini-search"
            logger.info(
                "[%s] tc-finder Phase 0: Gemini returned %d URL(s), "
                "none validated; falling through to footer/path probe",
                orgid, len(gemini_urls),
            )

    # ---- Phase 1 — homepage footer scan (each uni-owned domain) ----
    for d in uni_domains:
        if not _budget_ok():
            return None, None
        url = find_tnc_in_university_homepage(
            d, effective_domains=uni_domains,
            js_renderer=js_renderer,
            user_agent=user_agent, http_timeout=http_timeout,
        )
        if url:
            logger.info(
                "[%s] tc-finder Phase 1: found %s via homepage-footer on %s",
                orgid, url, d,
            )
            return url, "university-homepage-footer"

    # ---- Phase 2 — curated path probe (each uni-owned domain) ----
    for d in uni_domains:
        if not _budget_ok():
            return None, None
        uni_root = f"https://{d}"
        url = _probe_university_fallback_paths(
            uni_root,
            allowed_domains=uni_domains,
            user_agent=user_agent, http_timeout=http_timeout,
            js_renderer=js_renderer,
        )
        if url:
            logger.info(
                "[%s] tc-finder Phase 2: found %s via fallback-paths on %s",
                orgid, url, uni_root,
            )
            return url, "university-fallback-paths"

    logger.info("[%s] tc-finder: no university T&C found across %s", orgid, uni_domains)
    return None, None


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
            js_renderer=js_renderer,
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
        js_renderer=js_renderer,
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
                js_renderer=js_renderer,
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

        # Step 3a — Bug 34. Anchor-scan the university homepage. Indian
        # universities frequently link T&C / disclaimer / privacy as
        # PDFs in the page footer (often on a `cdn.<domain>` subdomain,
        # which `_host_on_allowed` already accepts as same-institution
        # via the `endswith("." + d)` check). The strict validation
        # gate (anchor count / homepage indicators / PDF keyword check)
        # filters out non-T&C URLs, and `min_score=3` requires "terms"
        # / "tnc" / "user agreement" in the anchor — both belt-and-
        # suspenders against the prior false-positive (an lkouniv news
        # article whose anchor text contained "terms").
        uni_body, _final = _fetch_body(
            uni_root, js_renderer=js_renderer, prefer_js=False,
            user_agent=user_agent, http_timeout=http_timeout,
        )
        if uni_body:
            url = _pick_top_tc_link(
                uni_body, base_url=uni_root,
                allowed_domains=effective_domains,
                user_agent=user_agent, http_timeout=http_timeout,
                min_score=3,
                js_renderer=js_renderer,
            )
            if url:
                logger.info(
                    "[%s] tc-finder for %s: found %s via university-level-anchor",
                    orgid, portal_url, url,
                )
                return {**base_finding, "tc_url": url, "source": "university-level-anchor"}

        url = _probe_university_fallback_paths(
            uni_root,
            allowed_domains=effective_domains,
            user_agent=user_agent, http_timeout=http_timeout,
            js_renderer=js_renderer,
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
    js_renderer: "JSRenderer | None" = None,
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
        js_renderer=js_renderer,
    )


# ---- Bug 35 — footer-targeted T&C anchor scan ----
#
# Indian university homepages typically render T&C / disclaimer / privacy
# / copyright links in the page footer, often as scanned PDFs uploaded to
# a CDN subdomain. Scoring every anchor on the homepage works but has
# two failure modes: (1) the home rotator/news section can include
# anchors with "terms" in the URL slug that pass the score filter, and
# (2) PDF anchors whose visible text is something like "Click here" /
# "Disclaimer" / a logo image alt-text don't score high enough on the
# generic scorer.
#
# Targeting the footer subtree specifically narrows the search to legal-
# document-shaped anchors and lets us safely promote PDF hrefs to medium
# score (the strict PDF-validation gate then filters out non-T&C PDFs by
# requiring ≥ TC_PDF_PHRASES_NEEDED of TC_PDF_REQUIRED_PHRASES and
# rejecting any TC_PDF_REJECTION_SIGNALS in the document head).

_FOOTER_CLASS_OR_ID_RE: re.Pattern = re.compile(r"\b(footer|site-footer|bottom)\b", re.I)


def _extract_footer_section(soup: BeautifulSoup) -> Any | None:
    """Locate the footer subtree of `soup`. Tries (in order): semantic
    `<footer>`, then `<div>`/`<section>` whose class or id contains
    `footer`/`site-footer`/`bottom` (case-insensitive). Returns the
    matched element or None when no footer-shaped element is present —
    callers may fall back to scanning the full page.
    """
    el = soup.find("footer")
    if el is not None:
        return el
    for tag in ("div", "section", "nav"):
        el = soup.find(tag, attrs={"class": _FOOTER_CLASS_OR_ID_RE})
        if el is not None:
            return el
        el = soup.find(tag, attrs={"id": _FOOTER_CLASS_OR_ID_RE})
        if el is not None:
            return el
    return None


def _pick_top_tc_link_from_footer(
    footer_el: Any,
    *,
    base_url: str,
    allowed_domains: list[str],
    user_agent: str,
    http_timeout: int,
    min_score: int = 2,
    js_renderer: "JSRenderer | None" = None,
) -> str | None:
    """Score every `<a>` inside `footer_el` for T&C-likelihood with a
    PDF-friendly tweak:

      * a PDF href whose anchor text/href already contains a T&C keyword
        gets +1 (so a "Disclaimer" PDF — score 2 — competes with a
        "Terms" HTML page — score 3);
      * a bare PDF in the footer with no T&C keyword anywhere is treated
        as medium score (2). Footer PDFs are nearly always legal docs,
        and the strict PDF gate filters out brochures / syllabi /
        newsletters by enforcing keyword content.

    Sorted highest-first; ties broken by earlier appearance. Returns the
    first URL that passes the strict validation gate.
    """
    if footer_el is None:
        return None
    scored: list[tuple[str, int]] = []
    for anchor in footer_el.find_all("a", href=True):
        href = anchor.get("href", "")
        if not href or any(href.startswith(p) for p in _LINK_INELIGIBLE_PREFIXES):
            continue
        text = anchor.get_text(strip=True) or ""
        score = _score_tc_anchor(href, text)
        is_pdf = href.lower().split("?")[0].split("#")[0].endswith(".pdf")
        if is_pdf:
            score = score + 1 if score > 0 else 2
        if score < min_score:
            continue
        try:
            abs_url = urljoin(base_url, href)
        except Exception:
            continue
        scored.append((abs_url, score))

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
        js_renderer=js_renderer,
    )


def find_tnc_in_university_homepage(
    primary_domain: str,
    *,
    effective_domains: list[str],
    js_renderer: "JSRenderer | None",
    user_agent: str,
    http_timeout: int,
) -> str | None:
    """Bug 35 — fetch the university homepage at `primary_domain`, isolate
    its footer subtree, and return the first valid T&C URL discovered
    there. Allowed hosts are `effective_domains` (typically the OrgID's
    university-owned roots + their subdomains, including `cdn.<root>`).

    Footer detection: semantic `<footer>` first, then class/id-based
    matches for `footer`/`site-footer`/`bottom`. If no footer element is
    found, the helper falls back to scoring anchors on the *full page* —
    same allow-list and PDF boost — so the homepage anchor scan still
    runs even on minimalist sites that omit a footer landmark.
    """
    if not primary_domain:
        return None
    uni_root = f"https://{primary_domain}"
    body, _final = _fetch_body(
        uni_root, js_renderer=js_renderer, prefer_js=False,
        user_agent=user_agent, http_timeout=http_timeout,
    )
    if not body:
        return None
    soup = BeautifulSoup(body, "html.parser")
    target = _extract_footer_section(soup) or soup
    return _pick_top_tc_link_from_footer(
        target, base_url=uni_root,
        allowed_domains=effective_domains,
        user_agent=user_agent, http_timeout=http_timeout,
        js_renderer=js_renderer,
    )


def _probe_tc_paths(
    root: str,
    *,
    allowed_domains: list[str],
    user_agent: str,
    http_timeout: int,
    js_renderer: "JSRenderer | None" = None,
) -> str | None:
    urls = [root.rstrip("/") + p for p in _TC_PROBE_PATHS]
    return _parallel_accept_first(
        urls,
        allowed_domains=allowed_domains,
        user_agent=user_agent, http_timeout=http_timeout,
        max_workers=_TC_PROBE_MAX_WORKERS,
        js_renderer=js_renderer,
    )


def _probe_university_fallback_paths(
    uni_root: str,
    *,
    allowed_domains: list[str],
    user_agent: str,
    http_timeout: int,
    js_renderer: "JSRenderer | None" = None,
) -> str | None:
    """Probe `UNIVERSITY_TC_FALLBACK_PATHS` against the university root in
    parallel; the first path passing the full validation gate (strict +
    final accessibility + paranoid re-fetch), preferring earlier-listed
    paths on ties, wins.

    Order is preserved for ranking (terms → privacy → disclaimer → CMS
    → legacy) so the most-specific match still wins among parallel
    successes — `_parallel_accept_first` honours the input list order
    for tiebreaking.

    Probes BOTH `https://<host>` and `https://www.<host>` when
    `uni_root`'s host doesn't already start with `www.`. Many Indian
    universities only host content on the `www.` subdomain — the bare
    apex returns a redirect to www, but the redirect doesn't always
    follow on direct path probes. Probing both surfaces succeeds when
    only one variant resolves.
    """
    base = uni_root.rstrip("/")
    bases: list[str] = [base]
    parsed = urlsplit(base)
    host = (parsed.netloc or "").lower().split(":")[0]
    if host and not host.startswith("www."):
        scheme = (parsed.scheme or "https").lower()
        bases.append(f"{scheme}://www.{host}")
    # Interleave so the bare-host paths are tried before the www-host
    # paths only when ordering matters; for parallel probing, the
    # `_parallel_accept_first` tiebreak by input position favours
    # bare-host hits first when both succeed.
    urls: list[str] = []
    for b in bases:
        for p in UNIVERSITY_TC_FALLBACK_PATHS:
            urls.append(b + p)
    return _parallel_accept_first(
        urls,
        allowed_domains=allowed_domains,
        user_agent=user_agent, http_timeout=http_timeout,
        max_workers=_TC_PROBE_MAX_WORKERS,
        js_renderer=js_renderer,
    )


def _parallel_accept_first(
    urls: list[str],
    *,
    allowed_domains: list[str],
    user_agent: str,
    http_timeout: int,
    max_workers: int,
    js_renderer: "JSRenderer | None" = None,
) -> str | None:
    """Run `_accept_candidate` over `urls` concurrently and return the first
    accepted URL by *input order* (not completion order). Stops as soon as
    every URL up to and including the first accepted one has resolved.

    Order matters: callers list URLs from most-specific to least, so the
    earlier accepted URL is preferred on ties even if a later URL finished
    its accept-check first.

    Fix 1 — `js_renderer`, when supplied, is threaded into
    `_accept_candidate` for the short-body T&C JS-render fallback.
    Playwright's sync API is bound to its creating thread, so calls
    from worker threads fail gracefully (caught inside
    `js_renderer.render` and returned as `ok=False`); the candidate
    then falls through to the normal short-body rejection. JS-render
    therefore only reliably upgrades the single-URL path, which is
    the common case for the curated `UNIVERSITY_TC_FALLBACK_PATHS`
    probe when only one path matches a `/disclaimer`-shaped URL.
    """
    if not urls:
        return None
    results: dict[int, str | None] = {}
    if len(urls) == 1:
        return _accept_candidate(
            urls[0], allowed_domains=allowed_domains,
            user_agent=user_agent, http_timeout=http_timeout,
            js_renderer=js_renderer,
        )
    with ThreadPoolExecutor(max_workers=min(max_workers, len(urls))) as exe:
        futures = {
            exe.submit(
                _accept_candidate, url,
                allowed_domains=allowed_domains,
                user_agent=user_agent, http_timeout=http_timeout,
                js_renderer=js_renderer,
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
#   1. Pre-fetch URL pattern check — TC_URL_REJECTION_PATTERNS (AICTE /
#      NAAC / accreditation / prospectus / syllabus / result / admit
#      card / tender / recruitment / annual report) reject without
#      fetching. URL is decoded first so percent-encoded spaces are
#      caught.
#   2. HTTP final status 200 (after redirects).
#   3. Content-Type is text/html or application/pdf.
#   4. Final URL path/query has no error patterns
#      (custom.htm / aspxerrorpath / etc. — ASPX soft-404 routes can return
#      200 OK with error markup).
#   5. HTML body has no error indicators ("technical issue", "page not
#      found", "an error has occurred", etc.).
#   6. HTML body length in [TC_PAGE_MIN_BYTES, TC_PAGE_MAX_BYTES] OR a
#      strong T&C title (e.g. "Disclaimer - Delhi University") is present.
#   7. <title> or <h1> contains a TC_TITLE_KEYWORD (terms / conditions /
#      privacy / disclaimer / policy / tos / agreement / legal).
#   8. ≤ TC_PAGE_MAX_ANCHOR_TAGS anchors and no HOMEPAGE_INDICATORS
#      (notifications / tenders / grievance redressal / anti-ragging) —
#      bypassed when a strong T&C title is present (CMS-chrome-heavy
#      legitimate pages like DU's actual disclaimer fail these counts).
#   9. PDFs additionally: openable by pdfplumber, ≥ TC_PDF_MIN_TEXT_LEN
#      chars of text, none of TC_PDF_REJECTION_SIGNALS in the first
#      TC_PDF_REJECTION_HEAD_CHARS chars (Bug 38 — rejects AICTE
#      approval / NAAC / annual-report PDFs outright), ≥
#      TC_PDF_PHRASES_NEEDED of TC_PDF_REQUIRED_PHRASES present, not
#      password-protected.
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
    js_renderer: "JSRenderer | None" = None,
) -> str | None:
    """Strict validate + final accessibility check + (optional) paranoid
    re-fetch. Returns the *final* URL after redirects on accept, else None.
    Every attempt is recorded into the active probe trace if any.

    Fix 1 — `js_renderer`, when supplied, lets `_validate_tc_url_strict`
    fall back to Playwright for short bodies on T&C-shaped URLs."""
    result = _validate_tc_url_strict(
        url, allowed_domains=allowed_domains,
        user_agent=user_agent, http_timeout=http_timeout,
        js_renderer=js_renderer,
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
            js_renderer=js_renderer,
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


# Fix 1 — URL-path tokens that gate the JS-render fallback for short
# T&C bodies. A page at `/disclaimer` / `/terms` / `/privacy` / `/legal`
# / `/policy` whose static body is below `TC_PAGE_MIN_BYTES` is almost
# always a JS-rendered SPA shell rather than a genuinely empty page.
_TC_PATH_TOKENS_FOR_JS_RENDER: tuple[str, ...] = (
    "/disclaimer", "/terms", "/privacy", "/legal", "/policy",
)


def _is_tc_shaped_path(url: str) -> bool:
    """True iff `url`'s path contains a `_TC_PATH_TOKENS_FOR_JS_RENDER`
    token. Used to decide whether a too-short body warrants a Playwright
    re-render.
    """
    path = (urlsplit(url).path or "").lower()
    return any(tok in path for tok in _TC_PATH_TOKENS_FOR_JS_RENDER)


def _validate_tc_url_strict(
    url: str,
    *,
    allowed_domains: list[str],
    user_agent: str,
    http_timeout: int,
    js_renderer: "JSRenderer | None" = None,
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

    # Bug 38 — pre-fetch rejection of non-T&C document URLs (AICTE
    # approvals, NAAC, accreditation, prospectus, …). Cheaper than
    # downloading a 5MB scanned PDF only to fail the phrase check.
    rejected, pat = _url_has_rejection_pattern(url)
    if rejected:
        pr.decision, pr.reason = "REJECT", f"URL path contains non-T&C pattern {pat!r}"
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

    # Fix 1 — JS-render fallback for short T&C bodies. When the static
    # body is below TC_PAGE_MIN_BYTES AND the URL path is T&C-shaped
    # AND a Playwright renderer is supplied, re-fetch via JS-render and
    # re-derive body / title / soup from the rendered HTML before the
    # size gate fires. The strong-title fast-path above already
    # bypassed the size gate; this branch only runs when the static
    # HTML didn't even have a strong title to bypass on.
    if (
        not strong_title
        and pr.body_len < TC_PAGE_MIN_BYTES
        and js_renderer is not None
        and _is_tc_shaped_path(url)
    ):
        try:
            rendered = js_renderer.render(resp.url or url)
        except Exception as err:
            logger.debug("[tc-finder gate] js-render raised on %s: %s", url, err)
            rendered = None
        if rendered is not None and rendered.ok and rendered.html:
            body = rendered.html
            pr.body = body
            pr.body_len = len(body)
            body_lower = body.lower()
            for indicator in TC_HTML_ERROR_INDICATORS:
                if indicator in body_lower:
                    pr.decision, pr.reason = (
                        "REJECT",
                        f"body contains error indicator {indicator!r} (after js-render)",
                    )
                    logger.debug("[tc-finder gate] %s %s: %s", pr.decision, url, pr.reason)
                    return pr
            soup = BeautifulSoup(body, "html.parser")
            title_text = (soup.title.string or "") if soup.title else ""
            h1_texts = [h.get_text(" ", strip=True) for h in soup.find_all("h1")[:5]]
            title_h1 = (title_text + " | " + " | ".join(h1_texts)).lower()
            strong_title = _has_strong_tc_title(title_text)
            logger.debug(
                "[tc-finder gate] js-render upgraded body for %s: %d bytes",
                url, pr.body_len,
            )

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
    """Returns (ok, reason, pages). PDF must:
      * open via pdfplumber (and not be password-protected),
      * yield ≥ TC_PDF_MIN_TEXT_LEN chars of extracted text,
      * have NONE of TC_PDF_REJECTION_SIGNALS in its first
        TC_PDF_REJECTION_HEAD_CHARS chars (Bug 38 — rejects AICTE
        approvals / NAAC / annual reports / prospectuses outright,
        even when the doc happens to mention "liability" elsewhere),
      * contain ≥ TC_PDF_PHRASES_NEEDED of TC_PDF_REQUIRED_PHRASES
        (Bug 38 — phrases not single words; "terms and conditions"
        is much harder to hit incidentally than "terms").
    """
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
    head = text_lower[:TC_PDF_REJECTION_HEAD_CHARS]
    for sig in TC_PDF_REJECTION_SIGNALS:
        if sig in head:
            return False, f"rejection signal {sig!r} in first {TC_PDF_REJECTION_HEAD_CHARS} chars", page_count
    hits = sum(1 for ph in TC_PDF_REQUIRED_PHRASES if ph in text_lower)
    if hits < TC_PDF_PHRASES_NEEDED:
        return False, f"only {hits} T&C phrases (need ≥{TC_PDF_PHRASES_NEEDED})", page_count
    return True, f"{page_count} pages, {hits} T&C phrases", page_count


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
    js_renderer: "JSRenderer | None" = None,
) -> tuple[bool, str]:
    """Re-fetch `url`, run the strict validator again, and confirm the body
    is similar enough to the original baseline (≥ TC_PARANOID_MIN_SIMILARITY).
    Returns (ok, reason). For PDFs we compare byte length and a length-ratio
    heuristic; for HTML we use difflib SequenceMatcher on the first 4KB
    (cheap; full-body comparison is O(n²) on worst case).

    Fix 1 — `js_renderer` is forwarded so the second fetch can also
    use the JS-render fallback. Otherwise a baseline that was upgraded
    via JS-render would diverge from a static-only second fetch and
    paranoid_recheck would always reject."""
    second = _validate_tc_url_strict(
        url,
        allowed_domains=[urlsplit(url).netloc.lower().split(":")[0]],
        user_agent=user_agent, http_timeout=http_timeout,
        js_renderer=js_renderer,
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


def _url_has_rejection_pattern(url: str) -> tuple[bool, str]:
    """Bug 38 — true iff the URL's path contains a known non-T&C document
    pattern (AICTE / NAAC / NIRF / accreditation / prospectus / syllabus /
    result / admit-card / tender / recruitment / annual-report).

    URL-decoded before matching so percent-encoded spaces (e.g.
    ``AICTE%20Approvals%20...``) are caught. Path-only — query strings
    are allowed to incidentally contain these tokens. Returns
    ``(matched, pattern_or_empty)``.
    """
    if not url:
        return False, ""
    parts = urlsplit(url)
    path_raw = (parts.path or "")
    try:
        from urllib.parse import unquote
        path_decoded = unquote(path_raw)
    except Exception:
        path_decoded = path_raw
    path_lower = path_decoded.lower()
    for pat in TC_URL_REJECTION_PATTERNS:
        if pat in path_lower:
            return True, pat
    return False, ""


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


def is_university_owned_domain(
    host: str,
    *,
    primary_domain: str | None,
    extra_effective_domains: list[str],
) -> bool:
    """Bug 37 — true iff `host` is the university's own domain or a
    subdomain thereof (incl. CDN subdomains), or an explicitly
    overridden secondary domain.

    SheerID's "Website Domain" column for some universities also lists
    affiliated colleges (e.g. `imsnoida.com` under Chaudhary Charan
    Singh University). Those are NOT the university's own legal-document
    home — pulling T&C from there gets you the affiliate's AICTE
    approval, not CCSU's disclaimer. This helper is the gate that keeps
    affiliated-college hosts out of T&C discovery.

    True iff:
      * ``host == primary_domain``,
      * ``host`` is a subdomain of ``primary_domain`` (e.g. ``cdn.x``,
        ``forms.x``), or
      * ``host`` equals (or is a subdomain of) any entry in
        ``extra_effective_domains`` — the per-OrgID override is the only
        way to admit a non-subdomain secondary university domain.
    """
    if not host:
        return False
    h = host.lower().lstrip(".")
    p = (primary_domain or "").lower().lstrip(".")
    if p and (h == p or h.endswith("." + p)):
        return True
    for d in extra_effective_domains or []:
        d_n = (d or "").lower().lstrip(".")
        if d_n and (h == d_n or h.endswith("." + d_n)):
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


_PLATFORM_OR_BLOCKLISTED = (
    frozenset(KNOWN_SHARED_PLATFORM_PATTERNS.keys())
    | SHARED_PLATFORM_DOMAINS
    | frozenset(EXTERNAL_DOMAIN_BLOCKLIST)
)


def _is_platform_or_blocklisted(domain: str) -> bool:
    """True iff `domain` equals or is a subdomain of any known shared
    platform (samarth / knimbus / cognibot / bihar-ums / …) OR any
    `EXTERNAL_DOMAIN_BLOCKLIST` entry (social, gov.in / nic.in, …).
    These domains never identify a university's primary website.
    """
    if not domain:
        return False
    d = domain.lower().lstrip(".")
    for entry in _PLATFORM_OR_BLOCKLISTED:
        if d == entry or d.endswith("." + entry):
            return True
    return False


_UNI_TLDS: tuple[str, ...] = (".ac.in", ".edu.in", ".ac.uk", ".edu", ".edu.au")


def infer_university_domain(
    portal_urls: list[str],
    configured_domains: list[str],
    extra_effective_domains: list[str] | None = None,
) -> str | None:
    """Pick the university's primary website root, used as the seed
    domain for T&C discovery.

    Bug C — earlier versions only filtered `SHARED_PLATFORM_DOMAINS`
    (samarth / digitaluniversity / myloft / knimbus). When all of an
    OrgID's portals live on `cognibot.in` / `bihar-ums.com` (also known
    shared platforms but not in that narrow set), inference picked the
    platform domain itself as the "university". The T&C finder then
    probed the platform's homepage for `<domain>/disclaimer` etc. and
    either returned a wrong document or fell through to the Samarth
    fallback when its own SheerID column had university domains right
    there in `configured_domains`.

    Priority order:

      1. The first entry of ``extra_effective_domains`` (per-OrgID
         override) when it's neither platform nor blocklisted. Highest
         confidence — explicitly curated.
      2. The first entry of ``configured_domains`` (SheerID primary)
         when it's neither platform nor blocklisted.
      3. Most-frequent eTLD+1 across ``portal_urls`` whose suffix is in
         ``_UNI_TLDS`` (``.ac.in`` / ``.edu.in`` / `.ac.uk` / etc.) and
         is not platform/blocklisted.
      4. Most-frequent eTLD+1 across ``portal_urls`` that is not
         platform/blocklisted.
      5. Any non-platform, non-blocklisted entry of
         ``configured_domains``.
      6. ``None`` — caller treats as "no university root".
    """
    extras = list(extra_effective_domains or [])

    # 1 — override extras.
    for d in extras:
        d_n = (d or "").lower().lstrip(".")
        if d_n and not _is_platform_or_blocklisted(d_n):
            return d_n

    # 2 — SheerID primary.
    if configured_domains:
        first = (configured_domains[0] or "").lower().lstrip(".")
        if first and not _is_platform_or_blocklisted(first):
            return first

    # 3 + 4 — counts across portal hosts (eTLD+1).
    uni_tld_counts: dict[str, int] = {}
    other_counts: dict[str, int] = {}
    for url in portal_urls:
        host = urlsplit(url).netloc.lower().split(":")[0]
        if not host:
            continue
        base = _base_domain(host)
        if not base or _is_platform_or_blocklisted(base):
            continue
        bucket = uni_tld_counts if any(base.endswith(t) for t in _UNI_TLDS) else other_counts
        bucket[base] = bucket.get(base, 0) + 1
    if uni_tld_counts:
        return max(uni_tld_counts.items(), key=lambda kv: kv[1])[0]
    if other_counts:
        return max(other_counts.items(), key=lambda kv: kv[1])[0]

    # 5 — any non-platform configured entry.
    for d in configured_domains:
        d_n = (d or "").lower().lstrip(".")
        if d_n and not _is_platform_or_blocklisted(d_n):
            return d_n

    return None


def _default_user_agent() -> str:
    return "reclaim-portal-agent/0.1"

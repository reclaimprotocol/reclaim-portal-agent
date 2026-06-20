"""Level-cascade T&C search for the pivoted "17&18June" tab.

For a given student-login portal we look for the governing Terms & Conditions
/ Privacy Policy at a *priority ladder* of locations and record the URL(s) in
the column for the level where they were found. The ladder is walked
LEFT-TO-RIGHT and STOPS at the first level that yields a T&C:

    Level 1  C  Exact URL              — T&C linked on the portal page itself
    Level2-4 D  Parent URL             — walk up the portal URL path, scan each
    Level5-6 E  Parent domain          — drop to the portal's parent domain root
    Level 8  F  Linked Parent Univ.    — uni homepage that links *back* to the
                                          portal → T&C on that uni site
    Level 7  G  Vendor Home page       — third-party vendor linked on the portal
                                          → vendor T&C governing the portal service
    Level 8  H  Unlinked Parent Univ.  — the university homepage itself, resolved
                                          via search (provenance; recorded always)

A level with no T&C found is "n/a". When a level yields several distinct T&C
pages (e.g. a Terms page AND a Privacy page) we return all of them — the caller
emits one sheet row per T&C URL.

Implementation reuses the trained primitives in `tc_finder` (anchor scoring,
strict candidate validation, curated-path probing, Gemini search) so the
notion of "what counts as a real T&C page" stays identical to the rest of the
pipeline. The NEW behaviours here are: explicit per-level classification, the
bidirectional portal<->university link check (F), the vendor-relevance gate
(G), and the search-resolved university homepage (H).
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

from . import tc_finder
from .tc_finder import (
    _LINK_INELIGIBLE_PREFIXES,
    _TC_ANCHOR_MAX_WORKERS,
    _TC_PROBE_PATHS,
    _accept_candidate,
    _fetch_body,
    _score_tc_anchor,
)
from ..config import OPENROUTER_API_KEY, OPENROUTER_MODEL, VENDOR_TC_MAP

if TYPE_CHECKING:
    from .js_renderer import JSRenderer

logger = logging.getLogger("tc_levels")

# Column keys, in cascade order. H is provenance-only (a homepage, not a T&C)
# and is resolved independently of where the T&C was found.
TC_LEVELS: tuple[str, ...] = (
    "exact", "parent_url", "parent_domain", "linked_uni", "vendor", "unlinked_uni",
)

# Indian second-level labels that sit under the ccTLD — so the registrable
# ("parent") domain of x.y.ac.in is y.ac.in, not ac.in.
_CCSLD_IN = frozenset({"ac", "edu", "co", "gov", "org", "nic", "res", "net", "gen", "ind"})

# Anchor-text / href hints that a link points at the university itself.
_UNI_HINTS = ("university", "college", "institute", "vidyalaya", "vidyapeeth", "vishwavidyalaya")
# Vendor-relevance hints — a vendor T&C is only admissible if its text shows it
# governs the *service the vendor provides to institutions*, not just the
# vendor's own marketing site.
_VENDOR_RELEVANCE_HINTS = (
    "client", "institution", "university", "college", "student", "subscriber",
    "customer", "services we provide", "the services", "platform", "portal",
    "user of the", "end user", "licensee",
)


@dataclass
class LevelResult:
    """Per-level outcome for one portal."""
    exact: list[str] = field(default_factory=list)
    parent_url: list[str] = field(default_factory=list)
    parent_domain: list[str] = field(default_factory=list)
    linked_uni: list[str] = field(default_factory=list)
    vendor: list[str] = field(default_factory=list)
    unlinked_uni: list[str] = field(default_factory=list)  # column H (T&C URLs)
    uni_homepage: str | None = None  # the parent-university homepage we resolved
    winning_level: str | None = None
    blocked: bool = False  # portal unreachable (403 / bot challenge / empty)
    spa_tc_hint: bool = False  # T&C present only as JS modal (# anchors), no URL
    timed_out: bool = False  # per-row deadline hit; remaining levels skipped

    def urls_for(self, level: str) -> list[str]:
        return list(getattr(self, level, []) or [])

    @property
    def tc_urls(self) -> list[str]:
        """All T&C URLs at the winning level (one sheet row each)."""
        return self.urls_for(self.winning_level) if self.winning_level else []


# ----------------------------------------------------------------- url helpers

def _host(url: str) -> str:
    return urlsplit(url).netloc.lower().split(":")[0]


# Academic hosts: their own homepages legitimately carry footer-PDF / lower-
# scored T&C links, so they get the generous scan. Non-academic (vendor /
# corporate) roots get the strict scan so marketing PDFs don't pass as terms.
_ACADEMIC_SUFFIXES = (
    ".ac.in", ".edu.in", ".edu", ".gov.in", ".res.in", ".nic.in",
    ".ac.uk", ".edu.au", ".edu.pk", ".edu.np",
)


def _is_academic_host(host: str) -> bool:
    """True for university-owned hosts (academic TLDs) — their own homepages
    legitimately carry footer-PDF / lower-scored T&C links."""
    h = (host or "").lower()
    return ".ac." in h or any(h.endswith(suf) for suf in _ACADEMIC_SUFFIXES)


# Markers of a bot-challenge / block interstitial — the headless browser
# returns 200 with this body instead of the real page, so an empty-handed
# cascade on such a host means "blocked", not "no T&C exists".
_CHALLENGE_MARKERS = (
    "just a moment", "cf-browser-verification", "cf-challenge", "_cf_chl_",
    "attention required", "checking your browser", "enable javascript and cookies",
    "ddos protection by", "captcha-delivery", "px-captcha",
)


def _is_blocked_page(html: str) -> bool:
    """True when `html` is empty or a known bot-challenge interstitial."""
    if not html or len(html) < 200:
        return True
    low = html.lower()
    return any(m in low for m in _CHALLENGE_MARKERS)


# Anchor text that unambiguously names a T&C / privacy page.
_STRONG_TC_TEXT = ("privacy policy", "terms of use", "terms and conditions",
                   "terms of service", "terms & conditions", "legal", "disclaimer")
# Href values that don't navigate anywhere — the link is a JS-driven SPA modal.
_NONNAV_HREF = ("#", "javascript:", "javascript:void(0)")


def _has_spa_tc_anchor(html: str) -> bool:
    """True when the page exposes a clearly-named T&C/privacy link whose href
    goes nowhere (a `#`/`javascript:` SPA modal) — the terms exist but only as
    JS-rendered content with no stable URL to capture."""
    if not html:
        return False
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        text = (a.get_text(strip=True) or "").lower()
        if not any(t in text for t in _STRONG_TC_TEXT):
            continue
        href = a.get("href", "").strip().lower()
        if href in _NONNAV_HREF or href.endswith("/#") or href.endswith("#"):
            return True
    return False


def _registrable_domain(host: str) -> str:
    """The registrable domain of a host: x.y.z.tld -> z.tld, honouring Indian
    ccSLDs (x.y.ac.in -> y.ac.in)."""
    host = (host or "").lstrip(".")
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    if labels[-1] == "in" and labels[-2] in _CCSLD_IN:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def _parent_domain(host: str) -> str | None:
    """The registrable parent domain of a host, or None when `host` is already
    the registrable domain (used for same-site/same-vendor comparison)."""
    reg = _registrable_domain(host)
    return reg if reg != (host or "").lstrip(".") else None


def _parent_hosts(host: str) -> list[str]:
    """Every parent host of `host`, climbing one subdomain label at a time,
    closest first, down to AND including the registrable domain. This is the
    "Parent domain (5-6)" subdomain-trim ladder:
      cdoe.bharatividyapeeth.integratededucation.pwc.in
        -> bharatividyapeeth.integratededucation.pwc.in
        -> integratededucation.pwc.in
        -> pwc.in                                  (the remaining/registrable domain)
    For a plain x.uni.ac.in host this is just [uni.ac.in]."""
    host = (host or "").lstrip(".")
    reg = _registrable_domain(host)
    if host == reg:
        return []  # portal already on the registrable domain — nothing to climb
    labels = host.split(".")
    out: list[str] = []
    for i in range(1, len(labels)):
        cand = ".".join(labels[i:])
        out.append(cand)
        if cand == reg:  # stop at the registrable domain — never the public suffix
            break
    return out


def _path_parents(url: str) -> list[str]:
    """Successive parent-path roots of a URL, most-specific first, excluding
    the bare host root (that belongs to the parent-domain level when the host
    is already registrable, and is covered by the host root otherwise)."""
    p = urlsplit(url)
    base = f"{p.scheme}://{p.netloc}"
    segs = [s for s in p.path.split("/") if s]
    out: list[str] = []
    # Drop the last segment first, then climb. Keep the host root too — a T&C
    # link often lives on the portal host landing page.
    for i in range(len(segs) - 1, -1, -1):
        out.append(base + "/" + "/".join(segs[:i]) if segs[:i] else base)
    if base not in out:
        out.append(base)
    # de-dupe preserving order
    seen: set[str] = set()
    uniq = []
    for u in out:
        u = u.rstrip("/")
        if u and u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq


# --------------------------------------------------------- page T&C extraction

def _all_tc_links_on_page(
    html: str,
    *,
    base_url: str,
    allowed_domains: list[str],
    user_agent: str,
    http_timeout: int,
    js_renderer: "JSRenderer | None",
    min_score: int = 2,
    allow_pdf_boost: bool = True,
) -> list[str]:
    """Every validated T&C/privacy URL reachable from anchors on this page,
    ordered best-score first. Unlike tc_finder._pick_top_tc_link this returns
    ALL accepted pages (so Terms + Privacy both become rows).

    `min_score`/`allow_pdf_boost` are tightened on noisy corporate/vendor roots
    (parent-domain + vendor levels) so a marketing PDF with no T&C keyword
    can't pass as terms; left generous on the institution's own pages where
    footer-PDF legal docs are legitimate."""
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    # T&C/legal links almost always live in the page FOOTER, and footer links
    # are often bare PDFs (legal docs). Anchors inside the footer subtree get
    # the PDF boost + are always-eligible, so a footer "Disclaimer" / "Privacy"
    # link isn't missed even on noisy pages.
    footer = None
    try:
        footer = tc_finder._extract_footer_section(soup)
    except Exception:
        footer = None
    footer_anchors = set()
    if footer is not None:
        for fa in footer.find_all("a", href=True):
            footer_anchors.add(id(fa))

    scored: list[tuple[str, int]] = []
    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        if not href or any(href.startswith(p) for p in _LINK_INELIGIBLE_PREFIXES):
            continue
        text = a.get_text(strip=True) or ""
        in_footer = id(a) in footer_anchors
        s = _score_tc_anchor(href, text)
        is_pdf = href.lower().split("?")[0].split("#")[0].endswith(".pdf")
        # PDF boost applies on permissive levels OR for any footer PDF (footer
        # PDFs are nearly always legal docs; the strict validator filters junk).
        if is_pdf and (allow_pdf_boost or in_footer):
            s = s + 1 if s > 0 else 2
        # A link explicitly NAMED as a legal page (privacy / terms / disclaimer /
        # copyright / legal / cookie / website-policy ...) is always a candidate,
        # even if the generic scorer rates it low (e.g. "Privacy Policy"=1,
        # "Copyright Policy"=0). The strict validator still filters non-T&C pages.
        blob = (href + " " + text).lower()
        is_legal = bool(_LEGAL_LINK_RE.search(blob)) and not _NOT_LEGAL_RE.search(blob)
        if (s < min_score and not is_legal) or (_NOT_LEGAL_RE.search(blob) and s < 3):
            continue
        try:
            # footer legal/PDF links get a small priority so they sort first.
            score = max(s, 2) if is_legal else s
            if in_footer and (is_legal or is_pdf):
                score += 1
            scored.append((urljoin(base_url, href), score))
        except Exception:
            continue
    scored.sort(key=lambda x: -x[1])
    accepted: list[str] = []
    seen: set[str] = set()
    for url, _s in scored:
        if url in seen:
            continue
        seen.add(url)
        final = _validate_tc_page(
            url, allowed_domains=allowed_domains,
            user_agent=user_agent, http_timeout=http_timeout, js_renderer=js_renderer,
        )
        if final and final not in accepted:
            accepted.append(final)
    return accepted


def _probe_all_tc_paths(
    root: str,
    *,
    allowed_domains: list[str],
    user_agent: str,
    http_timeout: int,
    js_renderer: "JSRenderer | None",
) -> list[str]:
    """Try every curated T&C path on `root`; return all that validate. Bounded
    by a local wall-clock cap so a slow/hanging host can't make the broadened
    path list run away (the per-row deadline only checks between levels)."""
    accepted: list[str] = []
    cap = time.monotonic() + _PROBE_BUDGET_SECONDS
    for path in _PROBE_PATHS_EXT:
        if time.monotonic() > cap:
            break
        final = _validate_tc_page(
            root.rstrip("/") + path, allowed_domains=allowed_domains,
            user_agent=user_agent, http_timeout=http_timeout, js_renderer=js_renderer,
        )
        if final and final not in accepted:
            accepted.append(final)
    return accepted


def _outbound_links(html: str, base_url: str) -> list[tuple[str, str]]:
    """(absolute_url, anchor_text) for every external-looking anchor."""
    out: list[tuple[str, str]] = []
    if not html:
        return out
    soup = BeautifulSoup(html, "html.parser")
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        if not href or any(href.startswith(p) for p in _LINK_INELIGIBLE_PREFIXES):
            continue
        try:
            abs_url = urljoin(base_url, href)
        except Exception:
            continue
        if not abs_url.startswith("http") or abs_url in seen:
            continue
        seen.add(abs_url)
        out.append((abs_url, a.get_text(strip=True) or ""))
    return out


# ------------------------------------------------- Gemini T&C search per domain

_SOFT_404_MARKERS = (
    "page not found", "not found", "404 error", "error 404",
    "page does not exist", "page you requested", "doesn't exist", "does not exist",
)
# A real terms/privacy page keeps a T&C-shaped slug in its (post-redirect) URL;
# a slug that redirects to the bare homepage has lost it → not the T&C page.
_TC_SLUG_HINTS = (
    "privacy", "terms", "tnc", "disclaimer", "legal", "condition", "policy",
    "website-polic", "hyperlinking",
)


_HOME_PATHS = ("", "/", "/home", "/index", "/index.html", "/index.php", "/app", "/app/")
_TC_BODY_KEYWORDS = (
    "terms", "privacy", "conditions", "disclaimer", "personal data",
    "cookie", "we collect", "you agree", "use of this", "this policy",
    "copyright", "legal", "intellectual property", "refund",
)
# A link/slug explicitly naming a legal page. Used to (a) always treat such an
# anchor as a candidate regardless of the generic score, and (b) accept the
# page on a single body keyword (it's already been named as legal).
_LEGAL_LINK_RE = re.compile(
    r"privacy|terms|conditions?|disclaimer|copyright|cookie|"
    r"\blegal\b|website[\s_-]*polic|data[\s_-]*protection|acceptable[\s_-]*use|"
    r"refund|hyperlink|\btnc\b|t&c",
    re.I,
)
# NOT a T&C even though the word "legal" / etc. appears — legal-awareness/aid/
# cell news & department pages, news/blog/event articles. Disqualifies a link
# that only matched _LEGAL_LINK_RE via a broad word like "legal".
_NOT_LEGAL_RE = re.compile(
    r"legal[\s_/-]*(awareness|aid|cell|literacy|studies|education|services?|"
    r"department|club|camp|metrolog|heir|maxim|news)|"
    r"/news/|/blog/|/events?/|/notice-?board|/gallery",
    re.I,
)

# Curated T&C paths to probe on a host — broadened with the real-world naming
# variety seen in the 17&18June gold reference (terms_and_conditions.html,
# terms.php, privacy_policy.html, website-policies, copyrights, camelCase
# termsAndConditions, etc.) so we don't miss legitimate pages that the curated
# tc_finder list (8 paths) skips. See [[project_waterfall_tnc_patterns]].
_PROBE_PATHS_EXT: tuple[str, ...] = (
    "/terms-and-conditions", "/terms-conditions", "/termsandconditions",
    "/terms_and_conditions.html", "/terms.php", "/terms-of-use", "/terms-of-service",
    "/terms", "/tnc", "/legal",
    "/privacy-policy", "/privacypolicy", "/privacy_policy.html", "/privacy-statement",
    "/privacy",
    "/disclaimer", "/disclaimer.php",
    "/copyright", "/copyrights", "/website-policies", "/website-policy",
)
# Wall-clock cap for one host's curated-path probe (the broadened list above
# would otherwise let a hanging host eat the whole per-row budget in one level).
_PROBE_BUDGET_SECONDS: float = 25.0

# Global-platform policy pages are NEVER the portal's T&C — they leak in via
# "Sign in with Google" / reCAPTCHA / Analytics / social buttons on portal
# pages. Reject any candidate whose registrable domain is one of these.
_GLOBAL_PLATFORM_HOSTS: frozenset[str] = frozenset({
    "google.com", "google.co.in", "policies.google.com", "gstatic.com",
    "googleapis.com", "youtube.com", "gmail.com",
    "facebook.com", "fb.com", "instagram.com", "twitter.com", "x.com",
    "linkedin.com", "whatsapp.com", "telegram.org", "pinterest.com",
    "microsoft.com", "office.com", "live.com", "bing.com", "apple.com",
    "cloudflare.com", "amazon.com", "adobe.com", "mozilla.org", "w3.org",
    "wordpress.com", "wordpress.org", "jquery.com", "github.com",
})

# Known shared platforms whose T&C lives at the REGISTRABLE ROOT. A tenant
# portal (X.samarth.edu.in) has no T&C of its own, so we skip the slow C
# (SPA login render) and D (path climb) and go straight to E on the platform
# root — this is the common Samarth case and was timing out at 116s/row.
_PLATFORM_TC_AT_ROOT: frozenset[str] = frozenset({"samarth.edu.in", "samarth.ac.in"})


def _on_allowed(host: str, allowed_domains: list[str]) -> bool:
    h = (host or "").lstrip(".")
    return any(h == d.lstrip(".") or h.endswith("." + d.lstrip(".")) for d in allowed_domains)


def _validate_tc_page(
    url: str, *, allowed_domains: list[str], user_agent: str, http_timeout: int,
    js_renderer: "JSRenderer | None",
) -> str | None:
    """Body-based T&C validation for the level cascade. PDFs go through the
    strict PDF gate (real content validation). HTML pages are validated on
    their RENDERED body — not title/h1 or page size — because modern portal/
    CMS T&C pages are SPA shells (thin static HTML, generic app-shell <title>)
    that the strict gate wrongly rejects. Static fetch first; JS-render only
    when the static body is a thin shell. Rejects soft-404s, off-host results,
    and slugs that redirect to the bare homepage."""
    # Never a portal's T&C — Google/Facebook/etc. policy pages leak in via
    # social/sign-in/Analytics widgets.
    if _registrable_domain(_host(url)) in _GLOBAL_PLATFORM_HOSTS:
        return None
    is_pdf = url.lower().split("?")[0].split("#")[0].endswith(".pdf")
    if is_pdf:
        return _accept_candidate(
            url, allowed_domains=allowed_domains, user_agent=user_agent,
            http_timeout=http_timeout, js_renderer=js_renderer,
        )
    body, final = _fetch_body(
        url, js_renderer=None, prefer_js=False,
        user_agent=user_agent, http_timeout=http_timeout,
    )
    text = BeautifulSoup(body, "html.parser").get_text(" ", strip=True).lower() if body else ""
    # Thin static shell (SPA) → render to get the real content.
    if js_renderer is not None and (len(text) < 500 or _is_blocked_page(body)):
        body, final = _fetch_body(
            url, js_renderer=js_renderer, prefer_js=True,
            user_agent=user_agent, http_timeout=http_timeout,
        )
        text = BeautifulSoup(body, "html.parser").get_text(" ", strip=True).lower() if body else ""
    if not body or _is_blocked_page(body) or not _on_allowed(_host(final), allowed_domains):
        return None
    # Redirect-to-homepage guard: a T&C slug that lands on the bare home page
    # isn't the T&C page (e.g. /terms-conditions -> /home).
    req_tc = any(k in urlsplit(url).path.lower() for k in _TC_SLUG_HINTS)
    if req_tc and urlsplit(final).path.lower().rstrip("/") in [p.rstrip("/") for p in _HOME_PATHS]:
        return None
    if len(text) < 500 or any(m in text for m in _SOFT_404_MARKERS):
        return None
    # A page already NAMED as legal (privacy/terms/copyright/disclaimer/... in
    # its slug) only needs one body keyword; an unnamed candidate needs two.
    pl = urlsplit(url).path.lower()
    named_legal = (req_tc or _LEGAL_LINK_RE.search(pl)) and not _NOT_LEGAL_RE.search(pl)
    needed = 1 if named_legal else 2
    return final if sum(k in text for k in _TC_BODY_KEYWORDS) >= needed else None


def _gemini_tc_urls(
    orgid: str, host: str, *, name: str, user_agent: str, http_timeout: int,
    js_renderer: "JSRenderer | None",
) -> list[str]:
    """Ask Gemini for the Terms / Privacy / Disclaimer URLs on `host` (these
    pages exist even when nothing is footer-linked), then validate each with
    the lighter JS-aware gate. Returns the accepted URLs (host-scoped)."""
    try:
        cands = tc_finder.gemini_tc_search(
            orgid, name or host, host, http_timeout=float(http_timeout),
        )
    except Exception:
        logger.warning("[%s] gemini T&C search raised for %s", orgid, host)
        return []
    accepted: list[str] = []
    for u in cands:
        final = _validate_tc_page(
            u, allowed_domains=[host], user_agent=user_agent,
            http_timeout=http_timeout, js_renderer=js_renderer,
        )
        if final and final not in accepted:
            accepted.append(final)
    return accepted


# --------------------------------------------------------------- search for H

def _gemini_university_for_portal(orgid: str, portal_url: str, *, http_timeout: float = 30.0) -> str | None:
    """Ask the LLM which university/college owns this student portal and return
    its official homepage URL. Used for column H (the unlinked-parent fallback)
    when nothing on the portal page tells us the university."""
    if not OPENROUTER_API_KEY or not portal_url:
        return None
    prompt = (
        f"This is a student login portal URL: {portal_url}\n"
        f"Which university or college in India does this student portal belong to, "
        f"and what is that institution's official main homepage URL? "
        f"Return ONLY a JSON array with the single best homepage URL, e.g. "
        f'["https://www.example.ac.in/"]. No other text.'
    )
    try:
        resp = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/reclaimprotocol",
            },
            json={"model": OPENROUTER_MODEL, "messages": [{"role": "user", "content": prompt}]},
            timeout=http_timeout,
        )
        content = resp.json()["choices"][0]["message"]["content"]
    except Exception as err:
        logger.warning("[%s] university-for-portal search failed: %s", orgid, err)
        return None
    try:
        start, end = content.find("["), content.rfind("]")
        urls = json.loads(content[start:end + 1]) if start >= 0 and end > start else []
    except Exception:
        urls = []
    for u in urls:
        u = tc_finder._sanitize_gemini_url(str(u))
        if u.startswith("http"):
            return u
    return None


def _gemini_homepage_search(university_name: str, *, http_timeout: float = 30.0) -> str | None:
    """Ask the LLM for the official homepage of the university/college. Used
    for column H when we have no portal->university link to follow."""
    if not OPENROUTER_API_KEY or not university_name:
        return None
    prompt = (
        f"What is the official main homepage URL of {university_name} in India? "
        f"Return ONLY a JSON array with the single best homepage URL, e.g. "
        f'["https://www.example.ac.in/"]. No other text.'
    )
    try:
        resp = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/reclaimprotocol",
            },
            json={"model": OPENROUTER_MODEL, "messages": [{"role": "user", "content": prompt}]},
            timeout=http_timeout,
        )
        content = resp.json()["choices"][0]["message"]["content"]
    except Exception as err:
        logger.warning("homepage search failed for %r: %s", university_name, err)
        return None
    try:
        start, end = content.find("["), content.rfind("]")
        urls = json.loads(content[start:end + 1]) if start >= 0 and end > start else []
    except Exception:
        urls = []
    for u in urls:
        u = tc_finder._sanitize_gemini_url(str(u))
        if u.startswith("http"):
            return u
    return None


# --------------------------------------------------------------- the cascade

def find_tc_levels(
    portal_url: str,
    *,
    orgid: str,
    university_name: str = "",
    domains: list[str] | None = None,
    js_renderer: "JSRenderer | None" = None,
    user_agent: str,
    http_timeout: int,
    deadline: float | None = None,
) -> LevelResult:
    """Walk the level ladder for one portal and return the per-level findings.

    Stops at the first level (exact -> parent_url -> parent_domain ->
    linked_uni -> vendor) that yields a validated T&C. Column H (uni homepage)
    is resolved independently and recorded whenever we can identify it.

    `deadline` (a time.monotonic() value): once passed, the remaining levels are
    skipped — a per-row wall-clock cap so one pathologically slow host can't
    hang the whole run. Whatever was found before the deadline is kept.
    """
    import time as _time
    domains = [d.strip() for d in (domains or []) if d.strip()]
    res = LevelResult()
    portal_host = _host(portal_url)
    # Root/homepage pages fetched during the cascade (host root in D, parent-
    # domain roots in E). Reused by the vendor level (G) to harvest "website
    # by X" developer credits + third-party links that live in the homepage
    # footer rather than on the login page.
    root_pages: list[tuple[str, str]] = []

    def _expired() -> bool:
        if deadline is not None and _time.monotonic() > deadline:
            res.timed_out = True
            return True
        return False

    def _stop() -> bool:
        return bool(res.winning_level) or _expired()

    # Known-platform fast-path: a tenant of a shared platform whose T&C lives
    # at the registrable root (Samarth) has no T&C on its own login page or
    # path — skip C (slow SPA render) and D (path climb) and go straight to E.
    reg_dom = _registrable_domain(portal_host)
    skip_cd = reg_dom in _PLATFORM_TC_AT_ROOT and reg_dom != portal_host

    portal_html, portal_final = "", portal_url
    if not skip_cd:
        # Fetch the portal page: static first (fast); JS-render only when the
        # static page is a thin SPA shell or blocked — avoids slow rendering on
        # the many non-SPA portals.
        portal_html, portal_final = _fetch_body(
            portal_url, js_renderer=None, prefer_js=False,
            user_agent=user_agent, http_timeout=http_timeout,
        )
        if js_renderer is not None and (len(portal_html) < 2000 or _is_blocked_page(portal_html)):
            rendered_html, rendered_final = _fetch_body(
                portal_url, js_renderer=js_renderer, prefer_js=True,
                user_agent=user_agent, http_timeout=http_timeout,
            )
            if rendered_html and len(rendered_html) > len(portal_html):
                portal_html, portal_final = rendered_html, rendered_final
        if _is_blocked_page(portal_html):
            res.blocked = True
            logger.info("[%s] portal page blocked/unreadable (403 / bot challenge): %s", orgid, portal_url)
            portal_html = ""  # don't scan a challenge interstitial for links
        elif _has_spa_tc_anchor(portal_html):
            res.spa_tc_hint = True

    # ---- Level 1 (C) — Exact URL: T&C linked on the portal page itself ----
    if not skip_cd:
        res.exact = _dedupe(_all_tc_links_on_page(
            portal_html, base_url=portal_final or portal_url, allowed_domains=[portal_host],
            user_agent=user_agent, http_timeout=http_timeout, js_renderer=js_renderer,
        ))
        if res.exact:
            res.winning_level = "exact"

    # ---- Level 2-4 (D) — Parent URL: climb the portal URL *path* only.
    # Static-first per path-parent (no per-page JS render — that was costing
    # ~40s/parent); curated-path probe runs only on the host ROOT, not every
    # path level, to keep D fast.
    if not _stop() and not skip_cd:
        found: list[str] = []
        parents = _path_parents(portal_url)
        host_root = f"https://{portal_host}"
        for parent in parents:
            if _host(parent) != portal_host:
                continue
            html, final = _fetch_body(
                parent, js_renderer=None, prefer_js=False,
                user_agent=user_agent, http_timeout=http_timeout,
            )
            if _is_blocked_page(html):
                html = ""
            found += _all_tc_links_on_page(
                html, base_url=final or parent, allowed_domains=[portal_host],
                user_agent=user_agent, http_timeout=http_timeout, js_renderer=js_renderer,
            )
            if parent.rstrip("/") == host_root.rstrip("/"):
                if html:
                    root_pages.append((html, final or parent))
                found += _probe_all_tc_paths(
                    parent, allowed_domains=[portal_host],
                    user_agent=user_agent, http_timeout=http_timeout, js_renderer=js_renderer,
                )
            if found:
                break
        res.parent_url = _dedupe(found)
        if res.parent_url:
            res.winning_level = "parent_url"

    # ---- Level 5-6 (E) — Parent domain: trim subdomain labels one at a time,
    # closest first, down to AND including the remaining registrable domain
    # (cdoe.X.Y.pwc.in -> X.Y.pwc.in -> Y.pwc.in -> pwc.in). Stop at first T&C.
    if not _stop():
        for parent_dom in _parent_hosts(portal_host):
            root = f"https://{parent_dom}"
            html, final = _fetch_body(
                root, js_renderer=js_renderer, prefer_js=False,
                user_agent=user_agent, http_timeout=http_timeout,
            )
            if _is_blocked_page(html):
                html = ""
            elif _has_spa_tc_anchor(html):
                res.spa_tc_hint = True
            if html:
                root_pages.append((html, final or root))
            # Generous scan on the university's OWN domain (footer PDFs are
            # legit T&C); strict on vendor/corporate roots (suppress marketing).
            academic = _is_academic_host(parent_dom)
            found = _all_tc_links_on_page(
                html, base_url=final or root, allowed_domains=[parent_dom],
                user_agent=user_agent, http_timeout=http_timeout, js_renderer=js_renderer,
                min_score=2 if academic else 3, allow_pdf_boost=academic,
            )
            found += _probe_all_tc_paths(
                root, allowed_domains=[parent_dom],
                user_agent=user_agent, http_timeout=http_timeout, js_renderer=js_renderer,
            )
            # Parent domains often DON'T footer-link their terms/privacy even
            # though those pages exist — ask Gemini directly and LIST them too.
            # Gated to academic hosts so a vendor's corporate root (pwc.in)
            # doesn't surface unrelated corporate terms as the portal's.
            if academic:
                found += _gemini_tc_urls(
                    orgid, parent_dom, name=university_name,
                    user_agent=user_agent, http_timeout=http_timeout, js_renderer=js_renderer,
                )
            if found:
                res.parent_domain = _dedupe(found)
                res.winning_level = "parent_domain"
                break

    # Identify a university homepage linked from the portal (used by F + H).
    uni_home = _find_university_link(portal_html, portal_url, university_name, domains)

    # ---- Level 8 (F) — Linked Parent University (bidirectional link) ----
    if not _stop() and uni_home:
        uni_host = _host(uni_home)
        uni_html, uni_final = _fetch_body(
            uni_home, js_renderer=js_renderer, prefer_js=False,
            user_agent=user_agent, http_timeout=http_timeout,
        )
        # F REQUIRES the bidirectional link: the university site must clearly
        # hyperlink the student-login portal before we trust its T&C.
        if uni_html and _links_back_to(uni_html, uni_final or uni_home, portal_host):
            found = _all_tc_links_on_page(
                uni_html, base_url=uni_final or uni_home, allowed_domains=[uni_host],
                user_agent=user_agent, http_timeout=http_timeout, js_renderer=js_renderer,
            )
            found += _probe_all_tc_paths(
                f"https://{uni_host}", allowed_domains=[uni_host],
                user_agent=user_agent, http_timeout=http_timeout, js_renderer=js_renderer,
            )
            # Same as E — the uni's terms/privacy may exist but not be
            # footer-linked; ask Gemini directly and LIST them too.
            found += _gemini_tc_urls(
                orgid, uni_host, name=university_name,
                user_agent=user_agent, http_timeout=http_timeout, js_renderer=js_renderer,
            )
            res.linked_uni = _dedupe(found)
            if res.linked_uni:
                res.winning_level = "linked_uni"

    # ---- Level 7 (G) — Vendor Home page (with relevance gate) ----
    if not _stop():
        res.vendor = _dedupe(_find_vendor_tc(
            portal_html, portal_url, portal_host, orgid=orgid,
            user_agent=user_agent, http_timeout=http_timeout, js_renderer=js_renderer,
            extra_pages=root_pages,
        ))
        if res.vendor:
            res.winning_level = "vendor"

    # ---- Level 8 (H) — Unlinked Parent University: LAST-RESORT fallback ----
    # Only when C-G all found nothing. Identify the portal's parent university
    # (whatever it is — no backlink required, unlike F) and record THAT
    # university's T&C page(s) in column H.
    if not _stop():
        uni = uni_home  # a uni link on the portal page, if any
        if not uni and domains:
            uni = f"https://{domains[0].lstrip('.')}"
        if not uni:
            uni = _gemini_university_for_portal(orgid, portal_url, http_timeout=http_timeout)
        if uni:
            res.uni_homepage = uni
            uni_host = _host(uni)
            html, final = _fetch_body(
                uni, js_renderer=js_renderer, prefer_js=js_renderer is not None,
                user_agent=user_agent, http_timeout=http_timeout,
            )
            found = []
            if html and not _is_blocked_page(html):
                found += _all_tc_links_on_page(
                    html, base_url=final or uni, allowed_domains=[uni_host],
                    user_agent=user_agent, http_timeout=http_timeout, js_renderer=js_renderer,
                )
            found += _probe_all_tc_paths(
                f"https://{uni_host}", allowed_domains=[uni_host],
                user_agent=user_agent, http_timeout=http_timeout, js_renderer=js_renderer,
            )
            found += _gemini_tc_urls(
                orgid, uni_host, name=university_name,
                user_agent=user_agent, http_timeout=http_timeout, js_renderer=js_renderer,
            )
            if found:
                res.unlinked_uni = _dedupe(found)
                res.winning_level = "unlinked_uni"

    return res


def _find_university_link(
    portal_html: str, portal_url: str, university_name: str, domains: list[str]
) -> str | None:
    """A university-homepage URL referenced from the portal page (or a known
    configured uni domain), used to drive levels F and H."""
    portal_host = _host(portal_url)
    portal_parent = _parent_domain(portal_host) or portal_host
    name_tokens = [t.lower() for t in (university_name or "").split() if len(t) > 3]
    for abs_url, text in _outbound_links(portal_html, portal_url):
        h = _host(abs_url)
        if not h or h == portal_host or (_parent_domain(h) or h) == portal_parent:
            continue  # same site / same vendor host — not the university
        blob = (abs_url + " " + text).lower()
        looks_uni = (
            any(hint in blob for hint in _UNI_HINTS)
            or h.endswith(".ac.in") or h.endswith(".edu.in") or h.endswith(".edu")
            or any(tok in blob for tok in name_tokens)
        )
        if looks_uni:
            return f"https://{h}/"
    # No link on the page — fall back to a configured university domain.
    if domains:
        return f"https://{domains[0].lstrip('.')}"
    return None


def _looks_like_university(host: str, html: str) -> bool:
    """True when a vendor *candidate* is actually a university/college site —
    its domain or its homepage title/headings name an institution. Such a
    site's T&C belongs to the linked/unlinked-university levels (F/H), not the
    vendor level (G). Used to reject e.g. a "developed by OUAT" credit that
    resolves to the university's own (non-.ac.in is also covered) site."""
    if any(hint in (host or "").lower() for hint in _UNI_HINTS):
        return True
    if not html:
        return False
    soup = BeautifulSoup(html, "html.parser")
    parts: list[str] = []
    if soup.title and soup.title.string:
        parts.append(soup.title.string)
    for tag in soup.find_all(["h1", "h2"], limit=5):
        parts.append(tag.get_text(" ", strip=True))
    blob = " ".join(parts).lower()
    return any(hint in blob for hint in _UNI_HINTS)


def _links_back_to(html: str, base_url: str, portal_host: str) -> bool:
    """True iff the page has any anchor pointing at the portal host — the
    proof the university officially owns the portal (level F gate)."""
    for abs_url, _text in _outbound_links(html, base_url):
        if _host(abs_url) == portal_host:
            return True
    return False


_POWERED_BY_RE = re.compile(
    r"(?:powered|developed|designed|maintained|created|built|made|run|hosted|"
    r"managed|provided|supplied|crafted|engineered)\s+by\s+([A-Za-z][\w .&'-]{2,40})",
    re.I,
)


def _vendor_hosts_from_name(orgid: str, portal_html: str, portal_url: str, *, http_timeout: int):
    """Vendors NAMED on the portal page but not necessarily hyperlinked: known
    VENDOR_TC_MAP signatures + "powered by X" phrases. For a named vendor with
    no usable host we Gemini-resolve its homepage. Returns (hosts, known_urls)."""
    hosts: list[str] = []
    known_urls: list[str] = []
    text = (BeautifulSoup(portal_html, "html.parser").get_text(" ", strip=True).lower()
            if portal_html else "")
    if not text:
        return hosts, known_urls
    for info in VENDOR_TC_MAP.values():
        for sig in info.get("signatures", ()):
            if sig.lower() in text:
                tcu = (info.get("tc_url") or "").strip()
                if tcu:
                    known_urls.append(tcu)
                hsig = sig.lower() if ("." in sig and " " not in sig) else (_host(tcu) if tcu else "")
                if hsig and hsig not in hosts:
                    hosts.append(hsig)
                break
    for m in _POWERED_BY_RE.finditer(text):
        name = m.group(1).strip(" .'")
        if len(name) < 3 or name in ("the", "us", "team"):
            continue
        home = _gemini_homepage_search(f"{name} (campus/ERP software vendor) India", http_timeout=http_timeout)
        if home:
            h = _parent_domain(_host(home)) or _host(home)
            if h and h not in hosts:
                hosts.append(h)
    return hosts, known_urls


def _find_vendor_tc(
    portal_html: str, portal_url: str, portal_host: str, *, orgid: str = "",
    user_agent: str, http_timeout: int, js_renderer: "JSRenderer | None",
    extra_pages: list[tuple[str, str]] | None = None,
) -> list[str]:
    """Follow third-party vendor links — OR vendors merely NAMED ("powered/
    developed by X" / known signatures, Gemini-resolved) — and collect T&C
    pages that pass the vendor-relevance gate (must govern the service the
    vendor provides to its clients, not just the marketing site).

    Harvests vendor links/credits from the login page AND the homepage /
    parent-domain root pages already fetched in the cascade (`extra_pages`):
    "website developed by X" credits typically sit in the homepage footer,
    not on the login page (e.g. vivacollege.in -> vssdevelopers.com)."""
    portal_parent = _parent_domain(portal_host) or portal_host
    # Pages to mine for vendor links/credits: the login page first, then the
    # cascade's root pages (de-duped by URL).
    pages: list[tuple[str, str]] = [(portal_html, portal_url)]
    seen_pages = {portal_url}
    for hp in (extra_pages or []):
        if hp and hp[0] and hp[1] not in seen_pages:
            pages.append(hp)
            seen_pages.add(hp[1])
    # Ensure the portal host's homepage root is scanned even if the cascade
    # never fetched it (platform fast-path, or D stopped before the host root)
    # — that's where the footer "built by" credit usually lives.
    have_home = any(
        _host(b) == portal_host and urlsplit(b).path.rstrip("/") == "" for _h, b in pages
    )
    if not have_home:
        home = f"https://{portal_host}/"
        h_html, h_final = _fetch_body(
            home, js_renderer=None, prefer_js=False,
            user_agent=user_agent, http_timeout=http_timeout,
        )
        if h_html and not _is_blocked_page(h_html):
            pages.append((h_html, h_final or home))

    vendor_hosts: list[str] = []
    known_urls: list[str] = []
    for html, base in pages:
        for abs_url, _text in _outbound_links(html, base):
            h = _host(abs_url)
            if not h or (_parent_domain(h) or h) == portal_parent:
                continue
            if h.endswith(".ac.in") or h.endswith(".edu.in") or h.endswith(".edu") or h.endswith(".gov.in"):
                continue  # that's a university, not a vendor
            vp = _parent_domain(h) or h
            if vp in _GLOBAL_PLATFORM_HOSTS:
                continue  # Google/Facebook/etc. — not the portal's vendor
            # Any other external host is a vendor candidate; the relevance gate
            # below decides whether its T&C actually governs the portal service.
            if vp not in vendor_hosts:
                vendor_hosts.append(vp)
        # Vendors named but not linked (powered/developed-by / known signatures).
        # A "developed by X" credit often names the UNIVERSITY itself (e.g.
        # ouatams.in -> "OUAT" -> ouat.ac.in), so apply the same academic-host
        # filter here — that's a university, handled by the F/H levels, not G.
        named_hosts, ku = _vendor_hosts_from_name(orgid, html, base, http_timeout=http_timeout)
        for h in named_hosts:
            if h.endswith(".ac.in") or h.endswith(".edu.in") or h.endswith(".edu") or h.endswith(".gov.in"):
                continue
            if h not in vendor_hosts:
                vendor_hosts.append(h)
        for u in ku:
            if u not in known_urls:
                known_urls.append(u)
    accepted: list[str] = []
    # Known registry T&C URLs (validated like any other) come first.
    for ku in known_urls:
        final = _validate_tc_page(ku, allowed_domains=[_host(ku)], user_agent=user_agent,
                                  http_timeout=http_timeout, js_renderer=js_renderer)
        if final and final not in accepted:
            accepted.append(final)
    for vhost in vendor_hosts[:5]:  # cap vendor fan-out
        root = f"https://{vhost}"
        html, final = _fetch_body(
            root, js_renderer=js_renderer, prefer_js=False,
            user_agent=user_agent, http_timeout=http_timeout,
        )
        # The candidate may actually be a UNIVERSITY/college site (non-academic
        # TLD, so the host filters above didn't catch it) — read its homepage
        # name/title. If it names an institution, it's not a vendor: skip it and
        # let the linked/unlinked-university levels (F/H) record its T&C.
        if _looks_like_university(vhost, html):
            continue
        cands = _all_tc_links_on_page(
            html, base_url=final or root, allowed_domains=[vhost],
            user_agent=user_agent, http_timeout=http_timeout, js_renderer=js_renderer,
            min_score=3, allow_pdf_boost=False,
        )
        cands += _probe_all_tc_paths(
            root, allowed_domains=[vhost],
            user_agent=user_agent, http_timeout=http_timeout, js_renderer=js_renderer,
        )
        for c in cands:
            if c in accepted:
                continue
            if _vendor_tc_is_relevant(c, user_agent=user_agent, http_timeout=http_timeout):
                accepted.append(c)
    return accepted


def _vendor_tc_is_relevant(url: str, *, user_agent: str, http_timeout: int) -> bool:
    """The vendor T&C must mention the service/clients it provides for — else
    it's the vendor's own-site boilerplate and not binding on the portal."""
    try:
        resp = tc_finder.discovery_rules.HTTP_SESSION.get(
            url, headers={"User-Agent": user_agent}, timeout=http_timeout, allow_redirects=True,
        )
        text = (resp.text or "").lower() if 200 <= resp.status_code < 400 else ""
    except requests.RequestException:
        return False
    return sum(h in text for h in _VENDOR_RELEVANCE_HINTS) >= 2


# Legal-document category of a URL — used to collapse path-VARIANTS of the same
# document (one row per category per host), so the broadened probe list can't
# turn /disclaimer + /disclaimer.php into two rows, while genuinely different
# types (terms vs privacy vs disclaimer vs copyright) each keep their own row.
_CATEGORY_PATTERNS: tuple[tuple[str, str], ...] = (
    ("privacy", "privacy"),
    ("disclaimer", "disclaim"),
    ("copyright", "copyright"),
    ("cookie", "cookie"),
    ("refund", "refund"),
    ("terms", "term"),            # after privacy so "privacy" wins on mixed slugs
    ("website-policy", "website"),
    ("hyperlinking", "hyperlink"),
    ("legal", "legal"),
)


def _legal_category(url: str) -> str | None:
    blob = (urlsplit(url).path + " " + urlsplit(url).query).lower()
    for cat, pat in _CATEGORY_PATTERNS:
        if pat in blob:
            return cat
    return None


def _dedupe(urls: list[str]) -> list[str]:
    """De-dupe to ONE entry per (host, legal-category): path-variants of the
    same document (/disclaimer, /disclaimer.php, /disclaimer/) collapse to one
    row, but distinct legal types (terms / privacy / disclaimer / copyright …)
    each keep a row. URLs with no recognizable category fall back to a
    scheme/trailing-slash-normalized key so they're not lost."""
    seen: set = set()
    out: list[str] = []
    for u in urls:
        if not u:
            continue
        cat = _legal_category(u)
        key = (_host(u), cat) if cat else u.split("://", 1)[-1].rstrip("/").lower()
        if key not in seen:
            seen.add(key)
            out.append(u)
    return out

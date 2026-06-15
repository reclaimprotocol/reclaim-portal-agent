"""Stage C.2 — T&C Analyzer.

Given a T&C URL, fetches the document (HTML or PDF), runs a deterministic
keyword scan, and returns
`{"verdict": ..., "evidence": ..., "reasoning": ..., "confidence": ...,
  "analyzer_path": ...}`.

Modes (TC_ANALYZER_MODE):
  * "keyword" (default) — keyword-only deterministic scan; never escalates.
  * "hybrid"            — keyword first, then a Claude *legal* second pass on
                          complex/ambiguous cases only (Part 2B). The Claude
                          pass runs via the Claude Code CLI subprocess — NO
                          API key / billing path. Clear-cut keyword Yes/No
                          return immediately for free.
  * "claude"            — legacy stub (unchanged); requires ANTHROPIC_API_KEY.

Part 3: a vendor-level cache (`vendor_tc_map`) lets every college on a known
campus-software vendor inherit one verdict; new vendors are auto-learned.
"""
from __future__ import annotations

import io
import logging
import os
import re
import shutil
import subprocess
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from . import discovery_rules
from ..config import (
    TC_CLAUDE_MAX_CALLS,
    TC_CLAUDE_TIMEOUT_SECONDS,
    TC_LONG_DOC_CHARS,
)

if TYPE_CHECKING:
    from ..pipeline import PipelineContext
    from ..state import StateStore

logger = logging.getLogger(__name__)


# ---- keyword sets -------------------------------------------------------

STRONG_PROHIBITIVE_KEYWORDS: frozenset[str] = frozenset({
    "scrape", "scraping", "screen-scraping", "screen scrape",
    "crawl", "crawling", "crawler",
    "robot", "robots",
    "spider", "spiders",
    "data mine", "data mining", "datamine", "datamining",
    "harvest", "harvesting",
    "automated tools", "automated means", "automated access",
    "bypass technical", "circumvent",
    "reverse engineer", "reverse engineering",
})

MODERATE_PROHIBITIVE_KEYWORDS: frozenset[str] = frozenset({
    "extract", "extraction",
    "archive", "archiving",
    "index", "indexing",
    "interfere", "disrupt",
    "bulk download", "systematically copy", "systematic copy",
    "unauthorized access",
})

NEGATION_PHRASES_NEAR: frozenset[str] = frozenset({
    "you may", "you can", "users may", "users can", "we may", "we will",
    "on request", "upon request", "for your", "to your",
    "your own", "their own",
})

PROHIBITION_INDICATOR_PHRASES: frozenset[str] = frozenset({
    "shall not", "may not", "must not", "prohibited", "forbidden",
    "do not", "shall refrain", "are not permitted", "is prohibited",
    "without authorization", "without permission", "without consent",
})

# Phrases that — when present in the 80-char window around a keyword match
# — indicate the keyword is NOT being used to prohibit user behaviour:
#   * "shall not be liable" / "shall in no way be liable" — liability
#     disclaimers ("the university shall not be liable for unauthorized
#     access to user accounts").
#   * "act, " — keyword appears inside a statute name ("Sexual Harassment
#     ... Act, 2013" mentions "prohibition" but isn't a prohibition).
#   * "notification regarding" — circulars/announcements, not contract terms.
#   * "compromised" — security advisory phrasing ("if your account is
#     compromised, contact ...").
#   * "offence" / "offense" — defining criminal offences in policy text.
#   * "redressal" — grievance redressal process descriptions.
CONTEXT_NEGATION_PHRASES: tuple[str, ...] = (
    "shall not be liable",
    "shall in no way be liable",
    "act, ",
    "notification regarding",
    "compromised",
    "offence",
    "offense",
    "redressal",
    # Part 2a — expanded false-positive windows.
    "not be liable",
    "not be responsible",
    "shall not be responsible",
    "not responsible for",
    "without limitation",
    "in no event",
    "no warranty",
    "as is",
    "all rights reserved",          # copyright-on-content boilerplate
    "copyright",
    "intellectual property rights",
    "trademark",
    "trade mark",
)

# Part 2b — scope objects. A STRONG keyword only counts as a real
# data/service-access prohibition when one of these "data" or "service"
# objects co-occurs within the sentence-sized window. Without an object the
# keyword is likely about posting content, uploading viruses, or trademark
# misuse — which do NOT block a user from logging in and extracting their
# OWN data — so it's downgraded to ambiguous rather than counted as "No".
DATA_SERVICE_OBJECT_TOKENS: frozenset[str] = frozenset({
    "data", "content", "information", "database", "records", "record",
    "website", "web site", "site", "portal", "service", "services",
    "platform", "page", "pages", "material", "materials", "server",
    "system", "systems", "account", "api", "network",
})

# STRONG keywords exempt from the scope check — these are inherently about
# automated data/service access, so they count even without an explicit
# object nearby.
_SCOPE_EXEMPT_STRONG: frozenset[str] = frozenset({
    "scrape", "scraping", "screen-scraping", "screen scrape",
    "crawl", "crawling", "crawler", "spider", "spiders",
    "data mine", "data mining", "datamine", "datamining",
    "harvest", "harvesting",
})

_SCOPE_WINDOW_HALF: int = 90   # ~180-char sentence window for the scope check


_MAX_TEXT_LEN: int = 50_000
_MIN_TEXT_LEN: int = 100
_QUOTE_WINDOW_CHARS: int = 200
_MODERATE_WINDOW_HALF: int = 50
_CONTEXT_WINDOW_HALF: int = 40   # 80-char total window for context-negation check
_EVIDENCE_MAX_LEN: int = 500


def _compile_keyword_pattern(kw: str) -> re.Pattern[str]:
    """Word-boundary pattern for a keyword. Multi-word keywords keep their
    internal word order but allow `\\s+` between tokens so weird whitespace
    in the source HTML doesn't make us miss a match.

    Uses `(?<![\\w-])` / `(?![\\w-])` rather than plain `\\b` so that
    hyphens count as part of the surrounding token. That blocks "harvest"
    matching inside "post-harvest" and "scrape" inside "screen-scrape-bot",
    while still allowing the literal hyphenated keywords (e.g. the keyword
    "screen-scraping" itself) since the lookarounds only inspect what
    appears OUTSIDE the keyword in the input.
    """
    parts = [re.escape(p) for p in kw.split()]
    inner = parts[0] if len(parts) == 1 else r"\s+".join(parts)
    return re.compile(rf"(?<![\w-]){inner}(?![\w-])", re.IGNORECASE)


_STRONG_PROHIBITIVE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (kw, _compile_keyword_pattern(kw))
    for kw in sorted(STRONG_PROHIBITIVE_KEYWORDS, key=len, reverse=True)
)
_MODERATE_PROHIBITIVE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (kw, _compile_keyword_pattern(kw))
    for kw in sorted(MODERATE_PROHIBITIVE_KEYWORDS, key=len, reverse=True)
)


@dataclass(frozen=True)
class _Match:
    term: str
    quote: str
    context: str   # "prohibitive" | "permissive" | "ambiguous"
    strength: str  # "strong" | "moderate"


# ============================================================ public API

# Severity ordering for tiebreaking the per-OrgID aggregate. "No" wins over
# "Maybe" wins over "Yes" — when counts tie, prefer the more restrictive verdict.
_VERDICT_SEVERITY: dict[str, int] = {"No": 3, "Maybe": 2, "Yes": 1}


def aggregate_verdicts(verdicts: list[str]) -> str:
    """Aggregate per-portal T&C verdicts into a single OrgID-level verdict.

    Rules:
      * Empty input → "Yes (No T&C Found)" (no portals analyzed).
      * Every input is "Yes (No T&C Found)" → preserve that suffix so
        downstream consumers can tell "no T&C anywhere" apart from
        "T&C found and permissive".
      * Otherwise count "Yes (No T&C Found)" as plain "Yes" and return the
        most common verdict; on a tie prefer worst (No > Maybe > Yes).
    """
    if not verdicts:
        return "Yes (No T&C Found)"
    if all(v == "Yes (No T&C Found)" for v in verdicts):
        return "Yes (No T&C Found)"
    counts = Counter("Yes" if v == "Yes (No T&C Found)" else v for v in verdicts)
    max_count = max(counts.values())
    leaders = [v for v, c in counts.items() if c == max_count]
    leaders.sort(key=lambda v: -_VERDICT_SEVERITY.get(v, 0))
    return leaders[0]


# Path tokens that mark a URL as the binding Terms-of-Use document (as opposed
# to a privacy policy / disclaimer, which govern data handling, not whether
# automated access is permitted).
_TERMS_URL_TOKENS: tuple[str, ...] = (
    "terms-and-conditions", "terms_condition", "termsandcondition",
    "terms-of-use", "terms-of-service", "termsofuse", "termsofservice",
    "/terms", "/tos", "/tou", "/legal", "conditions-of-use",
)


def is_terms_url(url: str) -> bool:
    """True when the URL path looks like a Terms-of-Use / T&C document rather
    than a privacy policy or disclaimer."""
    low = (url or "").lower()
    return any(tok in low for tok in _TERMS_URL_TOKENS)


def aggregate_verdicts_by_url(pairs: list[tuple[str, str]]) -> str:
    """URL-aware aggregation: the binding **Terms-of-Use** page wins over
    permissive privacy/disclaimer pages.

    Evidence (15June finder audit): universities like Lnct, Jain Online and
    Avinash list both a permissive privacy-policy (→ Yes) AND a prohibitive
    terms-and-conditions page (→ No); plain majority-vote `aggregate_verdicts`
    diluted the binding "No" into a "Yes". When any Terms-of-Use URL is
    present, decide from the Terms pages alone (strictest of them); only fall
    back to all-URL aggregation when no Terms page exists.

    `pairs` is a list of (url, verdict). Returns the row-level verdict.
    """
    if not pairs:
        return "Yes (No T&C Found)"
    terms = [v for (u, v) in pairs if is_terms_url(u) and v != "Yes (No T&C Found)"]
    if terms:
        # Strictest Terms verdict wins (No > Maybe > Yes).
        return max(terms, key=lambda v: _VERDICT_SEVERITY.get(v, 0))
    return aggregate_verdicts([v for (_u, v) in pairs])


def run(ctx: "PipelineContext") -> dict[str, Any]:
    """Pipeline entrypoint — analyses every T&C URL discovered by Stage C.1."""
    finder_result = ctx.results.get("tc_finder") or {}
    findings: list[dict[str, Any]] = finder_result.get("tc_findings") or []
    orgid = ctx.orgid
    state: "StateStore | None" = ctx.deps.get("state")
    user_agent = ctx.deps.get("user_agent") or _default_user_agent()
    http_timeout = int(ctx.deps.get("http_timeout") or 20)

    mode = (
        ctx.deps.get("tc_analyzer_mode")
        or os.environ.get("TC_ANALYZER_MODE", "keyword")
    ).lower()

    analyses: list[dict[str, Any]] = []
    for f in findings:
        portal_url = f.get("portal_url", "")
        tc_url = f.get("tc_url")
        vendor_name = f.get("vendor")  # Part 3 — set by tc_finder Tier 4
        if not tc_url and not vendor_name:
            analyses.append({
                "portal_url": portal_url, "tc_url": None,
                "verdict": "Yes (No T&C Found)",
                "evidence": "No T&C document found for this portal or its university",
                "reasoning": "Defaulting to permissive — no document to analyze",
                "confidence": 0.5, "clause": "", "had_conflict": False,
                "analyzer_path": "keyword",
            })
            continue
        result = analyze_tc_url(
            tc_url=tc_url or "", state=state, user_agent=user_agent,
            http_timeout=http_timeout, orgid=orgid,
            mode=mode, vendor_name=vendor_name,
            js_renderer=ctx.deps.get("js_renderer"),
        )
        analyses.append({
            "portal_url": portal_url, "tc_url": tc_url,
            "source": f.get("source"), "scope": f.get("scope"),
            "vendor": vendor_name, **result,
        })
    return {"tc_analyses": analyses}


def analyze_tc_url(
    *,
    tc_url: str,
    state: "StateStore | None" = None,
    user_agent: str | None = None,
    http_timeout: int = 20,
    orgid: str = "",
    force_refresh: bool = False,
    mode: str | None = None,
    vendor_name: str | None = None,
    js_renderer: Any = None,
) -> dict[str, Any]:
    """Fetch + analyse the T&C document. Returns dict with keys `verdict`,
    `evidence`, `reasoning`, `confidence`, `clause`, `had_conflict`,
    `analyzer_path`. Cached by normalised URL in state.db.

    `mode`:
      * "keyword" (default) — deterministic scan only.
      * "hybrid"            — keyword first, Claude legal pass on complex
                              cases (Part 2B), via the Claude Code CLI.
      * (other)             — treated as keyword (no escalation).

    `vendor_name` (Part 3): when set, a fresh `vendor_tc_map` verdict
    short-circuits the whole fetch+analysis; otherwise the freshly computed
    verdict is written back so every later college on that vendor inherits
    it (auto-learn).

    `force_refresh=True` skips cache *reads* (vendor + per-URL) so scoring
    changes take effect, but still writes results back.
    """
    mode = (mode or os.environ.get("TC_ANALYZER_MODE", "keyword")).lower()

    # ---- Part 3: vendor-level cache short-circuit (skip fetch + analysis) ----
    if vendor_name and state is not None and not force_refresh:
        v = state.get_vendor_tc(vendor_name)
        if v is not None:
            logger.info(
                "[%s] tc-analyzer vendor-cache hit for %r → %s",
                orgid, vendor_name, v["verdict"],
            )
            return {
                "verdict": v["verdict"],
                "evidence": f"vendor {vendor_name}: {v.get('reasoning') or 'cached verdict'}",
                "reasoning": v.get("reasoning") or "Inherited from vendor-level T&C cache",
                "confidence": 0.9, "clause": "", "had_conflict": False,
                "analyzer_path": "vendor-cache", "vendor": vendor_name,
            }

    if not tc_url:
        # vendor_name was set but unknown to the cache and there's no URL to
        # fetch → permissive default, then auto-learn below.
        result = {
            "verdict": "Yes (No T&C Found)",
            "evidence": "No T&C document URL for this vendor/portal",
            "reasoning": "Defaulting to permissive — no document to analyze",
            "confidence": 0.5, "clause": "", "had_conflict": False,
            "analyzer_path": "keyword",
        }
        if state is not None and vendor_name:
            state.set_vendor_tc(vendor_name, "", result["verdict"], result["reasoning"])
            logger.info("[%s] auto-learned vendor %r → %s", orgid, vendor_name, result["verdict"])
            result = {**result, "vendor": vendor_name}
        return result

    cache_key = normalize_tc_url(tc_url)
    if state is not None and not force_refresh:
        cached = state.get_tc_cache(cache_key)
        if cached is not None:
            logger.info("[%s] tc-analyzer cache hit for %s", orgid, tc_url)
            return cached

    text = _fetch_tc_text(
        tc_url,
        user_agent=user_agent or _default_user_agent(),
        http_timeout=http_timeout,
        js_renderer=js_renderer,
    )
    garbled = (not text) or (len(text.strip()) < _MIN_TEXT_LEN)
    if garbled:
        result = {
            "verdict": "Yes (No T&C Found)",
            "evidence": "No restrictive keywords found",
            "reasoning": "T&C URL fetched but content empty or unreadable",
            "confidence": 0.5, "clause": "", "had_conflict": False,
            "analyzer_path": "keyword",
        }
    else:
        if len(text) > _MAX_TEXT_LEN:
            text = text[:_MAX_TEXT_LEN]
        result = _score_tc_text(text)

    # ---- Part 2B: hybrid Claude legal escalation (complex cases only) ----
    if mode == "hybrid":
        reason = _escalation_reason(result, text, garbled=garbled)
        if reason is not None:
            escalated = _escalate_to_claude(text, result, orgid=orgid, reason=reason)
            if escalated is not None:
                result = escalated
        else:
            logger.debug(
                "[%s] tc-analyzer clear-cut keyword %s (conf=%.2f) — no escalation",
                orgid, result.get("verdict"), float(result.get("confidence", 0.0)),
            )

    if state is not None:
        state.set_tc_cache(cache_key, result)
        # Part 3 — auto-learn this vendor so all future colleges resolve free.
        if vendor_name:
            state.set_vendor_tc(
                vendor_name, tc_url, result.get("verdict"), result.get("reasoning"),
            )
            logger.info(
                "[%s] auto-learned vendor %r → %s (via %s)",
                orgid, vendor_name, result.get("verdict"), result.get("analyzer_path"),
            )
            result = {**result, "vendor": vendor_name}
    return result


# ============================================================ fetch + extract

def _fetch_tc_text(
    url: str,
    *,
    user_agent: str,
    http_timeout: int,
    js_renderer: Any = None,
) -> str:
    static_text = ""
    is_pdf = False
    try:
        resp = discovery_rules.HTTP_SESSION.get(
            url,
            headers={"User-Agent": user_agent},
            timeout=http_timeout,
            allow_redirects=True,
        )
    except requests.RequestException as err:
        logger.warning("tc-analyzer fetch failed %s: %s", url, err)
        resp = None
    if resp is not None and 200 <= resp.status_code < 400:
        content_type = (resp.headers.get("content-type") or "").lower()
        is_pdf = "pdf" in content_type or url.lower().split("?")[0].endswith(".pdf")
        if is_pdf:
            static_text = _extract_pdf_text(resp.content)
        else:
            static_text = _extract_html_text(resp.text or "")

    # JS-render fallback. A non-PDF whose static body came back empty/short is
    # either a JS-rendered SPA (200 OK, content injected by JavaScript) or a
    # transient connection failure. Render it with a headless browser and
    # re-extract — this closes the SPA blind spot where an empty static body
    # otherwise scores a false "Yes (No T&C Found)". PDFs are never rendered.
    if (
        not is_pdf
        and js_renderer is not None
        and len(static_text.strip()) < _MIN_TEXT_LEN
    ):
        try:
            rendered = js_renderer.render(url)
        except Exception as err:  # renderer must never abort the analysis
            logger.warning("tc-analyzer JS render raised for %s: %s", url, err)
            rendered = None
        if rendered is not None and rendered.ok and rendered.html:
            rendered_text = _extract_html_text(rendered.html)
            if len(rendered_text.strip()) > len(static_text.strip()):
                logger.info(
                    "tc-analyzer JS-render recovered %d chars for %s",
                    len(rendered_text.strip()), url,
                )
                return rendered_text
    return static_text


def _extract_pdf_text(content: bytes) -> str:
    try:
        import pdfplumber
    except ImportError:
        logger.warning("pdfplumber not installed — cannot extract PDF text")
        return ""
    try:
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            return "\n".join((page.extract_text() or "") for page in pdf.pages)
    except Exception as err:
        logger.warning("PDF extraction failed: %s", err)
        return ""


_WHITESPACE_RE = re.compile(r"\s+")

# Element types that virtually always hold chrome rather than body content.
# Decomposed before any text extraction — this kills the inline `<script>`
# bug (the old regex tag-stripper left GTM JS as plain text and let
# "harvest" inside "post-harvest processing" trigger a STRONG match) and
# trims away nav/header/footer/aside menus that contain stray keywords like
# "archive" / "notifications" sitting in nav breadcrumbs.
_CHROME_TAGS: tuple[str, ...] = (
    "script", "style", "noscript",
    "nav", "header", "footer", "aside",
)

# `<form>` is NOT in _CHROME_TAGS: ASP.NET WebForms (and many .aspx-style
# Indian-uni/govt sites) wrap the ENTIRE page body in a single
# `<form runat="server">`, so decomposing every form throws away all
# content and scores a false "No T&C Found". We instead drop only *small*
# forms — login boxes, search bars, newsletter signups — whose own text is
# under this threshold; large content-bearing forms are kept.
_FORM_CHROME_MAX_TEXT: int = 400

# Container candidates to prefer when present. Order encodes specificity:
# semantic HTML5 tags first, then ARIA, then common CMS class/id hints.
_MAIN_CONTENT_HINTS: tuple[str, ...] = (
    "main-wrapper", "main-content", "site-content", "page-content",
    "post-content", "entry-content", "content-area", "main_content",
    "page__content", "container-main",
)


def _extract_html_text(html: str) -> str:
    """Return clean plain text from an HTML document, biased toward the
    main content area. Strategy:

      1. Decompose `_CHROME_TAGS` (script/style/nav/header/footer/aside) plus
         small login/search forms so their text never enters the keyword scan
         (large content-bearing forms are kept — see `_FORM_CHROME_MAX_TEXT`).
      2. If a `<main>` / `<article>` / `[role=main]` exists, use it.
      3. Else find the largest `<div>` / `<section>` whose id or class
         matches a hint in `_MAIN_CONTENT_HINTS` (main-wrapper, etc.).
      4. Fall back to `<body>` (or the whole document if no <body>).

    Returns whitespace-collapsed text. Empty string on empty input.
    """
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    for tag_name in _CHROME_TAGS:
        for el in soup.find_all(tag_name):
            el.decompose()
    # Drop only small login/search/signup forms; keep large content-bearing
    # forms (ASP.NET wraps the whole page body in one <form>). See
    # `_FORM_CHROME_MAX_TEXT`.
    for form in soup.find_all("form"):
        if len(form.get_text(" ", strip=True)) < _FORM_CHROME_MAX_TEXT:
            form.decompose()

    for selector in ("main", "article"):
        el = soup.find(selector)
        if el:
            text = el.get_text(" ", strip=True)
            if len(text) >= _MIN_TEXT_LEN:
                return _WHITESPACE_RE.sub(" ", text)
    role_main = soup.find(attrs={"role": "main"})
    if role_main:
        text = role_main.get_text(" ", strip=True)
        if len(text) >= _MIN_TEXT_LEN:
            return _WHITESPACE_RE.sub(" ", text)

    # Largest hinted container wins — outer wrappers generally contain the
    # full body, inner ones may miss sections. Already de-chromed so the
    # outer wrapper is mostly content.
    candidates: list[tuple[int, Any]] = []
    for el in soup.find_all(("div", "section")):
        identifier = (el.get("id") or "") + " " + " ".join(el.get("class") or [])
        identifier = identifier.lower()
        if any(hint in identifier for hint in _MAIN_CONTENT_HINTS):
            text = el.get_text(" ", strip=True)
            if 200 < len(text) < 100_000:
                candidates.append((len(text), el))
    if candidates:
        candidates.sort(key=lambda x: -x[0])
        text = candidates[0][1].get_text(" ", strip=True)
        return _WHITESPACE_RE.sub(" ", text)

    body = soup.body or soup
    return _WHITESPACE_RE.sub(" ", body.get_text(" ", strip=True))


# ============================================================ scoring core

def _score_tc_text(text: str) -> dict[str, Any]:
    text_lower = text.lower()
    matched: list[_Match] = []
    # Dedup positions so "indexing" doesn't *also* match a hypothetical "index"
    # keyword (we iterate length-descending, so shorter keywords contained
    # inside already-matched longer ones are skipped). Note that with `\b`
    # word-boundary matching, "indexing" as a substring no longer triggers
    # "index" anyway — this mostly catches multi-word phrases that share
    # tokens with shorter ones.
    covered: list[tuple[int, int]] = []

    def _already_covered(start: int, end: int) -> bool:
        for s, e in covered:
            if start >= s and end <= e:
                return True
        return False

    def _classify_window(window: str) -> tuple[bool, str]:
        """Return (context_negated, classification). When context-negated the
        match is dropped entirely — the keyword is being used in a
        non-prohibitive sense (statute name, liability disclaimer, etc.)."""
        if any(neg in window for neg in CONTEXT_NEGATION_PHRASES):
            return True, "context-negated"
        if any(p in window for p in NEGATION_PHRASES_NEAR):
            return False, "permissive"
        if any(p in window for p in PROHIBITION_INDICATOR_PHRASES):
            return False, "prohibitive"
        return False, "ambiguous"

    # Part 2B signal — a STRONG keyword that matched but was context-negated
    # OR sat in a permissive window. A genuine conflict ("you may not scrape"
    # next to "shall not be liable") is exactly the ambiguous case worth
    # escalating to the Claude legal pass.
    strong_conflict = False

    for kw, pat in _STRONG_PROHIBITIVE_PATTERNS:
        for m in pat.finditer(text):
            idx, end = m.start(), m.end()
            if _already_covered(idx, end):
                continue
            win_start = max(0, idx - _CONTEXT_WINDOW_HALF)
            win_end = min(len(text_lower), end + _CONTEXT_WINDOW_HALF)
            window = text_lower[win_start:win_end]
            negated, classification = _classify_window(window)
            if negated:
                strong_conflict = True
                logger.debug(
                    "[tc-analyzer] DROP STRONG %r @%d via context-negation; window=%r",
                    kw, idx, window,
                )
                continue
            if classification == "permissive":
                strong_conflict = True
            # Part 2b — scope check. Outside the inherently-automated keyword
            # set, require a data/service object within the sentence window;
            # otherwise the keyword is about content/conduct/trademark, not
            # data access — downgrade to ambiguous instead of counting "No".
            if kw not in _SCOPE_EXEMPT_STRONG:
                s_start = max(0, idx - _SCOPE_WINDOW_HALF)
                s_end = min(len(text_lower), end + _SCOPE_WINDOW_HALF)
                scope_window = text_lower[s_start:s_end]
                if not any(obj in scope_window for obj in DATA_SERVICE_OBJECT_TOKENS):
                    covered.append((idx, end))
                    quote = _extract_quote(text, idx, end, _QUOTE_WINDOW_CHARS)
                    matched.append(_Match(term=kw, quote=quote, context="ambiguous", strength="strong"))
                    logger.debug(
                        "[tc-analyzer] DOWNGRADE STRONG %r @%d — no data/service object in scope; window=%r",
                        kw, idx, scope_window,
                    )
                    break
            covered.append((idx, end))
            quote = _extract_quote(text, idx, end, _QUOTE_WINDOW_CHARS)
            matched.append(_Match(term=kw, quote=quote, context="prohibitive", strength="strong"))
            logger.debug(
                "[tc-analyzer] KEEP STRONG %r @%d window=%r", kw, idx, window,
            )
            break  # one match per keyword is enough

    for kw, pat in _MODERATE_PROHIBITIVE_PATTERNS:
        for m in pat.finditer(text):
            idx, end = m.start(), m.end()
            if _already_covered(idx, end):
                continue
            win_start = max(0, idx - _MODERATE_WINDOW_HALF)
            win_end = min(len(text_lower), end + _MODERATE_WINDOW_HALF)
            window = text_lower[win_start:win_end]
            negated, classification = _classify_window(window)
            if negated:
                logger.debug(
                    "[tc-analyzer] DROP MODERATE %r @%d via context-negation; window=%r",
                    kw, idx, window,
                )
                continue
            covered.append((idx, end))
            quote = _extract_quote(text, idx, end, _QUOTE_WINDOW_CHARS)
            matched.append(_Match(term=kw, quote=quote, context=classification, strength="moderate"))
            logger.debug(
                "[tc-analyzer] KEEP MODERATE %r @%d ctx=%s window=%r",
                kw, idx, classification, window,
            )
            break

    strong_count = sum(
        1 for m in matched if m.strength == "strong" and m.context == "prohibitive"
    )
    weak_count = sum(
        1 for m in matched if m.strength == "moderate" and m.context == "prohibitive"
    )
    has_ambiguous = any(m.context == "ambiguous" for m in matched)

    prohibitive_matches = [m for m in matched if m.context == "prohibitive"]
    if strong_count >= 1:
        verdict = "No"
        confidence = 0.95 if strong_count >= 2 else 0.8
        top_terms = [m.term for m in matched if m.strength == "strong" and m.context == "prohibitive"][:3]
        reasoning = "T&C contains explicit prohibitive language: " + ", ".join(top_terms)
    elif weak_count >= 2:
        verdict = "No"
        confidence = 0.7
        top_terms = [m.term for m in matched if m.strength == "moderate" and m.context == "prohibitive"][:3]
        reasoning = "T&C contains multiple terms in restrictive context: " + ", ".join(top_terms)
    elif weak_count == 1:
        verdict = "Maybe"
        confidence = 0.4
        reasoning = "Single restrictive term found — manual review recommended"
    elif has_ambiguous:
        verdict = "Maybe"
        confidence = 0.35
        reasoning = "T&C contains relevant terms but context unclear"
    else:
        verdict = "Yes"
        confidence = 0.9
        reasoning = "No prohibitive language found in T&C"

    clause = _short_clause(prohibitive_matches[0].quote) if prohibitive_matches else ""
    return {
        "verdict": verdict,
        "evidence": _build_evidence(matched),
        "reasoning": reasoning,
        "confidence": round(confidence, 2),
        "clause": clause,
        "had_conflict": strong_conflict,
        "analyzer_path": "keyword",
    }


def _extract_quote(text: str, start: int, end: int, max_chars: int) -> str:
    half = max_chars // 2
    win_start = max(0, start - half)
    win_end = min(len(text), end + half)
    chunk = text[win_start:win_end].strip()
    chunk = _WHITESPACE_RE.sub(" ", chunk)
    return chunk[:max_chars]


def _build_evidence(matched: list[_Match]) -> str:
    if not matched:
        return "No restrictive keywords found"
    parts: list[str] = []
    for m in matched[:3]:
        snippet = m.quote[:80].replace("'", "ʼ")
        parts.append(f"{m.term} ({m.context}, '{snippet}')")
    extra = len(matched) - 3
    out = f"{len(matched)} matches: " + "; ".join(parts)
    if extra > 0:
        out += f" ...and {extra} more"
    if len(out) > _EVIDENCE_MAX_LEN:
        out = out[:_EVIDENCE_MAX_LEN - 3].rstrip() + "..."
    return out


def _short_clause(quote: str, max_words: int = 14) -> str:
    """Trim a quote to a single short clause (≤ max_words words) for the
    `clause` evidence field — Part 2c."""
    if not quote:
        return ""
    words = _WHITESPACE_RE.sub(" ", quote).strip().split(" ")
    if len(words) <= max_words:
        return " ".join(words)
    return " ".join(words[:max_words]) + "…"


# ==================================================== Part 2B — Claude legal

# Process-level cap counter (= per batch run, since one batch == one process).
_CLAUDE_CALLS_MADE: int = 0

_CLAUDE_LEGAL_PROMPT: str = (
    "You are a legal analyst. A service needs to let users log into this "
    "portal with their OWN credentials and extract their OWN "
    "academic/enrollment data, on the user's behalf, for verification. "
    "Based ONLY on this T&C, does it PROHIBIT that?\n"
    "- Treat scraping, crawling, robots/spiders, automated access, "
    "systematic retrieval, data mining, harvesting, and bans on "
    "commercial/third-party use of data obtained via the site as "
    "PROHIBITIONS.\n"
    "- Do NOT count liability disclaimers, copyright on website content, "
    "trademark rules, or content-posting/conduct rules as prohibitions.\n"
    "Answer strictly:\n"
    "  VERDICT: YES_PROHIBITED | NO_ALLOWED | MAYBE_AMBIGUOUS\n"
    "  CLAUSE: <single most relevant quote, under 15 words>\n"
    "  REASON: <one sentence>"
)

_CLAUDE_VERDICT_MAP: dict[str, str] = {
    "YES_PROHIBITED": "No",
    "NO_ALLOWED": "Yes",
    "MAYBE_AMBIGUOUS": "Maybe",
}


def _escalation_reason(result: dict[str, Any], text: str, *, garbled: bool) -> str | None:
    """Why a case is complex enough for the Claude legal pass — or None when
    it's clear-cut and should return immediately for free."""
    if result.get("verdict") == "Maybe":
        return "keyword verdict Maybe"
    if result.get("had_conflict"):
        return "STRONG keyword fired in a negation/false-positive window (conflict)"
    if garbled and (text or "").strip():
        return "document scanned empty/garbled"
    if len(text) > TC_LONG_DOC_CHARS and float(result.get("confidence", 1.0)) < 0.8:
        return f"long/dense document ({len(text)} chars) with low keyword confidence"
    return None


def _claude_cli_path() -> str | None:
    return shutil.which("claude")


def _parse_claude_legal(stdout: str) -> tuple[str, str, str] | None:
    """Parse the strict VERDICT/CLAUSE/REASON block. Returns
    (verdict, clause, reason) or None when no VERDICT line maps."""
    verdict: str | None = None
    clause = ""
    reason = ""
    for raw in (stdout or "").splitlines():
        line = raw.strip()
        upper = line.upper()
        if upper.startswith("VERDICT:"):
            token = line.split(":", 1)[1].strip().upper()
            for key, mapped in _CLAUDE_VERDICT_MAP.items():
                if key in token:
                    verdict = mapped
                    break
        elif upper.startswith("CLAUSE:"):
            clause = line.split(":", 1)[1].strip().strip('"').strip()
        elif upper.startswith("REASON:"):
            reason = line.split(":", 1)[1].strip()
    if verdict is None:
        return None
    return verdict, clause, reason


def _escalate_to_claude(
    text: str, keyword_result: dict[str, Any], *, orgid: str, reason: str,
) -> dict[str, Any] | None:
    """Run the Claude *legal* second pass via the Claude Code CLI subprocess.
    Returns a fresh result dict on success, a cap-flagged keyword result when
    the per-run cap is hit, or None to fall back to the keyword verdict
    (CLI missing / call failed / output unparseable). No API key path."""
    global _CLAUDE_CALLS_MADE

    claude = _claude_cli_path()
    if not claude:
        logger.info("[%s] Claude legal unavailable — using keyword verdict", orgid)
        return None

    if _CLAUDE_CALLS_MADE >= TC_CLAUDE_MAX_CALLS:
        logger.warning(
            "[%s] TC_CLAUDE_MAX_CALLS=%d cap reached — keeping keyword verdict (flagged)",
            orgid, TC_CLAUDE_MAX_CALLS,
        )
        flagged = dict(keyword_result)
        if flagged.get("verdict") == "Maybe":
            flagged["reasoning"] = (
                (flagged.get("reasoning") or "") + " (keyword — Claude cap reached)"
            ).strip()
        flagged["analyzer_path"] = "keyword-cap-reached"
        return flagged

    prompt = f"{_CLAUDE_LEGAL_PROMPT}\n\nT&C TEXT:\n{text[:_MAX_TEXT_LEN]}"
    _CLAUDE_CALLS_MADE += 1
    logger.info(
        "[%s] escalating to Claude legal (%s) — call %d/%d",
        orgid, reason, _CLAUDE_CALLS_MADE, TC_CLAUDE_MAX_CALLS,
    )
    try:
        proc = subprocess.run(
            [claude, "--print"],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=TC_CLAUDE_TIMEOUT_SECONDS,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as err:
        logger.warning(
            "[%s] Claude legal call failed (%s) — using keyword verdict",
            orgid, err,
        )
        return None
    if proc.returncode != 0:
        logger.warning(
            "[%s] Claude legal returned %d — using keyword verdict",
            orgid, proc.returncode,
        )
        return None

    parsed = _parse_claude_legal(proc.stdout or "")
    if parsed is None:
        logger.warning(
            "[%s] Claude legal output unparseable — using keyword verdict", orgid,
        )
        return None
    verdict, clause, claude_reason = parsed
    logger.info(
        "[%s] Claude legal verdict=%s clause=%r", orgid, verdict, clause[:60],
    )
    return {
        "verdict": verdict,
        "evidence": (f"Claude legal clause: {clause}" if clause
                     else keyword_result.get("evidence", "")),
        "reasoning": claude_reason or "Claude legal analysis",
        "confidence": 0.85,
        "clause": clause,
        "had_conflict": keyword_result.get("had_conflict", False),
        "analyzer_path": "claude-legal",
    }


# ============================================================ helpers

def normalize_tc_url(url: str) -> str:
    cleaned = discovery_rules.strip_session_ids(str(url))
    p = urlsplit(cleaned)
    scheme = (p.scheme or "https").lower()
    host = p.netloc.lower().split(":")[0]
    path = p.path
    return urlunsplit((scheme, host, path, "", ""))


def _default_user_agent() -> str:
    return "reclaim-portal-agent/0.1"

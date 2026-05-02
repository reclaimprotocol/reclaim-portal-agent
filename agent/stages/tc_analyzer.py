"""Stage C.2 — T&C Analyzer (free, keyword-only).

Given a T&C URL, fetches the document (HTML or PDF), runs a deterministic
keyword scan, and returns `{"verdict": ..., "evidence": ..., "reasoning": ...}`.

# Free keyword-only version. Claude-powered analyzer (TC_ANALYZER_MODE='claude')
# can be added when API credits become available.
"""
from __future__ import annotations

import io
import logging
import re
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from . import discovery_rules

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
)


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


def run(ctx: "PipelineContext") -> dict[str, Any]:
    """Pipeline entrypoint — analyses every T&C URL discovered by Stage C.1."""
    finder_result = ctx.results.get("tc_finder") or {}
    findings: list[dict[str, Any]] = finder_result.get("tc_findings") or []
    orgid = ctx.orgid
    state: "StateStore | None" = ctx.deps.get("state")
    user_agent = ctx.deps.get("user_agent") or _default_user_agent()
    http_timeout = int(ctx.deps.get("http_timeout") or 20)

    analyses: list[dict[str, Any]] = []
    for f in findings:
        portal_url = f.get("portal_url", "")
        tc_url = f.get("tc_url")
        if not tc_url:
            analyses.append({
                "portal_url": portal_url, "tc_url": None,
                "verdict": "Yes (No T&C Found)",
                "evidence": "No T&C document found for this portal or its university",
                "reasoning": "Defaulting to permissive — no document to analyze",
            })
            continue
        result = analyze_tc_url(
            tc_url=tc_url, state=state, user_agent=user_agent,
            http_timeout=http_timeout, orgid=orgid,
        )
        analyses.append({"portal_url": portal_url, "tc_url": tc_url, **result})
    return {"tc_analyses": analyses}


def analyze_tc_url(
    *,
    tc_url: str,
    state: "StateStore | None" = None,
    user_agent: str | None = None,
    http_timeout: int = 20,
    orgid: str = "",
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Fetch + keyword-analyse the T&C document. Returns dict with keys
    `verdict`, `evidence`, `reasoning`. Cached by normalised URL in state.db.

    `force_refresh=True` skips the cache *read* (so changes to the keyword
    set or scoring logic take effect immediately) but still writes the
    fresh result back, so the next non-force run benefits.
    """
    cache_key = normalize_tc_url(tc_url)
    if state is not None and not force_refresh:
        cached = state.get_tc_cache(cache_key)
        if cached is not None:
            logger.info("[%s] tc-analyzer cache hit for %s", orgid, tc_url)
            return cached

    text = _fetch_tc_text(tc_url, user_agent=user_agent or _default_user_agent(), http_timeout=http_timeout)
    if not text or len(text.strip()) < _MIN_TEXT_LEN:
        result = {
            "verdict": "Yes (No T&C Found)",
            "evidence": "No restrictive keywords found",
            "reasoning": "T&C URL fetched but content empty or unreadable",
        }
    else:
        if len(text) > _MAX_TEXT_LEN:
            text = text[:_MAX_TEXT_LEN]
        result = _score_tc_text(text)

    if state is not None:
        state.set_tc_cache(cache_key, result)
    return result


# ============================================================ fetch + extract

def _fetch_tc_text(url: str, *, user_agent: str, http_timeout: int) -> str:
    try:
        resp = discovery_rules.HTTP_SESSION.get(
            url,
            headers={"User-Agent": user_agent},
            timeout=http_timeout,
            allow_redirects=True,
        )
    except requests.RequestException as err:
        logger.warning("tc-analyzer fetch failed %s: %s", url, err)
        return ""
    if not (200 <= resp.status_code < 400):
        return ""

    content_type = (resp.headers.get("content-type") or "").lower()
    is_pdf = "pdf" in content_type or url.lower().split("?")[0].endswith(".pdf")

    if is_pdf:
        return _extract_pdf_text(resp.content)
    return _extract_html_text(resp.text or "")


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
    "nav", "header", "footer", "aside", "form",
)

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

      1. Decompose `_CHROME_TAGS` (script/style/nav/header/footer/aside/form)
         so their text never enters the keyword scan.
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
                logger.debug(
                    "[tc-analyzer] DROP STRONG %r @%d via context-negation; window=%r",
                    kw, idx, window,
                )
                continue
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

    if strong_count >= 1:
        verdict = "No"
        top_terms = [m.term for m in matched if m.strength == "strong" and m.context == "prohibitive"][:3]
        reasoning = "T&C contains explicit prohibitive language: " + ", ".join(top_terms)
    elif weak_count >= 2:
        verdict = "No"
        top_terms = [m.term for m in matched if m.strength == "moderate" and m.context == "prohibitive"][:3]
        reasoning = "T&C contains multiple terms in restrictive context: " + ", ".join(top_terms)
    elif weak_count == 1:
        verdict = "Maybe"
        reasoning = "Single restrictive term found — manual review recommended"
    elif has_ambiguous:
        verdict = "Maybe"
        reasoning = "T&C contains relevant terms but context unclear"
    else:
        verdict = "Yes"
        reasoning = "No prohibitive language found in T&C"

    return {"verdict": verdict, "evidence": _build_evidence(matched), "reasoning": reasoning}


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

"""Genie-V3 · Layer 2 — external web-search fallback (OpenAI).

The normal path is still the university's own rendered website. This module is
only a recovery path for a crawl that yields no useful links, or a targeted
capability expansion. OpenAI web search supplies *candidate* URLs from the
indexed web; it is deliberately not trusted as a portal verdict. Every URL it
returns goes through the same filter, model classification, live-endpoint and
student-login-surface checks as a URL found on the university homepage.

This uses OpenAI's hosted ``web_search`` tool through the Responses API. A
missing/invalid key, rate limit, timeout, or malformed response always degrades
to ``[]`` so a search outage cannot fail a long batch run.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import weakref
from collections.abc import Mapping, Sequence
from typing import Any

from openai import APIError, APITimeoutError, AsyncOpenAI, RateLimitError

from agent.languages import profile_for_country

logger = logging.getLogger("genie.search")

MAX_RESULTS = 6
_HTTP_URL_RE = re.compile(r"https?://[^\s<>\]\[\"')]+", re.I)
_DOCUMENT_URL_RE = re.compile(
    r"\.(?:pdf|docx?|xlsx?|pptx?|od[stp]|rtf)(?:$|[?#])", re.I)
# Search is the recovery path for browser-rendering failures.  A short timeout
# turns a temporary slow search into a false "no portal" result, so give the
# hosted tool enough time to obtain and cite live sources.
DEFAULT_TIMEOUT_S = int(os.getenv("GENIE_OPENAI_SEARCH_TIMEOUT_SECONDS", "90"))
DEFAULT_MODEL = "gpt-5.5"

_PLATFORM_TERMS = ("student portal", "login", "samarth", "moodle", "erp")

_search_sems: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, asyncio.Semaphore
] = weakref.WeakKeyDictionary()


def _get_search_sem() -> asyncio.Semaphore:
    """Bound hosted searches so large sheets do not self-throttle/time out."""
    loop = asyncio.get_running_loop()
    semaphore = _search_sems.get(loop)
    if semaphore is None:
        limit = max(1, int(os.getenv("GENIE_OPENAI_SEARCH_CONCURRENCY", "6")))
        semaphore = asyncio.Semaphore(limit)
        _search_sems[loop] = semaphore
    return semaphore


def _api_key() -> str:
    """Read at call time so a rotated key is picked up without restart."""
    return (os.getenv("OPENAI_API_KEY") or "").strip()


def _model() -> str:
    return (os.getenv("GENIE_OPENAI_SEARCH_MODEL") or DEFAULT_MODEL).strip()


def build_query(university_name: str, official_domain: str, country: str = "") -> str:
    """Build the broad recovery query for a university.

    ``official_domain`` is supporting evidence, not a restriction: legitimate
    Moodle, ERP and library systems commonly use a vendor-owned hostname.
    """
    terms = " OR ".join((*_PLATFORM_TERMS, *profile_for_country(country).search_terms))
    return f'"{university_name}" {official_domain} ({terms})'


def build_category_query(university_name: str, official_domain: str,
                         category_keyword_target: str) -> str:
    """Build a narrow query for one missing portal capability."""
    return f'"{university_name}" {official_domain} {category_keyword_target} login'


def build_login_endpoint_query(university_name: str, official_domain: str,
                               country: str = "") -> str:
    """A direct-login query that also permits institution-bound vendor SSO.

    A hard ``site:official_domain`` restriction hid legitimate Okta, SECIOSS,
    Salesforce, hosted LMS and library portals.  The official domain remains a
    strong identity term, but is no longer a search-engine exclusion rule.
    """
    native_terms = " OR ".join(profile_for_country(country).search_terms)
    return (
        f'"{university_name}" "{official_domain}" '
        f'(login OR sign-in OR SSO OR authentication OR Moodle OR LMS OR portal OR {native_terms}) '
        f'direct student login'
    )


def _as_mapping(value: Any) -> Mapping[str, Any] | None:
    """Support SDK objects and dicts (the latter keeps tests simple)."""
    if isinstance(value, Mapping):
        return value
    if hasattr(value, "model_dump"):
        dumped = value.model_dump()
        return dumped if isinstance(dumped, Mapping) else None
    if hasattr(value, "__dict__"):
        return vars(value)
    return None


def parse_results(response: Any, max_results: int = MAX_RESULTS) -> list[dict[str, str]]:
    """Extract cited web-search sources from a Responses API result.

    The Responses API returns sources beneath web-search call actions. We
    recursively locate ``sources`` to remain compatible with SDK shape changes,
    dedupe URLs, and convert them to the crawler's universal candidate shape.
    Search citations often point to an official help page which *mentions* the
    direct portal URL (for example, a university guide that says ``Log in at
    https://portal.example.edu``).  Those direct URLs are useful candidates
    when they originate in a web-search response, but remain untrusted: the
    lookup pipeline still applies its live endpoint, form and student-audience
    checks before returning one.  We therefore retain both cited pages and
    explicit HTTP URLs from the tool's generated search answer.
    """
    cited: list[dict[str, str]] = []
    cited_seen: set[str] = set()

    def add(source: Any) -> None:
        item = _as_mapping(source)
        if not item:
            return
        url = str(item.get("url") or "").strip()
        if not url or url in cited_seen or _DOCUMENT_URL_RE.search(url):
            return
        cited_seen.add(url)
        cited.append({"url": url, "text": str(item.get("title") or "").strip()})

    def walk(value: Any, *, in_sources: bool = False) -> None:
        item = _as_mapping(value)
        if item is not None:
            if in_sources:
                add(item)
                return
            for key, child in item.items():
                walk(child, in_sources=(key == "sources"))
            return
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            for child in value:
                walk(child, in_sources=in_sources)

    walk(response)

    # SDK objects expose the generated answer as ``output_text``.  Test
    # doubles and older response serialisations may hold it in a mapping.
    output_text = getattr(response, "output_text", "")
    if not output_text:
        mapped = _as_mapping(response) or {}
        output_text = str(mapped.get("output_text") or "")
    direct: list[dict[str, str]] = []
    direct_seen: set[str] = set()
    for url in _HTTP_URL_RE.findall(str(output_text)):
        clean = url.rstrip(".,;:!?")
        if (clean and clean not in direct_seen
                and not _DOCUMENT_URL_RE.search(clean)):
            direct_seen.add(clean)
            direct.append({"url": clean, "text": "OpenAI web-search direct URL"})

    # The answer's explicitly written direct endpoints are more valuable than
    # background citation pages. Previously six citations filled the cap first
    # and silently discarded a correct portal named in output_text.
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in direct + cited:
        url = item["url"]
        if url in seen:
            continue
        seen.add(url)
        out.append(item)
        if len(out) >= max_results:
            break
    return out


def _search_instruction(query: str, max_results: int, country: str = "") -> str:
    profile = profile_for_country(country)
    return (
        "Use web search to find up to " + str(max_results) +
        " direct URLs for real student-facing login portals for this institution. "
        "Prioritise student ERP/SIS, LMS (such as Moodle or Canvas), and library "
        "authentication pages. Do not search for admissions, staff/HR, alumni, PDFs, "
        "or generic homepages. Include university-owned subdomains when they "
        "host the service. Prefer the direct login endpoint over an article "
        "which merely describes it. In your answer, explicitly write each "
        "direct URL you found. Search in " + profile.languages +
        " as well as English when relevant. Search query: " + query
    )


async def execute_search_fallback(
    university_name: str,
    official_domain: str,
    *,
    max_results: int = MAX_RESULTS,
    timeout_seconds: int = DEFAULT_TIMEOUT_S,
    session: Any | None = None,
    query: str | None = None,
    client: AsyncOpenAI | None = None,
    country: str = "",
    search_context_size: str = "medium",
) -> list[dict[str, str]]:
    """Use OpenAI web search to return untrusted portal candidates.

    ``session`` remains accepted for call compatibility with the old search
    adapter; it is intentionally unused because the OpenAI SDK owns its HTTP
    client. ``client`` is an injection point for tests.
    """
    del session
    key = _api_key()
    if not key:
        logger.warning("search: OPENAI_API_KEY not set — web-search fallback disabled")
        return []

    q = query or build_query(university_name, official_domain, country)
    own_client = client is None
    api = client or AsyncOpenAI(
        api_key=key,
        timeout=timeout_seconds,
        max_retries=0,  # this rescue path owns retries at the orchestration layer
    )
    try:
        async with _get_search_sem():
            response = await api.responses.create(
                model=_model(),
                tools=[{"type": "web_search", "search_context_size": search_context_size}],
                tool_choice="required",
                include=["web_search_call.action.sources"],
                input=_search_instruction(q, max_results, country),
            )
        hits = parse_results(response, max_results)
        if not hits:
            logger.info("search: OpenAI web search returned 0 cited sources for %r", university_name)
        logger.info("search: %r (%s) -> %d result(s)", university_name, official_domain, len(hits))
        return hits
    except asyncio.CancelledError:
        raise
    except APITimeoutError:
        logger.warning("search: OpenAI timed out after %ss for %r", timeout_seconds, university_name)
    except RateLimitError:
        logger.error("search: OpenAI web-search rate limit hit for %r", university_name)
    except APIError as exc:
        logger.warning("search: OpenAI API error for %r: %s", university_name, str(exc)[:160])
    except Exception as exc:  # noqa: BLE001 — a rescue path must never abort a run
        logger.warning("search: unexpected %s: %s", type(exc).__name__, str(exc)[:160])
    finally:
        if own_client:
            await api.close()
    return []

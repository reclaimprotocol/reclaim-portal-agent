"""Genie-V3 · Layer 2 — external web-search fallback (OpenRouter).

The normal path is still the university's own rendered website. This module is
only a recovery path for a crawl that yields no useful links, or a targeted
capability expansion. OpenAI web search supplies *candidate* URLs from the
indexed web; it is deliberately not trusted as a portal verdict. Every URL it
returns goes through the same filter, model classification, live-endpoint and
student-login-surface checks as a URL found on the university homepage.

This uses OpenRouter's ``openrouter:web_search`` server tool, so it shares the
existing OpenRouter account/key and may use an OpenAI model directly. A
missing/invalid key, rate limit, timeout, or malformed response always degrades
to ``[]`` so a search outage cannot fail a long batch run.
"""
from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Mapping, Sequence
from typing import Any

from openai import APIError, APITimeoutError, AsyncOpenAI, RateLimitError

logger = logging.getLogger("genie.search")

MAX_RESULTS = 6
DEFAULT_TIMEOUT_S = 25
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "openai/gpt-5.6-terra"

_PLATFORM_TERMS = ("student portal", "login", "samarth", "moodle", "erp")


def _api_key() -> str:
    """Read at call time so a rotated key is picked up without restart."""
    return (os.getenv("OPENROUTER_API_KEY") or "").strip()


def _model() -> str:
    return (os.getenv("GENIE_OPENROUTER_SEARCH_MODEL") or DEFAULT_MODEL).strip()


def build_query(university_name: str, official_domain: str) -> str:
    """Build the broad recovery query for a university.

    ``official_domain`` is supporting evidence, not a restriction: legitimate
    Moodle, ERP and library systems commonly use a vendor-owned hostname.
    """
    terms = " OR ".join(_PLATFORM_TERMS)
    return f'"{university_name}" {official_domain} ({terms})'


def build_category_query(university_name: str, official_domain: str,
                         category_keyword_target: str) -> str:
    """Build a narrow query for one missing portal capability."""
    return f'"{university_name}" {official_domain} {category_keyword_target} login'


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
    Generated prose is never parsed as a URL.
    """
    found: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(source: Any) -> None:
        item = _as_mapping(source)
        if not item:
            return
        url = str(item.get("url") or "").strip()
        if not url or url in seen:
            return
        seen.add(url)
        found.append({"url": url, "text": str(item.get("title") or "").strip()})

    def walk(value: Any, *, in_sources: bool = False) -> None:
        if len(found) >= max_results:
            return
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
                if len(found) >= max_results:
                    return

    walk(response)
    return found


def _search_instruction(query: str, max_results: int) -> str:
    return (
        "Use web search to find up to " + str(max_results) +
        " direct URLs for real student-facing login portals for this institution. "
        "Prioritise student ERP/SIS, LMS (such as Moodle or Canvas), and library "
        "authentication pages. Do not search for admissions, staff/HR, alumni, PDFs, "
        "or generic homepages. Search query: " + query
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
) -> list[dict[str, str]]:
    """Use OpenRouter web search to return untrusted portal candidates.

    ``session`` remains accepted for call compatibility with the old Serper
    adapter; it is intentionally unused because the OpenAI SDK owns its HTTP
    client. ``client`` is an injection point for tests.
    """
    del session
    key = _api_key()
    if not key:
        logger.warning("search: OPENROUTER_API_KEY not set — web-search fallback disabled")
        return []

    q = query or build_query(university_name, official_domain)
    own_client = client is None
    api = client or AsyncOpenAI(
        base_url=OPENROUTER_BASE_URL,
        api_key=key,
        timeout=timeout_seconds,
        default_headers={
            "HTTP-Referer": "https://github.com/reclaimprotocol",
            "X-Title": "Genie-V3 portal discovery",
        },
    )
    try:
        response = await api.responses.create(
            model=_model(),
            tools=[{"type": "openrouter:web_search", "parameters": {
                "engine": "native", "max_results": max_results,
                "max_total_results": max_results,
            }}],
            tool_choice="required",
            include=["web_search_call.action.sources"],
            input=_search_instruction(q, max_results),
        )
        hits = parse_results(response, max_results)
        if not hits:
            logger.info("search: OpenRouter web search returned 0 cited sources for %r", university_name)
        logger.info("search: %r (%s) -> %d result(s)", university_name, official_domain, len(hits))
        return hits
    except asyncio.CancelledError:
        raise
    except APITimeoutError:
        logger.warning("search: OpenRouter timed out after %ss for %r", timeout_seconds, university_name)
    except RateLimitError:
        logger.error("search: OpenRouter web-search rate limit hit for %r", university_name)
    except APIError as exc:
        logger.warning("search: OpenRouter API error for %r: %s", university_name, str(exc)[:160])
    except Exception as exc:  # noqa: BLE001 — a rescue path must never abort a run
        logger.warning("search: unexpected %s: %s", type(exc).__name__, str(exc)[:160])
    finally:
        if own_client:
            await api.close()
    return []

"""Stage A Pass 2 — Claude fallback using the Anthropic `web_search` server tool.

Only invoked when the rule-based pass yields fewer than 2 validated portals.
Claude returns a strict JSON object; malformed responses are logged and
treated as "no additional portals found" (Pass 1 results are still returned).
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from ..anthropic_client import AnthropicClient
from .discovery_rules import Candidate

logger = logging.getLogger(__name__)

ALLOWED_CATEGORIES: tuple[str, ...] = (
    "Student Portal",
    "LMS/Moodle",
    "Fee Portal",
    "Examination Portal",
    "ERP",
    "Academic Portal",
    "Hostel Portal",
    "Library Portal",
    "Other",
)

_ALLOWED_CATEGORIES_SET = set(ALLOWED_CATEGORIES)

SYSTEM_PROMPT = (
    "You are a research assistant helping catalogue student-login portals for universities. "
    "Given a university and a list of portals already discovered by rule-based search, use the "
    "web_search tool to find additional student-login portals that were missed. A student-login "
    "portal is a web page where enrolled students authenticate with a username / roll number / "
    "enrollment ID and a password to access services such as LMS, exam results, fee payment, "
    "ERP, hostel management, or library/OPAC systems.\n\n"
    "Respond with a single JSON object and nothing else — no prose, no markdown fences.\n"
    "The object has exactly one key, 'portals', whose value is a list of objects. Each object "
    "has three string fields: 'url' (the full portal URL), 'category' (one of: "
    + ", ".join(ALLOWED_CATEGORIES)
    + "), and 'reasoning' (one short sentence explaining why you believe this is a student-login "
    "portal). If you find no additional portals beyond the known list, return "
    "{\"portals\": []}."
)


def run_claude_fallback(
    *,
    anthropic: AnthropicClient,
    model: str,
    university_name: str,
    domains: list[str],
    known_portals: list[Candidate],
    max_uses: int,
    max_retries: int = 3,
) -> list[Candidate]:
    user_prompt = _build_user_prompt(university_name, domains, known_portals)

    try:
        text = anthropic.complete_with_web_search(
            system=SYSTEM_PROMPT,
            user=user_prompt,
            model=model,
            max_uses=max_uses,
            max_tokens=4096,
            max_retries=max_retries,
        )
    except Exception:
        logger.exception("Claude fallback: API call failed; skipping Pass 2")
        return []

    portals = _parse_portals_json(text)
    if portals is None:
        logger.warning(
            "Claude fallback: could not parse JSON from response. Raw text:\n%s",
            text,
        )
        return []

    out: list[Candidate] = []
    for raw in portals:
        if not isinstance(raw, dict):
            continue
        url = str(raw.get("url", "")).strip()
        if not url:
            continue
        category = str(raw.get("category", "")).strip()
        if category not in _ALLOWED_CATEGORIES_SET:
            logger.debug("Claude returned category %r; coercing to 'Other'", category)
            category = "Other"
        reasoning = str(raw.get("reasoning", "")).strip() or "Claude web_search suggestion"
        out.append(
            Candidate(
                url=url,
                category=category,
                discovery_source="claude",
                discovery_reasoning=reasoning,
            )
        )
    return out


# --------------------------------------------------------------- internals

def _build_user_prompt(
    name: str,
    domains: list[str],
    known: list[Candidate],
) -> str:
    if known:
        known_block = "\n".join(f"  - {c.url} ({c.category})" for c in known)
    else:
        known_block = "  (none)"
    return (
        f"University name: {name}\n"
        f"Known domains: {', '.join(domains)}\n"
        f"Portals already found by rule-based search:\n{known_block}\n\n"
        "Find any additional student-login portals for this university that are hosted on "
        "these domains, any subdomain of them, or on a clearly-official third-party service "
        "the university delegates to (e.g. a Samarth ERP instance, a shared state-university "
        "exam portal). Do not duplicate anything in the known list."
    )


_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def _parse_portals_json(text: str) -> list[dict[str, Any]] | None:
    if not text:
        return None
    attempts = (text, _strip_code_fences(text))
    for candidate in attempts:
        try:
            data = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        portals = data.get("portals") if isinstance(data, dict) else None
        if isinstance(portals, list):
            return portals
    m = _JSON_OBJECT_RE.search(text)
    if m:
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
        portals = data.get("portals") if isinstance(data, dict) else None
        if isinstance(portals, list):
            return portals
    return None


def _strip_code_fences(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("```"):
        return text
    lines = stripped.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines)

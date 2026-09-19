"""Genie-V3 service · one organisation, start to finish.

Runs the V3 engine for a single org and turns its answer into output rows:

    1. liveness-check the portals the caller already has
    2. discover portals from the website
    3. subtract the ones they already have  <-- the only genuinely new logic
    4. emit one row per new portal, or exactly one row saying none was found

WHAT "NEW" MEANS
----------------
Suppression is at HOST level, not exact URL. If the caller already has
`erp.x.edu/login` and the crawl surfaces `erp.x.edu/student/login`, that is the
same system reached by a second path, not a discovery — the extraction schema
already treats path variants on one host as a single portal, and reporting it
as new costs a human review to reach the same conclusion.

Nothing is silently dropped: whatever was suppressed is reported in its own
column, because a filter you cannot audit is a filter you cannot trust.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Sequence

from service.csvio import (InputRow, STATUS_FAILED, STATUS_NEW, STATUS_NONE)

logger = logging.getLogger("genie.service.discovery")

#: Liveness budget for re-checking the caller's existing portals. Separate from
#: the engine's own guardrail timeouts — this is a cheap HEAD sweep, not a crawl.
KNOWN_CHECK_TIMEOUT_S = int(os.getenv("AGENT_KNOWN_CHECK_TIMEOUT", "8"))
KNOWN_CHECK_ENABLED = os.getenv("AGENT_CHECK_KNOWN_PORTALS", "1").strip().lower() \
    not in ("0", "false", "no")


def _host(url: str) -> str:
    from agent.graph_matcher import host_of
    return host_of(url)


def _tnc_reason(portal: dict, stats: dict) -> str:
    """Why this portal has no terms — the distinction a reviewer needs.

    'we found no legal links to score' and 'we found some and rejected them
    all' demand completely different follow-up, and a blank cell hides which
    one happened.

    Only two of the four cases are separable from here: the finer split
    (foreign-domain veto vs. vendor-terms cap) lives in the matcher's
    `last_edges`, which `find_portals` does not return. Surfacing those would
    mean threading the edge list out of the engine.
    """
    if portal.get("tnc_url"):
        return ""
    harvested = int(stats.get("legal_from_homepage", 0) or 0) + \
        int(stats.get("legal_from_portals", 0) or 0)
    return "no_legal_links_found" if harvested == 0 else "no_match_above_threshold"


async def check_known_portals(urls: Sequence[str], country: str = "") -> list[str]:
    """Return the caller's portals that are NOT live.

    Answers 'mark previous portal urls as not correct' for roughly one HTTP
    request per URL. Note the guardrail counts 401/403/429 as ALIVE — a WAF
    refusing us still proves a server is there — so this does not flood the
    report with false verdicts on protected portals.
    """
    if not urls or not KNOWN_CHECK_ENABLED:
        return []
    from agent.guardrails import verify_portal_endpoint_detailed
    checks = await asyncio.gather(*(
        verify_portal_endpoint_detailed(u, KNOWN_CHECK_TIMEOUT_S, country_hint=country)
        for u in urls), return_exceptions=True)
    dead = []
    for u, c in zip(urls, checks):
        if isinstance(c, BaseException):
            continue                      # an errored probe is not evidence of death
        if not c[0]:
            dead.append(u)
    return dead


async def process_org(row: InputRow) -> list[dict]:
    """Discover, diff, and return this org's output rows. Never raises."""
    base = {"orgId": row.org_id}
    try:
        from agent.lookup import find_portals

        known_hosts = {_host(u) for u in row.current_portals if u.strip()}
        dead_known, result = await asyncio.gather(
            check_known_portals(row.current_portals, row.country),
            find_portals(row.website, name=row.name, country=row.country))

        base["dead_known_portals"] = "|".join(dead_known)
        stats = result.get("stats") or {}

        fresh, suppressed = [], []
        for p in (result.get("portals") or []):
            (suppressed if _host(p.get("url", "")) in known_hosts else fresh).append(p)
        base["suppressed_as_known"] = "|".join(
            p.get("url", "") for p in suppressed)

        if not fresh:
            logger.info("org %s (%s) — no new portals (%d already known, %d dead)",
                        row.org_id, row.website, len(suppressed), len(dead_known))
            return [{**base, "status": STATUS_NONE,
                     "error": result.get("error", "") if not result.get("portals") else ""}]

        logger.info("org %s (%s) — %d NEW portal(s), %d suppressed as known",
                    row.org_id, row.website, len(fresh), len(suppressed))
        return [{**base,
                 "status": STATUS_NEW,
                 "new_portal_url": p.get("url", ""),
                 "category": p.get("category", ""),
                 "system": p.get("system", ""),
                 "tnc_url": p.get("tnc_url") or "",
                 "tnc_reason": _tnc_reason(p, stats),
                 "confidence": ("" if p.get("match_confidence") is None
                                else p["match_confidence"]),
                 "match_basis": p.get("match_basis") or "",
                 "http_status": p.get("http_status", "")}
                for p in fresh]

    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — one org must never end the run
        logger.exception("org %s (%s) failed", row.org_id, row.website)
        return [{**base, "status": STATUS_FAILED,
                 "error": f"{type(exc).__name__}: {str(exc)[:300]}"}]

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
#: Probes in flight per organisation. `current_portals` is caller-supplied and
#: unbounded, so firing the whole cell at once can exhaust sockets — and a
#: probe that times out because WE ran out of sockets is recorded as a dead
#: portal, which is exactly the false verdict this feature exists to avoid.
KNOWN_CHECK_CONCURRENCY = max(1, int(os.getenv("AGENT_KNOWN_CHECK_CONCURRENCY", "8")))
#: Hard cap per org. Beyond this the list is almost certainly a data error
#: rather than a real portal inventory; truncate loudly instead of crawling it.
KNOWN_CHECK_MAX = max(1, int(os.getenv("AGENT_KNOWN_CHECK_MAX", "50")))


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


async def check_known_portals(urls: Sequence[str],
                              country: str = "") -> tuple[list[str], str]:
    """Return (portals that are NOT live, note).

    `note` is non-empty when the supplied list was truncated. Returning it
    rather than only logging it matters: an empty dead-list that came from a
    truncated check looks identical to a clean bill of health, and the caller
    acts on the CSV, not on our logs.

    Answers 'mark previous portal urls as not correct' for roughly one HTTP
    request per URL. Note the guardrail counts 401/403/429 as ALIVE — a WAF
    refusing us still proves a server is there — so this does not flood the
    report with false verdicts on protected portals.
    """
    if not urls or not KNOWN_CHECK_ENABLED:
        return [], ""
    from agent.guardrails import verify_portal_endpoint_detailed

    urls, supplied, note = list(urls), len(urls), ""
    if supplied > KNOWN_CHECK_MAX:
        note = f"known_portals_truncated: checked {KNOWN_CHECK_MAX} of {supplied}"
        logger.warning("%d known portals supplied — checking the first %d only",
                       supplied, KNOWN_CHECK_MAX)
        urls = urls[:KNOWN_CHECK_MAX]

    sem = asyncio.Semaphore(KNOWN_CHECK_CONCURRENCY)

    async def probe(u: str):
        async with sem:
            return await verify_portal_endpoint_detailed(
                u, KNOWN_CHECK_TIMEOUT_S, country_hint=country)

    checks = await asyncio.gather(*(probe(u) for u in urls),
                                  return_exceptions=True)
    dead = []
    for u, c in zip(urls, checks):
        if isinstance(c, BaseException):
            continue                      # an errored probe is not evidence of death
        if not c[0]:
            dead.append(u)
    return dead, note


async def process_org(row: InputRow) -> list[dict]:
    """Discover, diff, and return this org's output rows. Never raises."""
    base = {"orgId": row.org_id}
    try:
        from agent.lookup import find_portals

        known_hosts = {_host(u) for u in row.current_portals if u.strip()}
        dead_known, result = await asyncio.gather(
            check_known_portals(row.current_portals, row.country),
            find_portals(row.website, name=row.name, country=row.country))

        dead_list, known_note = dead_known
        base["dead_known_portals"] = "|".join(dead_list)

        # A portal removed by the https policy must never look like "this
        # university has no portal" — that is the same silent-filter trap the
        # rest of this pipeline is built to avoid, and it is exactly how the
        # rule went missing in V3 unnoticed in the first place.
        notes = [known_note] if known_note else []
        insecure = result.get("insecure_dropped") or []
        if insecure:
            notes.append("dropped_insecure_http: " + "|".join(insecure))
            logger.info("org %s (%s) — %d portal(s) dropped by https policy",
                        row.org_id, row.website, len(insecure))
        base["notes"] = "; ".join(notes)
        stats = result.get("stats") or {}

        fresh, suppressed = [], []
        for p in (result.get("portals") or []):
            (suppressed if _host(p.get("url", "")) in known_hosts else fresh).append(p)
        base["suppressed_as_known"] = "|".join(
            p.get("url", "") for p in suppressed)

        if not fresh:
            logger.info("org %s (%s) — no new portals (%d already known, %d dead)",
                        row.org_id, row.website, len(suppressed), len(dead_list))
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

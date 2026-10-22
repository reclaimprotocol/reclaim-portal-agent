"""Genie-V3 · Single-domain lookup.

Give it one university domain and it returns that university's student portals
and the legal documents governing them — no spreadsheet, no org ID, no batch.

    .venv/bin/python -m agent.lookup buet.ac.bd
    .venv/bin/python -m agent.lookup sgu.edu.vn --name "Saigon University"
    .venv/bin/python -m agent.lookup unifran.edu.br --portals-only
    .venv/bin/python -m agent.lookup kbu.ac.th --json > result.json

Or from Python:

    from agent.lookup import find_portals
    result = await find_portals("buet.ac.bd")

WHY THIS IS A SEPARATE ENTRY POINT
----------------------------------
`v3_orchestrator` is sheet-driven end to end: it reads rows from Google Sheets,
writes CSVs under a run-wide date stamp, and records each organisation into the
resume shield. None of that belongs in a one-off lookup — a person checking a
single university should not need OAuth credentials, and should not have their
answer appended to a batch deliverable.

So this module reuses the same COMPONENTS (crawler, filter, cascade, guardrails,
portal-level harvest, graph matcher) in the same order, but returns a plain dict
instead of writing anywhere. The only shared mutable state it touches is the
Layer-5 vendor cache, and only for reading.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try:
    from dotenv import load_dotenv; load_dotenv(ROOT / ".env")
except Exception:  # noqa: BLE001
    pass

from agent.crawler import extract_raw_university_links          # noqa: E402
from agent.filters import LocalKnowledgeMatrixFilter            # noqa: E402
from agent.graph_matcher import (GraphComplianceMatcher, host_of,  # noqa: E402
                                 DISTANCE_NATIVE_CRAWL, DISTANCE_SEARCH_FALLBACK)
from agent.guardrails import (verify_portal_endpoint_final,     # noqa: E402
                              verify_student_login_surface, portal_url_allowed,
                              transport_ok, REQUIRE_HTTPS)
from agent.memory_cache import MemoryCache, signature           # noqa: E402
from agent.openrouter_cascade import execute_model_cascade      # noqa: E402
from agent.schemas import IntegratedDiscoveryOutput             # noqa: E402
from agent.search_fallback import execute_search_fallback       # noqa: E402
from agent.v3_orchestrator import (harvest_portal_legal_links,  # noqa: E402
                                   UniversityRow, GUARDRAIL_TIMEOUT_S,
                                   GUARDRAIL_RETRY_S)

logger = logging.getLogger("genie.lookup")

# Independent lookups may run in parallel, but each one starts a heavyweight
# Crawl4AI/Chromium session.  Serialising this one fragile stage avoids browser
# collisions while model calls, web search, and endpoint verification continue
# concurrently after a crawl completes.
_browser_sem: asyncio.Semaphore | None = None


def _get_browser_sem() -> asyncio.Semaphore:
    global _browser_sem
    if _browser_sem is None:
        limit = max(1, int(os.getenv("GENIE_LOOKUP_BROWSER_CONCURRENCY", "1")))
        _browser_sem = asyncio.Semaphore(limit)
    return _browser_sem


def _clean_domain(raw: str) -> str:
    """Accept 'buet.ac.bd', 'https://buet.ac.bd/x', or 'www.buet.ac.bd/'."""
    d = (raw or "").strip()
    if "://" in d:
        d = urlsplit(d).netloc
    d = d.split("/")[0].split("@")[-1].split(":")[0].strip().strip(".")
    return d[4:] if d.startswith("www.") else d


async def find_portals(
    domain: str,
    *,
    name: str = "",
    country: str = "",
    portals_only: bool = False,
    page_timeout_ms: int = 30_000,
    use_search_fallback: bool = True,
) -> dict[str, Any]:
    """Discover portals for one university domain.

    Args:
        domain: bare domain or any URL on it.
        name: institution name. Improves model accuracy; the domain is used
            when omitted, which is usually good enough.
        country: used to pick the residential-proxy exit when the domain
            carries no ccTLD (a .com/.org university still needs a local exit).
        portals_only: skip all compliance work — no portal-level crawl, no
            graph matching. Roughly halves the runtime.
        use_search_fallback: fall back to web search when the crawl finds
            nothing.

    Returns a dict with `domain`, `portals` (each with `tnc_url` unless
    `portals_only`), and a `stats` block explaining what happened.
    """
    domain = _clean_domain(domain)
    if not domain:
        return {"domain": "", "portals": [], "error": "no domain given"}

    row = UniversityRow(row=0, org_id="lookup", name=name or domain,
                        domain=domain, country=country)
    lkm = LocalKnowledgeMatrixFilter()
    cache = MemoryCache()
    matcher = GraphComplianceMatcher(
        saas_roots={e["root"] for e in (lkm.kb.get("saas_infra_whitelist") or [])
                    if e.get("root")},
        learned_keywords=lkm.kb.get("learned_legal_keywords") or [])
    stats: dict[str, Any] = {"crawled_links": 0, "candidates": 0,
                             "used_search": False, "search_reason": "",
                             "dead_portals": 0,
                             "rejected_non_web_urls": 0,
                             "rejected_non_login_pages": 0,
                             "rejected_non_student_portals": 0,
                             "rejected_external_redirects": 0}

    # 1. crawl -----------------------------------------------------------
    async with _get_browser_sem():
        links = await extract_raw_university_links(
            domain, page_timeout_ms=page_timeout_ms, country_hint=country)
    stats["crawled_links"] = len(links or [])
    candidates = lkm.filter_and_rank_links(links) if links else []

    # 2. search rescue ---------------------------------------------------
    # Crawl4AI deliberately returns [] on an unrenderable/blocked page so one
    # site cannot abort a batch.  Treat that outcome as a reason to search the
    # live web, not as evidence that the university has no portal.
    if not candidates and use_search_fallback:
        stats["search_reason"] = "crawl yielded no usable portal candidates"
        logger.info("lookup: %s — %s; invoking OpenAI web-search rescue",
                    domain, stats["search_reason"])
        hits = await execute_search_fallback(row.name, domain)
        stats["used_search"] = True
        if hits:
            candidates = lkm.filter_and_rank_links(hits) or hits
    stats["candidates"] = len(candidates)
    if not candidates:
        return {"domain": domain, "name": row.name, "portals": [],
                "stats": stats, "error": "no candidates found"}

    # 3. extract ---------------------------------------------------------
    result: IntegratedDiscoveryOutput = await asyncio.to_thread(
        execute_model_cascade, "lookup", row.name, domain, candidates)
    # A homepage can contain links but none that identify a student portal.
    # Give web search one independent chance in this case too.  Search results
    # are still only candidates: they go through the same classifier and the
    # strict endpoint/login-surface checks below.
    if (not result.discovered_portals and use_search_fallback
            and not stats["used_search"]):
        stats["search_reason"] = "website candidates identified no portal"
        logger.info("lookup: %s — %s; invoking OpenAI web-search rescue",
                    domain, stats["search_reason"])
        hits = await execute_search_fallback(row.name, domain)
        stats["used_search"] = True
        if hits:
            search_candidates = lkm.filter_and_rank_links(hits) or hits
            stats["candidates"] += len(search_candidates)
            result = await asyncio.to_thread(
                execute_model_cascade, "lookup", row.name, domain,
                search_candidates)
    if not result.discovered_portals:
        return {"domain": domain, "name": row.name, "portals": [],
                "stats": stats, "error": "no portals identified"}

    # 4. verify ----------------------------------------------------------
    # Never send document URLs or literal IPs to the network guard.  Apart from
    # being useless as a student portal, those are often model/search noise and
    # IP literals can point at infrastructure that this workflow must not probe.
    allowed_portals = []
    for portal in result.discovered_portals:
        allowed, reason = portal_url_allowed(portal.exact_url)
        if allowed:
            allowed_portals.append(portal)
        else:
            stats["rejected_non_web_urls"] += 1
            logger.info("lookup: %s — dropped %s (%s)",
                        domain, portal.exact_url, reason)
    if not allowed_portals:
        return {"domain": domain, "name": result.university_name, "portals": [],
                "stats": stats, "error": "no web portal candidates"}

    checks = await asyncio.gather(*(
        verify_portal_endpoint_final(p.exact_url, GUARDRAIL_TIMEOUT_S,
                                     country_hint=country,
                                     retry_timeout_seconds=GUARDRAIL_RETRY_S)
        for p in allowed_portals))
    reachable = [(p, c) for p, c in zip(allowed_portals, checks) if c[0]]
    stats["dead_portals"] = len(allowed_portals) - len(reachable)

    # --- transport policy ------------------------------------------------
    # Applied to where the request LANDED, not to the published link: many
    # institutions advertise http and redirect to https, and judging those on
    # the input would discard perfectly secure portals.
    #
    # The landing URL is adopted only when it is the SAME HOST. A portal that
    # redirects to an identity provider lands on accounts.google.com or
    # login.microsoftonline.com, and recording THAT as the student portal
    # would be worse than recording nothing.
    live: list[Any] = []
    insecure: list[str] = []
    for p, c in reachable:
        landed = (c[3] or p.exact_url)
        if host_of(landed) != host_of(p.exact_url):
            # We cannot verify that the originally-published URL itself is a
            # student login if it bounces to a generic IdP or another host.
            # Do not report the original link as a verified portal.
            stats["rejected_external_redirects"] += 1
            logger.info("lookup: %s — dropped %s (redirected to %s)",
                        domain, p.exact_url, landed)
            continue
        p.exact_url = landed              # same-host http -> https upgrade
        if transport_ok(landed):
            live.append((p, c))
        else:
            insecure.append(landed)
    if insecure:
        logger.info("lookup: %s — %d portal(s) dropped, final URL not https: %s",
                    domain, len(insecure), ", ".join(insecure[:3]))
    stats["dropped_insecure_http"] = len(insecure)
    stats["require_https"] = REQUIRE_HTTPS

    # Reachable is not the same as usable.  Require an HTML page containing a
    # password field, identifier field, and sign-in/authorise control before a
    # URL can be returned to a user as a student login portal.
    auth_checks = await asyncio.gather(*(
        verify_student_login_surface(p.exact_url, country_hint=country)
        for p, _ in live))
    verified_live = []
    for (portal, check), (accepted, note, final_url) in zip(live, auth_checks):
        if accepted:
            portal.exact_url = final_url
            verified_live.append((portal, check))
        else:
            stats["rejected_non_login_pages"] += 1
            if note in {"non-student audience only", "no student or academic audience evidence"}:
                stats["rejected_non_student_portals"] += 1
            logger.info("lookup: %s — dropped %s (not a verified login page: %s)",
                        domain, portal.exact_url, note)
    live = verified_live

    if portals_only:
        return {
            "domain": domain, "name": result.university_name,
            "insecure_dropped": insecure,
            "portals": [{"url": p.exact_url, "category": p.category,
                         "system": p.portal_system_name, "http_status": c[1]}
                        for p, c in live],
            "stats": stats,
        }

    # 5. portal-level legal harvest + graph match ------------------------
    legal = list(result.harvested_legal_links or [])
    stats["legal_from_homepage"] = len(legal)
    sem = asyncio.Semaphore(4)
    portal_legal = await harvest_portal_legal_links(row, live, lkm, cache, sem)
    seen = {(getattr(c, "url", "") or "") for c in legal}
    legal += [c for c in portal_legal if c["url"] not in seen]
    stats["legal_from_portals"] = len(portal_legal)

    distance = DISTANCE_SEARCH_FALLBACK if stats["used_search"] else DISTANCE_NATIVE_CRAWL
    mapping = matcher.resolve_optimal_compliance_mappings(
        [p for p, _ in live], legal, distance, official_domain=domain)

    portals = []
    for p, c in live:
        m = mapping.get(p.exact_url) or {}
        tnc = m.get("tnc_url")
        if not tnc:
            cached = cache.legal_for_portal(p.exact_url)
            if cached:
                tnc = cached.get("tnc_url") or cached.get("privacy_policy_url")
                m = {"confidence": None, "domain_relation": "memory-cache"}
        portals.append({
            "url": p.exact_url, "category": p.category,
            "system": p.portal_system_name, "http_status": c[1],
            "tnc_url": tnc,
            "match_confidence": m.get("confidence"),
            "match_basis": m.get("domain_relation"),
        })
    return {"domain": domain, "name": result.university_name,
            "portals": portals, "insecure_dropped": insecure, "stats": stats}


# --------------------------------------------------------------------------- #
def _render(res: dict[str, Any]) -> None:
    name = res.get("name") or res.get("domain")
    print(f"\n  {name}   ({res.get('domain')})")
    print("  " + "─" * 66)
    if res.get("error") and not res.get("portals"):
        print(f"  no result — {res['error']}")
    for p in res.get("portals", []):
        print(f"\n  [{p['category']}]  {p['system']}")
        print(f"    portal : {p['url']}")
        if "tnc_url" in p:
            if p["tnc_url"]:
                conf = p.get("match_confidence")
                basis = p.get("match_basis") or ""
                tail = f"   (confidence {conf}, {basis})" if conf is not None else f"   ({basis})"
                print(f"    terms  : {p['tnc_url']}{tail}")
            else:
                print("    terms  : none found")
    s = res.get("stats", {})
    if s:
        bits = [f"{s.get('crawled_links',0)} links crawled",
                f"{s.get('candidates',0)} candidates"]
        if s.get("used_search"):
            bits.append("web search used")
        if s.get("dead_portals"):
            bits.append(f"{s['dead_portals']} dead endpoint(s) dropped")
        if s.get("legal_from_portals") is not None:
            bits.append(f"legal links: {s.get('legal_from_homepage',0)} homepage "
                        f"+ {s['legal_from_portals']} portal-level")
        print("\n  " + " · ".join(bits))
    print()


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="agent.lookup",
        description="Find the student portals (and their terms) for one university domain.")
    ap.add_argument("domain", help="e.g. buet.ac.bd — or any URL on that domain")
    ap.add_argument("--name", default="", help="institution name; improves accuracy")
    ap.add_argument("--country", default="", help="used to pick the proxy exit")
    ap.add_argument("--portals-only", action="store_true",
                    help="skip all compliance work — portals only, ~2x faster")
    ap.add_argument("--no-search", action="store_true",
                    help="do not fall back to web search")
    ap.add_argument("--page-timeout-ms", type=int, default=30_000)
    ap.add_argument("--json", action="store_true", help="emit JSON instead of text")
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO if a.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    for noisy in ("aiohttp", "openai", "urllib3", "crawl4ai", "httpx", "instructor"):
        logging.getLogger(noisy).setLevel(logging.ERROR)

    res = asyncio.run(find_portals(
        a.domain, name=a.name, country=a.country,
        portals_only=a.portals_only, page_timeout_ms=a.page_timeout_ms,
        use_search_fallback=not a.no_search))

    if a.json:
        print(json.dumps(res, indent=2, ensure_ascii=False))
    else:
        _render(res)
    sys.exit(0 if res.get("portals") else 1)


if __name__ == "__main__":
    main()

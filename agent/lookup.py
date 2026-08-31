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
from agent.graph_matcher import (GraphComplianceMatcher,        # noqa: E402
                                 DISTANCE_NATIVE_CRAWL, DISTANCE_SEARCH_FALLBACK)
from agent.guardrails import verify_portal_endpoint_detailed    # noqa: E402
from agent.memory_cache import MemoryCache, signature           # noqa: E402
from agent.openrouter_cascade import execute_model_cascade      # noqa: E402
from agent.schemas import IntegratedDiscoveryOutput             # noqa: E402
from agent.search_fallback import execute_search_fallback       # noqa: E402
from agent.v3_orchestrator import (harvest_portal_legal_links,  # noqa: E402
                                   UniversityRow, GUARDRAIL_TIMEOUT_S,
                                   GUARDRAIL_RETRY_S)

logger = logging.getLogger("genie.lookup")


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
                             "used_search": False, "dead_portals": 0}

    # 1. crawl -----------------------------------------------------------
    links = await extract_raw_university_links(
        domain, page_timeout_ms=page_timeout_ms, country_hint=country)
    stats["crawled_links"] = len(links or [])
    candidates = lkm.filter_and_rank_links(links) if links else []

    # 2. search rescue ---------------------------------------------------
    if not candidates and use_search_fallback:
        hits = await execute_search_fallback(row.name, domain)
        if hits:
            stats["used_search"] = True
            candidates = lkm.filter_and_rank_links(hits) or hits
    stats["candidates"] = len(candidates)
    if not candidates:
        return {"domain": domain, "name": row.name, "portals": [],
                "stats": stats, "error": "no candidates found"}

    # 3. extract ---------------------------------------------------------
    result: IntegratedDiscoveryOutput = await asyncio.to_thread(
        execute_model_cascade, "lookup", row.name, domain, candidates)
    if not result.discovered_portals:
        return {"domain": domain, "name": row.name, "portals": [],
                "stats": stats, "error": "no portals identified"}

    # 4. verify ----------------------------------------------------------
    checks = await asyncio.gather(*(
        verify_portal_endpoint_detailed(p.exact_url, GUARDRAIL_TIMEOUT_S,
                                        country_hint=country,
                                        retry_timeout_seconds=GUARDRAIL_RETRY_S)
        for p in result.discovered_portals))
    live = [(p, c) for p, c in zip(result.discovered_portals, checks) if c[0]]
    stats["dead_portals"] = len(result.discovered_portals) - len(live)

    if portals_only:
        return {
            "domain": domain, "name": result.university_name,
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
            "portals": portals, "stats": stats}


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

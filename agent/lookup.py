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
import re
import sys
import weakref
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try:
    from dotenv import load_dotenv; load_dotenv(ROOT / ".env")
except Exception:  # noqa: BLE001
    pass

from agent.crawler import (extract_targeted_university_links,   # noqa: E402
                           extract_links_from_evidence_pages)
from agent.filters import LocalKnowledgeMatrixFilter            # noqa: E402
from agent.graph_matcher import (GraphComplianceMatcher,           # noqa: E402
                                 DISTANCE_NATIVE_CRAWL, DISTANCE_SEARCH_FALLBACK)
from agent.guardrails import (verify_portal_endpoint_final,     # noqa: E402
                              verify_student_login_surface_hybrid, portal_url_allowed,
                              portal_dedupe_key, canonical_portal_url,
                              transport_ok, REQUIRE_HTTPS)
from agent.memory_cache import MemoryCache, signature           # noqa: E402
from agent.openai_cascade import execute_model_cascade          # noqa: E402
from agent.schemas import DiscoveredPortal, IntegratedDiscoveryOutput  # noqa: E402
from agent.search_fallback import (build_login_endpoint_query,  # noqa: E402
                                   execute_search_fallback)
from agent.v3_orchestrator import (harvest_portal_legal_links,  # noqa: E402
                                   UniversityRow, GUARDRAIL_TIMEOUT_S,
                                   GUARDRAIL_RETRY_S)

logger = logging.getLogger("genie.lookup")

_DIRECT_LOGIN_HINT_RE = re.compile(
    r"(?:login|logon|sign[\s_-]?in|auth(?:enticate|entication)?|sso|"
    r"moodle|canvas|blackboard|brightspace|lms|manaba|portal|"
    r"(?:^|[./_-])my(?:[./_-]|$))", re.I)

# Independent lookups may run in parallel, but each one starts a heavyweight
# Crawl4AI/Chromium session.  Serialising this one fragile stage avoids browser
# collisions while model calls, web search, and endpoint verification continue
# concurrently after a crawl completes.
_browser_sems: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, asyncio.Semaphore
] = weakref.WeakKeyDictionary()


def _get_browser_sem() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    semaphore = _browser_sems.get(loop)
    if semaphore is None:
        limit = max(1, int(os.getenv("GENIE_LOOKUP_BROWSER_CONCURRENCY", "1")))
        semaphore = asyncio.Semaphore(limit)
        _browser_sems[loop] = semaphore
    return semaphore


def _clean_domain(raw: str) -> str:
    """Accept 'buet.ac.bd', 'https://buet.ac.bd/x', or 'www.buet.ac.bd/'."""
    d = (raw or "").strip()
    if "://" in d:
        d = urlsplit(d).netloc
    d = d.split("/")[0].split("@")[-1].split(":")[0].strip().strip(".")
    return d[4:] if d.startswith("www.") else d


def _crawl_target(raw: str) -> str:
    """Preserve the operator-supplied hostname for the first crawl.

    Many Japanese university bare domains have no DNS record while their
    published ``www`` host works.  Domain normalisation is still useful for
    ownership comparisons and search, but it must not rewrite the fetch URL.
    """
    value = (raw or "").strip()
    if not value:
        return value
    if "://" in value:
        return value
    return "https://" + value.lstrip("/")


def _is_university_host(url: str, official_domain: str) -> bool:
    """True for the official host or one of its institutional subdomains."""
    host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
    root = _clean_domain(official_domain).lower()
    return bool(host and root and (host == root or host.endswith("." + root)))


def _direct_login_candidates(
    candidates: list[dict[str, Any]], official_domain: str,
) -> list[DiscoveredPortal]:
    """Preserve explicit university login endpoints before verification.

    This does not bypass validation: the URL still needs a live same-host HTTPS
    endpoint, credentials UI and student/academic evidence below.
    """
    found: list[DiscoveredPortal] = []
    seen: set[str] = set()
    for candidate in candidates:
        url = str(candidate.get("url") or "").strip()
        text = str(candidate.get("text") or "")
        if not url or url in seen or not _is_university_host(url, official_domain):
            continue
        parts = urlsplit(url)
        if not _DIRECT_LOGIN_HINT_RE.search(
                f"{parts.netloc}{parts.path}?{parts.query} {text}"):
            continue
        seen.add(url)
        found.append(DiscoveredPortal(
            category="General Student Login",
            exact_url=url,
            portal_system_name=text[:120] or "University login",
        ))
    return found


def _merge_portals(*groups: list[DiscoveredPortal]) -> list[DiscoveredPortal]:
    merged: list[DiscoveredPortal] = []
    seen: set[str] = set()
    for group in groups:
        for portal in group:
            key = portal.exact_url.rstrip("/").lower()
            if key and key not in seen:
                seen.add(key)
                merged.append(portal)
    return merged


def _portal_application_key(url: str) -> str:
    """Treat one hostname as one deployed portal application.

    A single LMS commonly advertises `/login`, `/saml/login`, `/xlogin`, and
    language-specific OAuth routes.  They are authentication mechanisms for
    the same application, not separate portals, and should occupy one sheet
    row.  Third-party multi-tenant hosts are processed per university lookup,
    so this consolidation cannot merge different institutions.
    """
    return (urlsplit(url).hostname or "").lower()


def _portal_entry_rank(url: str) -> tuple[int, int]:
    """Prefer the simplest reusable entry URL for one portal application."""
    parsed = urlsplit(url)
    path = (parsed.path or "/").lower().rstrip("/")
    score = 0
    if re.search(r"/(?:account/)?login(?:\.[a-z0-9]+)?$", path):
        score += 80
    if path.endswith("/portal/login"):
        score += 30
    if any(part in path for part in ("/saml/", "/oauth/", "/cas/", "/xlogin")):
        score -= 25
    if parsed.query:
        score -= 15
    return score, -len(url)


async def find_portals(
    domain: str,
    *,
    name: str = "",
    country: str = "",
    portals_only: bool = False,
    page_timeout_ms: int = 30_000,
    use_search_fallback: bool = True,
    _targeted_endpoint_rescue: bool = False,
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
    supplied_domain = domain
    crawl_target = _crawl_target(supplied_domain)
    domain = _clean_domain(supplied_domain)
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

    # 1. crawl / targeted search ----------------------------------------
    if _targeted_endpoint_rescue:
        stats["search_reason"] = "all initial portal candidates failed verification"
        hits = await execute_search_fallback(
            row.name, domain,
            query=build_login_endpoint_query(row.name, domain, country), country=country,
        )
        stats["used_search"] = True
        candidates = lkm.filter_and_rank_links(hits, country=country) if hits else []
        if not candidates and hits:
            candidates = hits
    else:
        async def _crawl() -> list[dict[str, str]]:
            async with _get_browser_sem():
                return await extract_targeted_university_links(
                    crawl_target, page_timeout_ms=page_timeout_ms,
                    country_hint=country)

        # Crawl4AI and OpenAI search are independent evidence sources. Running
        # them together prevents a slow/dead homepage from serially delaying
        # the rescue path and makes vendor-hosted SSO discoverable even when it
        # is not linked from the public homepage.
        crawl_task = asyncio.create_task(_crawl())
        # Ask for direct authentication endpoints from the start.  The broad
        # query often spent its six-result budget on rankings and IT guides;
        # the endpoint query is both more deterministic and better at finding
        # multiple systems (SIS + LMS + library) for one university.
        search_task = (asyncio.create_task(execute_search_fallback(
            row.name, domain,
            query=build_login_endpoint_query(row.name, domain, country),
            max_results=8, country=country)) if use_search_fallback else None)
        if search_task is not None:
            links, hits = await asyncio.gather(crawl_task, search_task)
            stats["used_search"] = True
            stats["search_reason"] = "parallel independent discovery"
            # Search commonly returns an official IT guide rather than the
            # portal linked by that guide. Follow only a few university-owned
            # evidence pages and merge the destinations as candidates.
            official_evidence_pages = [
                str(hit.get("url") or "") for hit in hits
                if _is_university_host(str(hit.get("url") or ""), domain)
            ]
            if official_evidence_pages:
                evidence_links = await extract_links_from_evidence_pages(
                    official_evidence_pages, country_hint=country)
                hits = list(hits) + evidence_links
        else:
            links, hits = await crawl_task, []
        stats["crawled_links"] = len(links or [])
        crawl_candidates = lkm.filter_and_rank_links(
            links, country=country) if links else []
        search_candidates = lkm.filter_and_rank_links(
            hits, country=country) if hits else []
        candidates = []
        seen_candidates: set[str] = set()
        for candidate in crawl_candidates + search_candidates:
            key = str(candidate.get("url") or "").rstrip("/").lower()
            if key and key not in seen_candidates:
                seen_candidates.add(key)
                candidates.append(candidate)

    # 2. broad search rescue --------------------------------------------
    # Crawl4AI deliberately returns [] on an unrenderable/blocked page so one
    # site cannot abort a batch.  Treat that outcome as a reason to search the
    # live web, not as evidence that the university has no portal.
    if (not candidates and use_search_fallback and not _targeted_endpoint_rescue
            and not stats["used_search"]):
        stats["search_reason"] = "crawl yielded no usable portal candidates"
        logger.info("lookup: %s — %s; invoking OpenAI web-search rescue",
                    domain, stats["search_reason"])
        hits = await execute_search_fallback(row.name, domain, country=country)
        stats["used_search"] = True
        if hits:
            candidates = lkm.filter_and_rank_links(hits, country=country) or hits
    stats["candidates"] = len(candidates)
    if not candidates:
        return {"domain": domain, "name": row.name, "portals": [],
                "stats": stats, "error": "no candidates found"}

    # 3. extract ---------------------------------------------------------
    result: IntegratedDiscoveryOutput = await asyncio.to_thread(
        execute_model_cascade, "lookup", row.name, domain, candidates, country=country)
    # A homepage can contain links but none that identify a student portal.
    # Give web search one independent chance in this case too.  Search results
    # are still only candidates: they go through the same classifier and the
    # strict endpoint/login-surface checks below.
    if (not result.discovered_portals and use_search_fallback
            and not stats["used_search"]):
        stats["search_reason"] = "website candidates identified no portal"
        logger.info("lookup: %s — %s; invoking OpenAI web-search rescue",
                    domain, stats["search_reason"])
        hits = await execute_search_fallback(row.name, domain, country=country)
        stats["used_search"] = True
        if hits:
            search_candidates = lkm.filter_and_rank_links(hits, country=country) or hits
            stats["candidates"] += len(search_candidates)
            candidates += search_candidates
            result = await asyncio.to_thread(
                execute_model_cascade, "lookup", row.name, domain,
                search_candidates, country=country)

    # A model may be appropriately conservative about unfamiliar languages,
    # but it must not erase a direct, university-owned login endpoint already
    # observed in the crawl/search candidates.  The endpoint still passes the
    # same network, form and student-audience checks below.
    deterministic = _direct_login_candidates(candidates, domain)
    result.discovered_portals = _merge_portals(
        list(result.discovered_portals), deterministic,
    )

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
    stats["endpoint_probe_failures"] = sum(1 for c in checks if not c[0])

    # --- transport + rendered evidence -----------------------------------
    # A failed HEAD/GET is no longer a final verdict. Some real portals return
    # an anomalous 403/404 to aiohttp but render a student login in Chromium.
    # Likewise, an official university entry point may legitimately redirect
    # to a tenant-bound IdP. The hybrid verifier inspects that rendered DOM and
    # returns the stable published URL rather than a SAML/session transaction.
    live: list[Any] = []
    insecure: list[str] = []
    eligible: list[tuple[Any, Any]] = []
    for p, c in zip(allowed_portals, checks):
        if transport_ok(p.exact_url):
            eligible.append((p, c))
        else:
            insecure.append(p.exact_url)
    if insecure:
        logger.info("lookup: %s — %d portal(s) dropped, final URL not https: %s",
                    domain, len(insecure), ", ".join(insecure[:3]))
    stats["dropped_insecure_http"] = len(insecure)
    stats["require_https"] = REQUIRE_HTTPS

    # Accept a real credential form OR an actionable rendered Login/SSO control.
    # URL vocabulary is not an acceptance/rejection rule here.
    auth_checks = await asyncio.gather(*(
        verify_student_login_surface_hybrid(
            p.exact_url, country_hint=country, official_domain=domain)
        for p, _ in eligible))
    verified_keys: set[str] = set()
    verified_apps: dict[str, tuple[Any, Any]] = {}
    for (portal, check), (accepted, note, stable_url) in zip(eligible, auth_checks):
        if accepted:
            portal.exact_url = canonical_portal_url(stable_url)
            output_allowed, output_reason = portal_url_allowed(portal.exact_url)
            if not output_allowed:
                stats["rejected_non_login_pages"] += 1
                logger.info("lookup: %s — dropped final %s (%s)",
                            domain, portal.exact_url, output_reason)
                continue
            key = portal_dedupe_key(portal.exact_url)
            if key in verified_keys:
                stats["duplicate_verified_portals"] = (
                    stats.get("duplicate_verified_portals", 0) + 1)
                continue
            verified_keys.add(key)
            app_key = _portal_application_key(portal.exact_url)
            previous = verified_apps.get(app_key)
            if previous is None:
                verified_apps[app_key] = (portal, check)
            elif _portal_entry_rank(portal.exact_url) > _portal_entry_rank(
                    previous[0].exact_url):
                verified_apps[app_key] = (portal, check)
                stats["duplicate_verified_portals"] = (
                    stats.get("duplicate_verified_portals", 0) + 1)
            else:
                stats["duplicate_verified_portals"] = (
                    stats.get("duplicate_verified_portals", 0) + 1)
        else:
            stats["rejected_non_login_pages"] += 1
            if ("non-student audience only" in note
                    or "no student or academic audience evidence" in note):
                stats["rejected_non_student_portals"] += 1
            logger.info("lookup: %s — dropped %s (not a verified login page: %s)",
                        domain, portal.exact_url, note)
    live = list(verified_apps.values())
    stats["dead_portals"] = len(allowed_portals) - len(live)

    # Only now do we know that every homepage/model candidate was unsuitable:
    # dead, external-IdP-only, not a login form, or not student-facing.  Make
    # one independent, subdomain-aware search attempt rather than calling a
    # generic web search for every successful university.
    if not live and use_search_fallback and not _targeted_endpoint_rescue:
        logger.info("lookup: %s — all candidates failed verification; "
                    "invoking targeted login endpoint rescue", domain)
        return await find_portals(
            domain, name=name, country=country, portals_only=portals_only,
            page_timeout_ms=page_timeout_ms, use_search_fallback=use_search_fallback,
            _targeted_endpoint_rescue=True,
        )

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

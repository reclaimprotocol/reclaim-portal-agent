"""Genie-V3 · Master pipeline — the unified, non-blocking orchestrator.

Joins the V3 components end to end, one coroutine per university:

    Sheets (bulk read)
        -> crawler.extract_raw_university_links   rendered Chromium, proxy-aware
        -> filters.LocalKnowledgeMatrixFilter     KB-driven token shield
        -> openrouter_cascade.execute_model_cascade   waterfall prompt + failover
        -> guardrails.verify_portal_endpoint       Layer 3: is the endpoint live?
        -> verified_compliance.csv | missing_tnc_portals.csv   (asyncio.Lock)
        -> tnc_memory.json                         Layer 5: vendor -> legal URLs

The legacy engine (agent/magic.py, agent/stages/*) is untouched: `genie/` runs
in production against it, and 3,114 August4000 rows still need its T&C pass.

STAYING NON-BLOCKING
--------------------
Two of our dependencies are synchronous and would stall the whole event loop if
awaited naively:

  * `sheets_client` is googleapiclient (sync sockets);
  * `openrouter_cascade.execute_model_cascade` uses the sync OpenAI client.

Both are dispatched with `asyncio.to_thread`, so a slow Sheets call or a 90 s
model timeout parks one worker instead of freezing all twenty.

CONCURRENCY — TWO SEPARATE LIMITS, DELIBERATELY
-----------------------------------------------
`asyncio.Semaphore(20)` is the row turnstile, as specified. But a row's first
act is to launch a headless Chromium, and on this 8 GB box V2 batch runs were
stable at ~10 worker processes and began swapping past that. Twenty concurrent
browsers would thrash, and swap makes everything look like a network timeout —
the most misleading failure mode we have.

So browser work passes through a SECOND, smaller semaphore
(`GENIE_BROWSER_CONCURRENCY`, default 6). Twenty rows stay in flight; at most
six are rendering at any moment, while the rest sit in the model call or the
guardrail check, which are cheap. Raise it on a bigger machine.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import os
import random
import re
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try:
    from dotenv import load_dotenv; load_dotenv(ROOT / ".env")
except Exception:  # noqa: BLE001
    pass

from agent import proxy as _proxy                                   # noqa: E402
from agent.crawler import extract_raw_university_links, is_blocked  # noqa: E402
from agent.filters import LocalKnowledgeMatrixFilter                # noqa: E402
from agent.guardrails import verify_portal_endpoint_detailed        # noqa: E402
from agent.memory_cache import MemoryCache, signature               # noqa: E402
from agent.openrouter_cascade import execute_model_cascade          # noqa: E402
from agent.schemas import IntegratedDiscoveryOutput                 # noqa: E402
from agent.search_fallback import execute_search_fallback           # noqa: E402

logger = logging.getLogger("genie.v3")

LOG_FILE = Path(os.getenv("GENIE_LOG_FILE", ROOT / "agent_run.log"))


def setup_logging(level: int = logging.INFO, log_file: Path = LOG_FILE) -> None:
    """Timestamped text to the console AND an append-only agent_run.log.

    Configured on the `genie` parent logger so every component — crawler,
    filters, cascade, guardrails, memory, search — lands in the same stream
    without each one configuring itself.

    ON CONCURRENCY: `logging.Handler.emit` takes a re-entrant lock and writes
    one formatted record per call, so lines from 20 concurrent workers never
    interleave mid-string. That guarantee is exactly why the cascade's `print()`
    calls were converted to logging — print has no such lock, and those calls
    ran inside `asyncio.to_thread` worker threads where interleaving is real.
    """
    class _SingleLine(logging.Formatter):
        """One record == one line, always.

        Upstream messages embed newlines (crawl4ai reports Playwright errors as
        multi-line blocks), which makes a record span several lines and breaks
        grep/awk over the log. Collapsing them is what actually delivers the
        "do not overlap text lines" requirement — the handler lock already
        prevents worker interleaving, but it cannot flatten a message that
        arrives with newlines inside it.
        """

        def format(self, record: logging.LogRecord) -> str:
            return super().format(record).replace("\r", " ").replace("\n", " | ")

    fmt = _SingleLine(
        "%(asctime)s %(levelname)-7s [%(name)s] %(message)s", datefmt="%H:%M:%S")
    genie = logging.getLogger("genie")
    genie.setLevel(level)
    genie.handlers.clear()
    genie.propagate = False

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    genie.addHandler(console)

    log_file.parent.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(log_file, mode="a", encoding="utf-8")   # append-only
    fh.setFormatter(_SingleLine(
        "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"))       # full date on disk
    genie.addHandler(fh)

    # Third-party chatter would drown the signal in the same file.
    for noisy in ("aiohttp", "httpx", "openai", "urllib3", "asyncio",
                  "crawl4ai", "googleapiclient", "instructor"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logger.info("logging -> console + %s", log_file)

ROW_SEMAPHORE = 20
BROWSER_SEMAPHORE = int(os.getenv("GENIE_BROWSER_CONCURRENCY", "6"))

VERIFIED_CSV = ROOT / "verified_compliance.csv"
MISSING_CSV = ROOT / "missing_tnc_portals.csv"
TNC_MEMORY = ROOT / "tnc_memory.json"
DOMAIN_HISTORY = ROOT / "domain_history.json"

# Deliberately narrow, per the Layer-4 spec. The rich per-portal detail
# (category, confidence, waterfall stage, privacy URL, http status) is NOT lost
# — it is written in full to domain_history.json, which is also what the
# pre-crawl shield reads back. These CSVs are the delivery artefact, not the
# system of record.
VERIFIED_COLUMNS = ["orgId", "portal_url", "tnc_url"]
MISSING_COLUMNS = ["orgId", "portal_url"]

#: One lock per artefact. A single global lock would serialise every worker on
#: every write; these are held for microseconds each.
_csv_lock = asyncio.Lock()
_mem_lock = asyncio.Lock()
_hist_lock = asyncio.Lock()

_MULTI_LABEL_TLDS = {
    "ac.in", "co.in", "edu.in", "org.in", "net.in", "gov.in", "ac.uk", "co.uk",
    "com.br", "edu.br", "org.br", "gov.br", "com.au", "edu.au", "com.mx",
    "edu.mx", "com.ar", "edu.ar", "com.co", "edu.co", "ac.id", "sch.id",
    "edu.ph", "com.ph", "ac.lk", "edu.lk", "ac.bd", "edu.bd", "edu.pk",
    "edu.ng", "ac.ke", "ac.za", "edu.my", "edu.vn", "edu.eg", "edu.sa",
}


def registrable_root(host_or_url: str) -> str:
    """Vendor-level signature for a host: `a.b.jacad.com.br` -> `jacad.com.br`.

    This is the key `tnc_memory.json` is stored under, so that one lookup of a
    SaaS vendor's legal pages serves every university on that platform — 542
    institutions share samarth.edu.in alone.
    """
    host = host_or_url
    if "://" in host:
        host = urlsplit(host).netloc
    host = (host or "").lower().split("@")[-1].split(":")[0].strip(".")
    if host.startswith("www."):
        host = host[4:]
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    if ".".join(parts[-2:]) in _MULTI_LABEL_TLDS:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


@dataclass
class UniversityRow:
    row: int
    org_id: str
    name: str
    domain: str
    country: str = ""


@dataclass
class RowOutcome:
    org_id: str
    ok: bool
    portals: int = 0
    verified: int = 0
    missing: int = 0
    seconds: float = 0.0
    note: str = ""
    cached: bool = False
    stages: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
#  1. Bulk sheet read                                                          #
# --------------------------------------------------------------------------- #
def _fetch_rows_sync(sheet_id: str, tab: str, start_row: int, count: int) -> list[UniversityRow]:
    """ONE ranged read for the whole block, then resolve columns by HEADER.

    Two hard-won V2 rules: never poll row-by-row (that is what exhausts the
    Sheets quota), and never hardcode column letters — the tab layout has been
    reordered several times and letter-indexed writers silently put verdicts in
    the wrong column.
    """
    from agent.config import load_config
    from agent.sheets_client import SheetsClient

    sc = SheetsClient.from_config(load_config())
    sc.sheet_id = sheet_id
    svc = sc._service.spreadsheets()

    header = (svc.values().get(spreadsheetId=sheet_id, range=f"'{tab}'!A1:Z1")
              .execute().get("values", [[]]) or [[]])[0]
    idx = {str(h).strip().lower(): i for i, h in enumerate(header)}

    def col(*names: str) -> int | None:
        for n in names:
            if n.lower() in idx:
                return idx[n.lower()]
        for n in names:
            for h, i in idx.items():
                if n.lower() in h:
                    return i
        return None

    c_id = col("org id", "organization id", "orgid")
    c_nm = col("org name", "organization name", "university name", "name")
    c_dm = col("email domains", "website domain", "domains", "website")
    c_cy = col("country")
    if c_id is None or c_dm is None:
        raise SystemExit(f"'{tab}': could not resolve Org ID / Email Domains from header {header}")

    last = start_row + count - 1
    values = (svc.values().get(spreadsheetId=sheet_id, range=f"'{tab}'!A{start_row}:Z{last}")
              .execute().get("values", []))
    width = max((len(r) for r in values), default=0)

    out: list[UniversityRow] = []
    for off, raw in enumerate(values):
        r = (list(raw) + [""] * width)[:width]
        oid = str(r[c_id]).strip() if c_id < len(r) else ""
        dom = str(r[c_dm]).strip() if c_dm < len(r) else ""
        if not oid or not dom:
            continue
        primary = dom.replace(",", " ").split()[0].strip() if dom.strip() else ""
        if not primary:
            continue
        out.append(UniversityRow(
            row=start_row + off,
            org_id=oid,
            name=(str(r[c_nm]).strip() if c_nm is not None and c_nm < len(r) else ""),
            domain=primary,
            country=(str(r[c_cy]).strip() if c_cy is not None and c_cy < len(r) else ""),
        ))
    return out


async def fetch_rows(sheet_id: str, tab: str, start_row: int, count: int) -> list[UniversityRow]:
    return await asyncio.to_thread(_fetch_rows_sync, sheet_id, tab, start_row, count)


# --------------------------------------------------------------------------- #
#  2. Layer 3 guardrail — is the endpoint actually live?                       #
# --------------------------------------------------------------------------- #
#: Layer 3 budget. 5s catches the common case cheaply; the 20s retry fires ONLY
#: on a timeout, because slow is not dead — V2 re-checks at a longer timeout
#: flipped 26 of 52 "unreachable" portals to working, and every one of those
#: would otherwise have been dropped from a delivery.
GUARDRAIL_TIMEOUT_S = int(os.getenv("GENIE_GUARDRAIL_TIMEOUT", "5"))
GUARDRAIL_RETRY_S = int(os.getenv("GENIE_GUARDRAIL_RETRY", "20"))


# --------------------------------------------------------------------------- #
#  3. Persistence — CSV + Layer-5 memory, each behind its own lock             #
# --------------------------------------------------------------------------- #
def _atomic_json_write(path: Path, data: dict[str, Any]) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh, indent=1, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


async def append_rows(path: Path, columns: list[str],
                      rows: list[dict[str, Any]]) -> None:
    """Append under `_csv_lock`, writing the header only on first creation.

    If the file exists with a DIFFERENT header (an earlier run used the wide
    schema), it is rotated aside rather than appended to — silently writing
    3-column rows under a 14-column header produces a file that parses without
    error and is wrong in every row.
    """
    if not rows:
        return
    async with _csv_lock:
        new = not path.exists() or path.stat().st_size == 0
        if not new:
            with path.open("r", encoding="utf-8") as fh:
                existing = (fh.readline().strip().split(",") if fh else [])
            if existing != columns:
                bak = path.with_suffix(
                    f".{datetime.now(timezone.utc):%Y%m%dT%H%M%S}.bak.csv")
                path.rename(bak)
                logger.warning("v3: %s had an old header — rotated to %s",
                               path.name, bak.name)
                new = True
        with path.open("a", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
            if new:
                w.writeheader()
            for r in rows:
                w.writerow(r)


async def update_tnc_memory(entries: list[dict[str, Any]]) -> None:
    """Cache legal URLs under the portal's DOMAIN SIGNATURE (registrable root).

    Keyed by vendor root rather than university, because that is what makes it
    reusable: learn jacad.com.br's terms once and every one of its tenants is
    answered without another lookup.

    Only Stage 1-3 hits are stored. Stage 4 is the apex-root rescue — it may not
    mention the portal at all, and caching it would propagate our weakest guess
    to every future university on that domain.
    """
    if not entries:
        return
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    async with _mem_lock:
        try:
            mem = json.loads(TNC_MEMORY.read_text() or "{}")
        except Exception:  # noqa: BLE001
            mem = {}
        for e in entries:
            stage = e.get("waterfall_discovery_stage") or ""
            if stage.startswith("Stage 4") or stage == "None Found":
                continue
            if not (e.get("tnc_url") or e.get("privacy_policy_url")):
                continue
            sig = registrable_root(e["exact_url"])
            cur = mem.get(sig) or {"domain_signature": sig, "first_seen": now,
                                   "hits": 0, "orgs": []}
            cur.update({
                "tnc_url": e.get("tnc_url") or cur.get("tnc_url"),
                "privacy_policy_url": e.get("privacy_policy_url") or cur.get("privacy_policy_url"),
                "waterfall_discovery_stage": stage,
                "last_seen": now,
                "hits": int(cur.get("hits", 0)) + 1,
            })
            orgs = list(dict.fromkeys([*cur.get("orgs", []), str(e.get("org_id", ""))]))
            cur["orgs"] = [o for o in orgs if o][:50]
            mem[sig] = cur
        _atomic_json_write(TNC_MEMORY, mem)


async def record_history(row: UniversityRow, result: IntegratedDiscoveryOutput,
                         verified: int) -> None:
    """Successful university -> portal mapping, doubling as the resume record."""
    async with _hist_lock:
        try:
            hist = json.loads(DOMAIN_HISTORY.read_text() or "{}")
        except Exception:  # noqa: BLE001
            hist = {}
        hist[row.org_id] = {
            "org_id": row.org_id,
            "university_name": row.name,
            "official_domain": row.domain,
            "sheet_row": row.row,
            "portals": [
                {"url": p.exact_url, "category": p.category,
                 "system": p.portal_system_name,
                 "confidence": p.confidence_score,
                 "tnc_url": p.compliance_metrics.tnc_url,
                 "privacy_policy_url": p.compliance_metrics.privacy_policy_url,
                 "stage": p.compliance_metrics.waterfall_discovery_stage}
                for p in result.discovered_portals
            ],
            "verified_live": verified,
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        _atomic_json_write(DOMAIN_HISTORY, hist)


# --------------------------------------------------------------------------- #
#  Token pacing + rate-limit resilience                                        #
# --------------------------------------------------------------------------- #
PACING_MIN_S, PACING_MAX_S = 0.5, 1.5
CASCADE_MAX_ATTEMPTS = 3

#: A 429 never reaches us as an exception — `execute_model_cascade` catches every
#: tier failure and escalates internally, returning an empty baseline once all
#: three are spent. So rate limiting is detected by inspecting the trace, not by
#: catching. Miss this and the retry loop would be dead code that never fires.
_RATE_LIMIT_RE = re.compile(r"429|rate.?limit|too many requests|quota", re.I)


def _rate_limited(trace: dict[str, Any]) -> bool:
    fails = trace.get("failures") or []
    return bool(fails) and any(_RATE_LIMIT_RE.search(f.get("error", "")) for f in fails)


async def cascade_with_pacing(
    row: "UniversityRow", candidates: list[dict[str, Any]],
) -> tuple[IntegratedDiscoveryOutput, dict[str, Any]]:
    """Run the cascade with TPM pacing and exponential backoff on 429s.

    The pacing sleep is applied BEFORE every attempt, including retries: with 20
    workers hitting OpenRouter the instant their crawls finish, requests arrive
    in bursts that trip per-minute limits even when the average rate is fine.
    Jittering each worker smooths the burst without lowering throughput
    meaningfully.
    """
    result: IntegratedDiscoveryOutput | None = None
    trace: dict[str, Any] = {}
    for attempt in range(CASCADE_MAX_ATTEMPTS):
        await asyncio.sleep(random.uniform(PACING_MIN_S, PACING_MAX_S))
        trace = {}
        result = await asyncio.to_thread(
            lambda: execute_model_cascade(row.org_id, row.name, row.domain,
                                          candidates, trace=trace))
        if result.discovered_portals or not _rate_limited(trace):
            break
        if attempt < CASCADE_MAX_ATTEMPTS - 1:
            delay = (2 ** attempt) + random.uniform(0.1, 0.5)
            logger.warning("[RATE LIMIT PACING] org %s — OpenRouter throttled on "
                           "attempt %d/%d; backing off %.2fs",
                           row.org_id, attempt + 1, CASCADE_MAX_ATTEMPTS, delay)
            await asyncio.sleep(delay)
        else:
            logger.error("[RATE LIMIT PACING] org %s — still throttled after %d "
                         "attempts; giving up on this row",
                         row.org_id, CASCADE_MAX_ATTEMPTS)
    return result, trace


# --------------------------------------------------------------------------- #
#  Search rescue                                                               #
# --------------------------------------------------------------------------- #
async def search_rescue(row: "UniversityRow",
                        lkm: LocalKnowledgeMatrixFilter) -> list[dict[str, Any]]:
    """Web search, then the SAME local filter the crawl output goes through.

    Search results carry their own clutter — Wikipedia, Studocu, ranking sites,
    press coverage, PDF prospectuses — and the blacklist that
    strips careers/admissions/alumni from a crawl strips them here too. Handing
    raw hits to the cascade would spend context on exactly the noise the token
    shield exists to remove.

    If filtering leaves nothing, the raw hits are returned rather than an empty
    list: a rescue path that silently cancels itself is worse than a noisy one,
    and the model still applies its own judgement downstream.
    """
    hits = await execute_search_fallback(row.name, row.domain)
    if not hits:
        return []
    filtered = lkm.filter_and_rank_links(hits)
    if filtered:
        logger.info("v3: org %s — search %d hit(s) -> %d after filter",
                    row.org_id, len(hits), len(filtered))
        return filtered
    logger.info("v3: org %s — filter dropped all %d search hit(s); "
                "passing raw to the cascade", row.org_id, len(hits))
    return hits


# --------------------------------------------------------------------------- #
#  4. Worker                                                                   #
# --------------------------------------------------------------------------- #
async def process_university(
    row: UniversityRow,
    lkm: LocalKnowledgeMatrixFilter,
    cache: MemoryCache,
    row_sem: asyncio.Semaphore,
    browser_sem: asyncio.Semaphore,
    *,
    page_timeout_ms: int = 30_000,
    dry_run: bool = False,
) -> RowOutcome:
    """Shield -> crawl -> filter -> cascade -> verify -> (search) -> persist."""
    t0 = time.monotonic()
    async with row_sem:
        try:
            # -- 1. PRE-CRAWL SHIELD -------------------------------------
            hit = cache.pre_crawl_check(row.org_id, row.domain)
            if hit:
                logger.info("[CACHE HIT] org %s (%s) — %s: %s — skipping crawl "
                            "and inference", row.org_id, row.domain, hit.kind, hit.reason)
                return RowOutcome(row.org_id, hit.kind == "history",
                                  portals=len(hit.record.get("portals", []) or []),
                                  seconds=round(time.monotonic() - t0, 1),
                                  note=f"shield:{hit.kind}", cached=True)

            # -- 2. CRAWL (RAM-protected) --------------------------------
            async with browser_sem:
                links = await extract_raw_university_links(
                    row.domain, page_timeout_ms=page_timeout_ms,
                    country_hint=row.country)

            if not links:
                blocked = is_blocked(row.domain)
                logger.warning(
                    "[CRAWL INFRASTRUCTURE BLOCK] org %s (%s) — %s",
                    row.org_id, row.domain,
                    f"firewall: {blocked.get('block_type')}" if blocked
                    else "crawler returned 0 links")

            # -- 3. FILTER + cascade -------------------------------------
            candidates = lkm.filter_and_rank_links(links) if links else []
            if not candidates:
                logger.info("[FALLBACK TRIGGER] org %s (%s) — no crawl candidates, "
                            "entering DuckDuckGo search", row.org_id, row.domain)
                candidates = await search_rescue(row, lkm)
                if not candidates:
                    return RowOutcome(row.org_id, False,
                                      seconds=round(time.monotonic() - t0, 1),
                                      note="no candidates (crawl+search empty)")

            result, trace = await cascade_with_pacing(row, candidates)
            if trace.get("escalated"):
                logger.warning("[API CASCADE ESCALATION] org %s — tier 1 dropped it; "
                               "answered by tier %s (%s)", row.org_id,
                               trace.get("tier"), trace.get("model"))
            if not result.discovered_portals:
                return RowOutcome(row.org_id, False,
                                  seconds=round(time.monotonic() - t0, 1),
                                  note="cascade returned no portals")

            # -- 4. GUARDRAIL --------------------------------------------
            checks = await asyncio.gather(*(
                verify_portal_endpoint_detailed(
                    p.exact_url, GUARDRAIL_TIMEOUT_S, country_hint=row.country,
                    retry_timeout_seconds=GUARDRAIL_RETRY_S)
                for p in result.discovered_portals))

            live_portals = [(p, c) for p, c in zip(result.discovered_portals, checks) if c[0]]
            dead = len(result.discovered_portals) - len(live_portals)
            for p, (live, code, note) in zip(result.discovered_portals, checks):
                if not live:
                    logger.warning("[DEAD ENDPOINT DETECTED] org %s — %s "
                                   "(http=%s, %s) — discarded",
                                   row.org_id, p.exact_url, code, note)

            # Every portal failed verification -> search for a replacement and
            # re-run the cascade over what search found.
            if not live_portals:
                logger.warning("[FALLBACK TRIGGER] org %s — all %d portal(s) failed "
                               "verification, routing to DuckDuckGo search",
                               row.org_id, dead)
                hits = await search_rescue(row, lkm)
                if hits:
                    result, _t2 = await cascade_with_pacing(row, hits)
                    checks = await asyncio.gather(*(
                        verify_portal_endpoint_detailed(
                            p.exact_url, GUARDRAIL_TIMEOUT_S, country_hint=row.country,
                            retry_timeout_seconds=GUARDRAIL_RETRY_S)
                        for p in result.discovered_portals))
                    live_portals = [(p, c) for p, c in
                                    zip(result.discovered_portals, checks) if c[0]]
                if not live_portals:
                    return RowOutcome(row.org_id, False, portals=len(result.discovered_portals),
                                      seconds=round(time.monotonic() - t0, 1),
                                      note=f"all portals dead ({dead})")

            # -- 5/6. PERSIST + COMPOUND MEMORY --------------------------
            verified_rows, missing_rows, mem_entries, stages, detail = [], [], [], [], []
            for p, (live, code, _note) in live_portals:
                cm = p.compliance_metrics
                tnc = cm.tnc_url or cm.privacy_policy_url or ""
                # Vendor memory can answer what this run could not.
                if not tnc:
                    cached = cache.legal_for_portal(p.exact_url)
                    if cached:
                        tnc = cached.get("tnc_url") or cached.get("privacy_policy_url") or ""
                        logger.info("v3: org %s — legal injected from memory (%s)",
                                    row.org_id, signature(p.exact_url))
                stages.append(cm.waterfall_discovery_stage)
                if tnc:
                    verified_rows.append({"orgId": result.org_id,
                                          "portal_url": p.exact_url, "tnc_url": tnc})
                    mem_entries.append({"org_id": result.org_id, "exact_url": p.exact_url,
                                        "tnc_url": cm.tnc_url,
                                        "privacy_policy_url": cm.privacy_policy_url,
                                        "waterfall_discovery_stage": cm.waterfall_discovery_stage})
                else:
                    missing_rows.append({"orgId": result.org_id, "portal_url": p.exact_url})
                detail.append({"url": p.exact_url, "category": p.category,
                               "system": p.portal_system_name,
                               "confidence": p.confidence_score,
                               "tnc_url": cm.tnc_url, "privacy_policy_url": cm.privacy_policy_url,
                               "stage": cm.waterfall_discovery_stage, "http_status": code})

            if not dry_run:
                await asyncio.gather(
                    append_rows(VERIFIED_CSV, VERIFIED_COLUMNS, verified_rows),
                    append_rows(MISSING_CSV, MISSING_COLUMNS, missing_rows),
                    cache.remember_legal(mem_entries))
                await cache.remember_org(row.org_id, {
                    "org_id": row.org_id, "university_name": row.name,
                    "official_domain": row.domain, "sheet_row": row.row,
                    "portals": detail, "verified_live": len(verified_rows)})

            return RowOutcome(row.org_id, True, portals=len(live_portals),
                              verified=len(verified_rows), missing=len(missing_rows),
                              seconds=round(time.monotonic() - t0, 1), stages=stages,
                              note=f"{dead} dead" if dead else "")

        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — one bad row must never kill the run
            logger.exception("v3: org %s failed", row.org_id)
            return RowOutcome(row.org_id, False, seconds=round(time.monotonic() - t0, 1),
                              note=f"{type(exc).__name__}: {str(exc)[:140]}")


# --------------------------------------------------------------------------- #
#  5. Run                                                                      #
# --------------------------------------------------------------------------- #
async def run_pipeline(
    sheet_id: str, tab: str, start_row: int, count: int, *,
    resume: bool = True, dry_run: bool = False, page_timeout_ms: int = 30_000,
    row_concurrency: int = ROW_SEMAPHORE, retry_blocked: bool = False,
) -> list[RowOutcome]:
    rows = await fetch_rows(sheet_id, tab, start_row, count)
    if not rows:
        logger.info("v3: nothing to do")
        return []

    lkm = LocalKnowledgeMatrixFilter()
    # `resume=False` disables the shield's history arm only; block-skipping and
    # vendor lookups stay on, since those are correctness aids rather than
    # progress tracking.
    cache = MemoryCache(retry_blocked=retry_blocked)
    if not resume:
        cache.history = {}
    row_sem = asyncio.Semaphore(row_concurrency)
    browser_sem = asyncio.Semaphore(BROWSER_SEMAPHORE)
    logger.info("v3: %d universities | row turnstile %d | browsers %d | dry_run=%s",
                len(rows), row_concurrency, BROWSER_SEMAPHORE, dry_run)

    tasks = [process_university(r, lkm, cache, row_sem, browser_sem,
                                page_timeout_ms=page_timeout_ms, dry_run=dry_run)
             for r in rows]
    outcomes: list[RowOutcome] = []
    for coro in asyncio.as_completed(tasks):
        oc = await coro
        outcomes.append(oc)
        mark = "HIT" if oc.cached else ("OK " if oc.ok else "-- ")
        logger.info("v3: [%d/%d] %s org %s portals=%d verified=%d missing=%d %.1fs %s",
                    len(outcomes), len(rows), mark, oc.org_id, oc.portals,
                    oc.verified, oc.missing, oc.seconds, oc.note)
    logger.info("v3: memory — %s", cache.summary())
    return outcomes


def summarise(outcomes: Sequence[RowOutcome]) -> None:
    ok = [o for o in outcomes if o.ok]
    print("\n=== Genie-V3 run summary ===")
    print(f"  universities processed : {len(outcomes)}")
    print(f"  with >=1 portal        : {len(ok)}")
    print(f"  portals found          : {sum(o.portals for o in outcomes)}")
    print(f"  shield cache hits      : {sum(1 for o in outcomes if o.cached)}")
    print(f"  verified (live + legal): {sum(o.verified for o in outcomes)}  -> {VERIFIED_CSV.name}")
    print(f"  missing T&C / not live : {sum(o.missing for o in outcomes)}  -> {MISSING_CSV.name}")
    if outcomes:
        print(f"  median seconds/org     : {sorted(o.seconds for o in outcomes)[len(outcomes)//2]}")
    from collections import Counter
    st = Counter(s for o in outcomes for s in o.stages)
    if st:
        print("  waterfall stages       :", dict(st))
    fails = Counter(o.note.split(":")[0] for o in outcomes if not o.ok and o.note)
    if fails:
        print("  failure reasons        :", dict(fails))


def main() -> None:
    ap = argparse.ArgumentParser(description="Genie-V3 orchestrator")
    ap.add_argument("--sheet-id", "--sheet", dest="sheet_id",
                    default=os.getenv("GENIE_V3_SHEET",
                                      "1hDMn93A1xjXVUoK7H9PsNGjpVzZtLc8YXA3_Pfb3Bto"),
                    help="Google Sheet ID to read university rows from")
    ap.add_argument("--tab", default=os.getenv("GENIE_V3_TAB", "all orgs"))
    ap.add_argument("--start-row", type=int, default=2)
    ap.add_argument("--count", type=int, default=20)
    ap.add_argument("--concurrency", type=int, default=ROW_SEMAPHORE)
    ap.add_argument("--page-timeout-ms", type=int, default=30_000)
    ap.add_argument("--no-resume", action="store_true",
                    help="ignore domain_history so already-done orgs re-run")
    ap.add_argument("--retry-blocked", action="store_true",
                    help="re-attempt hosts recorded in infrastructure_block.json")
    ap.add_argument("--dry-run", action="store_true", help="run everything, write nothing")
    ap.add_argument("--verbose", action="store_true", help="DEBUG-level logging")
    ap.add_argument("--log-file", default=str(LOG_FILE))
    a = ap.parse_args()

    setup_logging(logging.DEBUG if a.verbose else logging.INFO)
    outcomes = asyncio.run(run_pipeline(
        a.sheet_id, a.tab, a.start_row, a.count,
        resume=not a.no_resume, dry_run=a.dry_run, retry_blocked=a.retry_blocked,
        page_timeout_ms=a.page_timeout_ms, row_concurrency=a.concurrency,
    ))
    summarise(outcomes)


if __name__ == "__main__":
    main()

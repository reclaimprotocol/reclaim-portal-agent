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
from typing import Any, Iterable, Sequence
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
from agent.graph_matcher import (GraphComplianceMatcher,            # noqa: E402
                                 DISTANCE_NATIVE_CRAWL, DISTANCE_SEARCH_FALLBACK)
from agent.guardrails import verify_portal_endpoint_detailed        # noqa: E402
from agent.memory_cache import MemoryCache, signature               # noqa: E402
from agent.openrouter_cascade import execute_model_cascade          # noqa: E402
from agent.schemas import IntegratedDiscoveryOutput                 # noqa: E402
from agent.search_fallback import execute_search_fallback           # noqa: E402

logger = logging.getLogger("genie.v3")




def setup_logging(level: int = logging.INFO, log_file: Path | None = None) -> None:
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

    log_file = Path(log_file or os.getenv("GENIE_LOG_FILE") or LOG_FILE_DEFAULT)
    os.makedirs(log_file.parent, exist_ok=True)

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

# --------------------------------------------------------------------------- #
#  Output paths — one date stamp per RUN                                       #
# --------------------------------------------------------------------------- #
#: Computed exactly once, at import. Every parallel task therefore writes to the
#: SAME three files: calling strftime per task would let a run that starts at
#: 23:59 silently split its results across two dates mid-flight.
CURRENT_DATE = datetime.now().strftime("%Y-%m-%d")

#: Anchored to the repo root, not to os.getcwd(). A relative "output" would put
#: the directory wherever the process happened to be launched from — fine for
#: `python -m agent.v3_orchestrator` at the root, silently wrong under cron, a
#: systemd unit, or an IDE with a different working directory.
OUTPUT_DIR = ROOT / "output"

VERIFIED_CSV = OUTPUT_DIR / f"verified_compliance_{CURRENT_DATE}.csv"
MISSING_CSV = OUTPUT_DIR / f"missing_tnc_portals_{CURRENT_DATE}.csv"
LOG_FILE_DEFAULT = OUTPUT_DIR / f"agent_run_{CURRENT_DATE}.log"

#: Memory files stay at the REPOSITORY ROOT: they are tracked core assets that
#: compound across runs, not per-run output. Date-stamping them would reset the
#: agent's learning every midnight, and `output/` is gitignored so they would
#: also stop being versioned.
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
def _fetch_rows_sync(sheet_id: str, tab: str, start_row: int, count: int,
                     only_org_ids: set[str] | None = None) -> list[UniversityRow]:
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

    # With an explicit org list the rows are scattered, so scan a wide window
    # and filter — still ONE ranged read, which is what protects the quota.
    last = 100_000 if only_org_ids else (start_row + count - 1)
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
        if only_org_ids is not None and oid not in only_org_ids:
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


async def fetch_rows(sheet_id: str, tab: str, start_row: int, count: int,
                     only_org_ids: set[str] | None = None) -> list[UniversityRow]:
    return await asyncio.to_thread(_fetch_rows_sync, sheet_id, tab, start_row,
                                   count, only_org_ids)


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
#  Dynamic linguistic co-learning                                              #
# --------------------------------------------------------------------------- #
KB_PATH = ROOT / "agent_knowledge_base.json"
_kb_lock = asyncio.Lock()

#: Phrases shorter than this are too generic to be a useful signal ("cs", "pp")
#: and would fire tier 1 on unrelated links across every future run.
_MIN_LEARNED_LEN = 4


async def learn_native_keywords(words: Iterable[str],
                                matcher: GraphComplianceMatcher) -> list[str]:
    """Persist newly seen native legal phrases into agent_knowledge_base.json.

    A language is translated by the model ONCE. After that the phrase lives in
    the knowledge base and every later run scores it locally by regex, with no
    model call — which is the point: the agent widens its own dictionary as it
    meets new nations.

    The matcher is updated in the same breath, so the other nineteen workers in
    THIS run benefit immediately rather than only the next run.
    """
    cand = {w.strip() for w in words
            if w and len(w.strip()) >= _MIN_LEARNED_LEN}
    if not cand:
        return []
    async with _kb_lock:
        try:
            kb = json.loads(KB_PATH.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("v3: cannot read knowledge base (%s) — skipping learn",
                           type(exc).__name__)
            return []
        learned = kb.setdefault("learned_legal_keywords", [])
        have = {str(x).strip().lower() for x in learned}
        fresh = [w for w in sorted(cand) if w.lower() not in have]
        if not fresh:
            return []
        learned.extend(fresh)
        kb.setdefault("counts", {})["learned_legal_keywords"] = len(learned)
        _atomic_json_write(KB_PATH, kb)
    added = matcher.add_learned_keywords(fresh)
    logger.info("[LINGUISTIC CO-LEARNING] +%d new native keyword(s): %s "
                "(dictionary now %d)", added, ", ".join(fresh[:4]),
                len(matcher.learned_keywords))
    return fresh


# --------------------------------------------------------------------------- #
#  Two-Step Portal-Level Crawl                                                 #
# --------------------------------------------------------------------------- #
#: Shorter than the 30s homepage budget — a login page is small, and unlike a
#: marketing homepage there is nothing worth waiting for.
PORTAL_CRAWL_TIMEOUT_MS = int(os.getenv("GENIE_PORTAL_CRAWL_TIMEOUT_MS", "20000"))

#: Moodle publishes its data-privacy summary at a fixed path under the install
#: root. Probing it is one request instead of a browser render.
MOODLE_LEGAL_PATH = "/admin/tool/dataprivacy/summary.php"
_MOODLE_LOGIN_TAIL = re.compile(r"/login/index\.php.*$|/login/?$", re.I)
_MOODLE_HINT = re.compile(r"moodle|/login/index\.php|/course/|/my/?$|\bava\b|aula.?virtual", re.I)


def _moodle_wwwroot(portal_url: str) -> str:
    """Moodle install root for a login URL.

    NOT simply scheme+host: Moodle is frequently installed under a subpath, and
    the T&C sits under THAT root. Observed in the sheet —
        https://host.unilus.app/plagiarismcheck/login/index.php
        https://host.unilus.app/plagiarismcheck/admin/tool/dataprivacy/summary.php
    Appending the legal path to the bare host would 404 on every such install.
    """
    s = urlsplit(portal_url)
    path = _MOODLE_LOGIN_TAIL.sub("", s.path or "")
    return f"{s.scheme or 'https'}://{s.netloc}{path.rstrip('/')}"


def _is_moodle(portal: Any) -> bool:
    blob = (f"{getattr(portal, 'portal_system_name', '')} "
            f"{getattr(portal, 'category', '')} {getattr(portal, 'exact_url', '')}")
    return bool(_MOODLE_HINT.search(blob))


async def harvest_portal_legal_links(
    row: UniversityRow,
    live_portals: list,
    lkm: LocalKnowledgeMatrixFilter,
    cache: MemoryCache,
    browser_sem: asyncio.Semaphore,
) -> list[dict[str, str]]:
    """Step two: fetch each LIVE portal and return the legal links published on it.

    The homepage crawl can only surface legal pages linked from the university's
    front page. Measured over 453 portal/T&C pairs in the August4000 sheet, that
    is 45% of them — 42% sit on the PORTAL's own host and are structurally
    unreachable without this step. That gap, not the matcher, is why T&C recall
    was stuck at 30%.

    Anything found here scores S_domain = 1.0 (exact host) in the graph, so it
    clears the 0.40 gate on domain structure alone.
    """
    found: dict[str, dict[str, str]] = {}
    for portal, _check in live_portals:
        purl = (getattr(portal, "exact_url", "") or "").strip()
        if not purl:
            continue

        # Vendor already known -> no network at all.
        if cache.legal_for_portal(purl):
            logger.info("v3: org %s — portal harvest skipped, vendor cached (%s)",
                        row.org_id, signature(purl))
            continue

        # --- cheap probe: Moodle ------------------------------------------
        if _is_moodle(portal):
            cand = _moodle_wwwroot(purl) + MOODLE_LEGAL_PATH
            alive, code, _n = await verify_portal_endpoint_detailed(
                cand, GUARDRAIL_TIMEOUT_S, country_hint=row.country)
            # Strict 2xx only. verify_portal_endpoint treats 401/403/429 as
            # "alive" (correct for a login), but a WAF page is not a T&C — the
            # document has to actually be served.
            if alive and 200 <= code < 300:
                found[cand] = {"url": cand, "anchor_text": "Data privacy summary"}
                logger.info("[PORTAL PROBE HIT] org %s — Moodle legal page %s (%s)",
                            row.org_id, cand[:66], code)
                continue
            logger.info("v3: org %s — Moodle probe missed (%s) for %s, crawling",
                        row.org_id, code, purl[:48])

        # --- fallback: render the portal page -----------------------------
        async with browser_sem:
            links = await extract_raw_university_links(
                purl, page_timeout_ms=PORTAL_CRAWL_TIMEOUT_MS,
                country_hint=row.country)
        if not links:
            continue
        # Route through the SAME local matrix the homepage links go through, so
        # blacklist rules apply identically and no new noise path opens up.
        kept = lkm.filter_and_rank_links(links)
        n = 0
        for cand in kept:
            blob = f"{cand.get('url','')} {cand.get('text','')}"
            if lkm.legal_re and lkm.legal_re.search(blob):
                u = cand["url"]
                if u not in found:
                    found[u] = {"url": u, "anchor_text": (cand.get("text") or "")[:160]}
                    n += 1
        logger.info("v3: org %s — portal crawl %s -> %d legal of %d link(s)",
                    row.org_id, purl[:48], n, len(links))
    return list(found.values())


# --------------------------------------------------------------------------- #
#  4. Worker                                                                   #
# --------------------------------------------------------------------------- #
async def process_university(
    row: UniversityRow,
    lkm: LocalKnowledgeMatrixFilter,
    cache: MemoryCache,
    matcher: GraphComplianceMatcher,
    row_sem: asyncio.Semaphore,
    browser_sem: asyncio.Semaphore,
    *,
    page_timeout_ms: int = 30_000,
    dry_run: bool = False,
) -> RowOutcome:
    """Shield -> crawl -> filter -> cascade -> verify -> (search) -> persist."""
    t0 = time.monotonic()
    used_search = False
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
                used_search = bool(candidates)
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

            if not live_portals:
                logger.warning("[FALLBACK TRIGGER] org %s — all %d portal(s) failed "
                               "verification, routing to DuckDuckGo search",
                               row.org_id, dead)
                hits = await search_rescue(row, lkm)
                if hits:
                    used_search = True
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

            # -- 4b. TWO-STEP PORTAL CRAWL -------------------------------
            legal_links: list[Any] = list(result.harvested_legal_links or [])
            n_home = len(legal_links)
            portal_legal = await harvest_portal_legal_links(
                row, live_portals, lkm, cache, browser_sem)
            if portal_legal:
                seen_l = {(getattr(c, "url", "") or "") for c in legal_links}
                legal_links += [c for c in portal_legal if c["url"] not in seen_l]
                logger.info("v3: org %s — legal candidates: %d homepage + %d portal-level",
                            row.org_id, n_home, len(legal_links) - n_home)

            # -- 5. GRAPH MATCH ------------------------------------------
            # Association is decided HERE, not by the model. Distance decays to
            # 0.4 when the candidates came from search rather than the crawl,
            # because a search result is weaker provenance for "this document
            # governs this portal".
            distance = DISTANCE_SEARCH_FALLBACK if used_search else DISTANCE_NATIVE_CRAWL
            mapping = matcher.resolve_optimal_compliance_mappings(
                [p for p, _ in live_portals],
                legal_links,
                distance,
                official_domain=row.domain)

            # -- 6/7. PERSIST + COMPOUND MEMORY --------------------------
            verified_rows, missing_rows, mem_entries, detail = [], [], [], []
            confidences: list[float] = []
            learned_now: list[str] = []
            for p, (live, code, _note) in live_portals:
                m = mapping.get(p.exact_url)
                tnc = (m or {}).get("tnc_url") or ""
                if not tnc:
                    cached = cache.legal_for_portal(p.exact_url)
                    if cached:
                        tnc = cached.get("tnc_url") or cached.get("privacy_policy_url") or ""
                        m = {"tnc_url": tnc, "confidence": 0.0,
                             "domain_relation": "memory-cache", "assignment": "memory"}
                        logger.info("v3: org %s — legal injected from memory (%s)",
                                    row.org_id, signature(p.exact_url))
                if tnc:
                    logger.info("[GRAPH MATCH SUCCESS] org %s — %s -> %s "
                                "(W=%.2f: domain=%.2f/%s semantic=%.2f distance=%.2f "
                                "ownership=%.2f/%s, %s)",
                                row.org_id, p.exact_url[:52], tnc[:52],
                                m.get("confidence", 0.0), m.get("s_domain", 0.0),
                                m.get("domain_relation", "?"), m.get("s_semantic", 0.0),
                                m.get("s_distance", 0.0), m.get("s_ownership", 1.0),
                                m.get("ownership_relation", "?"), m.get("assignment", "?"))
                    confidences.append(float(m.get("confidence", 0.0)))
                    # Co-learning: this document is live AND matched, so the
                    # phrase that identified it is trustworthy enough to keep.
                    kw = next((getattr(c, "detected_native_keyword", None)
                               for c in legal_links
                               if (getattr(c, "url", "") or "") == tnc), None)
                    if kw:
                        learned_now.append(kw)
                    verified_rows.append({"orgId": result.org_id,
                                          "portal_url": p.exact_url, "tnc_url": tnc})
                    mem_entries.append({"org_id": result.org_id, "exact_url": p.exact_url,
                                        "portal_system_name": p.portal_system_name,
                                        "tnc_url": tnc, "privacy_policy_url": None,
                                        "graph_confidence": m.get("confidence", 0.0)})
                else:
                    logger.info("[GRAPH MATCH GATED] org %s — %s: no legal link "
                                "cleared the %.2f threshold",
                                row.org_id, p.exact_url[:52], matcher.threshold)
                    missing_rows.append({"orgId": result.org_id, "portal_url": p.exact_url})
                detail.append({"url": p.exact_url, "category": p.category,
                               "system": p.portal_system_name, "tnc_url": tnc or None,
                               "graph": m, "http_status": code})

            if learned_now and not dry_run:
                await learn_native_keywords(learned_now, matcher)

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
                              seconds=round(time.monotonic() - t0, 1),
                              stages=[f"{c:.2f}" for c in confidences],
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
    only_org_ids: set[str] | None = None,
) -> list[RowOutcome]:
    rows = await fetch_rows(sheet_id, tab, start_row, count, only_org_ids)
    if not rows:
        logger.info("v3: nothing to do")
        return []

    lkm = LocalKnowledgeMatrixFilter()
    # `resume=False` disables the shield's history arm only; block-skipping and
    # vendor lookups stay on, since those are correctness aids rather than
    # progress tracking.
    cache = MemoryCache(retry_blocked=retry_blocked)
    # Vendor roots from the knowledge base feed S_domain's SaaS tier, so a
    # tenant portal can be matched to its vendor's corporate terms.
    matcher = GraphComplianceMatcher(
        saas_roots={e["root"] for e in (lkm.kb.get("saas_infra_whitelist") or [])
                    if e.get("root")},
        learned_keywords=lkm.kb.get("learned_legal_keywords") or [])
    if matcher.learned_keywords:
        logger.info("v3: loaded %d learned native keyword(s) from the knowledge base",
                    len(matcher.learned_keywords))
    if not resume:
        cache.history = {}
    row_sem = asyncio.Semaphore(row_concurrency)
    browser_sem = asyncio.Semaphore(BROWSER_SEMAPHORE)
    logger.info("v3: %d universities | row turnstile %d | browsers %d | dry_run=%s",
                len(rows), row_concurrency, BROWSER_SEMAPHORE, dry_run)

    tasks = [process_university(r, lkm, cache, matcher, row_sem, browser_sem,
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
        print("  graph confidences      :", dict(st))
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
    ap.add_argument("--org-ids-file", default=None,
                    help="JSON list/dict or newline-delimited org IDs; restricts "
                         "the run to those orgs wherever they sit in the tab")
    ap.add_argument("--retry-blocked", action="store_true",
                    help="re-attempt hosts recorded in infrastructure_block.json")
    ap.add_argument("--dry-run", action="store_true", help="run everything, write nothing")
    ap.add_argument("--verbose", action="store_true", help="DEBUG-level logging")
    ap.add_argument("--log-file", default=None,
                    help=f"default: {LOG_FILE_DEFAULT}")
    a = ap.parse_args()

    # Guarantees the directory exists before any handler or writer touches it.
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    setup_logging(logging.DEBUG if a.verbose else logging.INFO,
                  Path(a.log_file) if a.log_file else None)
    only = None
    if a.org_ids_file:
        raw = Path(a.org_ids_file).read_text()
        try:
            parsed = json.loads(raw)
            only = set(map(str, parsed.keys() if isinstance(parsed, dict) else parsed))
        except json.JSONDecodeError:
            only = {ln.strip() for ln in raw.splitlines() if ln.strip()}
        logger.info("v3: restricted to %d org id(s) from %s", len(only), a.org_ids_file)

    outcomes = asyncio.run(run_pipeline(
        a.sheet_id, a.tab, a.start_row, a.count,
        resume=not a.no_resume, dry_run=a.dry_run, retry_blocked=a.retry_blocked,
        page_timeout_ms=a.page_timeout_ms, row_concurrency=a.concurrency,
        only_org_ids=only,
    ))
    summarise(outcomes)


if __name__ == "__main__":
    main()

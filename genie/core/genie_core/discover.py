"""Live discovery: wraps the existing agent pipeline (agent.stages.discovery.run)
and streams its log output as ProgressEvents, then yields the final portals.

The pipeline isn't callback-instrumented, so we capture the `agent.*` logger
output (the same "found … / REJECTED … / validate KEEP …" lines you see on the
CLI) and push each line to the stream — real step-by-step progress with zero
changes to the agent."""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

# Repo root so `agent.*` imports resolve (genie/core/genie_core/ -> repo root).
_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from .models import Portal, ProgressEvent


def host_of(url: str) -> str:
    u = url.strip()
    if "://" not in u:
        u = "http://" + u
    h = (urlparse(u).netloc or "").lower().split(":")[0]
    return h[4:] if h.startswith("www.") else h


def _portal_key(url: str) -> str:
    p = urlparse(url if "://" in url else "http://" + url)
    h = (p.netloc or "").lower().split(":")[0]
    h = h[4:] if h.startswith("www.") else h
    return h + (p.path or "").rstrip("/")


def _log_shadow(log, rules_res: dict, judge_res: dict) -> None:
    """Log a recall comparison rules-vs-judge (shadow mode). Output is
    unchanged; this is pure measurement of what the judge would add/miss."""
    r = {_portal_key(p.get("url", "")) for p in rules_res.get("portals", [])}
    j = {_portal_key(p.get("url", "")) for p in judge_res.get("portals", [])}
    log.info("[shadow] rules=%d judge=%d  shared=%d  judge-only=%d  rules-only=%d",
             len(r), len(j), len(r & j), len(j - r), len(r - j))
    if j - r:
        log.info("[shadow] judge FOUND (rules missed): %s", sorted(j - r))
    if r - j:
        log.info("[shadow] rules found (judge missed): %s", sorted(r - j))


def _name_from_domain(domain: str) -> str:
    """A best-effort readable name from a domain when the caller gives none —
    e.g. 'dbskkv.ac.in' -> 'dbskkv'. Enough to satisfy the pipeline; Gemini
    resolves the real institution from this + the domain."""
    label = domain.split(".")[0] if domain else "university"
    return label or "university"


async def discover_portals(url: str, include_affiliated: bool = True, name: str = "",
                           orgid: str = "", suppress: bool = True):
    """Async generator of ProgressEvent. Terminal events: 'result' then 'done'
    (or 'error'). If `orgid` is given, URLs a human previously disputed for that
    org are suppressed from the results (the 'training' the UI performs).

    `suppress=False` is 'investigate/raw' mode: nothing is dropped — disputed and
    globally-denied portals are kept and merely flagged, so you can see exactly
    what the agent produces and why (used by the Training → Investigate button)."""
    from agent.config import load_config
    from agent.pipeline import PipelineContext
    from agent.stages import discovery
    from agent.stages.js_renderer import JSRenderer
    from agent.state import StateStore

    domain = host_of(url)
    if not domain:
        yield ProgressEvent("error", f"could not parse a domain from {url!r}")
        return
    yield ProgressEvent("log", f"▶ discovering portals for {domain}  (affiliated={include_affiliated})")

    # Self-improving: feed the agent the subdomain patterns it learned for this
    # domain's country in earlier runs (e.g. Brazil learned 'portalservicos').
    from . import db as _db
    country = _db.country_from_domain(domain)
    learned_probes = _db.get_learned_patterns(country) if country != "Global" else []
    if learned_probes:
        yield ProgressEvent("log",
            f"🧠 applying {len(learned_probes)} learned {country} portal pattern(s)")

    config = load_config()
    loop = asyncio.get_running_loop()
    q: asyncio.Queue = asyncio.Queue()

    class _QueueHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            try:
                msg = self.format(record)
            except Exception:
                return
            loop.call_soon_threadsafe(q.put_nowait, ProgressEvent("log", msg))

    handler = _QueueHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    ag_logger = logging.getLogger("agent")
    ag_logger.addHandler(handler)
    prev_level = ag_logger.level
    ag_logger.setLevel(logging.INFO)

    box: dict = {}

    # Discovery engine selector (local/experimental; prod default = rules):
    #   rules  — the legacy rule pipeline (agent.stages.discovery), unchanged.
    #   judge  — the rules-free global LLM-judge agent (agent.global_agent).
    #   shadow — run rules for the OUTPUT, also run the judge and log a
    #            recall comparison (judge-only hosts) without changing output.
    engine = os.environ.get("DISCOVERY_ENGINE", "rules").strip().lower()
    uni_name = (name or "").strip() or _name_from_domain(domain)
    _aglog = logging.getLogger("agent")

    def _judge_result() -> dict:
        from agent import global_agent
        ctry = _db.country_from_domain(domain)
        portals = global_agent.discover(uni_name, domain,
                                        "" if ctry == "Global" else ctry)
        return {"university_name": uni_name, "portals": [{
            "url": p["url"], "category": p.get("category", ""),
            "discovery_source": "judge:" + str(p.get("provenance", "")),
            "discovery_reasoning": p.get("reason", ""),
        } for p in portals]}

    def _run() -> None:
        jr = None
        try:
            if engine == "judge":
                box["res"] = _judge_result()
                return
            if config.enable_js_rendering:
                jr = JSRenderer(timeout_seconds=config.js_rendering_timeout_seconds,
                                user_agent=config.user_agent)
            with StateStore(":memory:") as state:
                deps = {"state": state, "js_renderer": jr,
                        "user_agent": config.user_agent,
                        "http_timeout": config.http_timeout_seconds}
                if not include_affiliated:
                    deps["_skip_affiliation_discovery"] = True
                if learned_probes:
                    deps["learned_probes"] = learned_probes
                ctx = PipelineContext(
                    orgid=f"genie:{domain}",
                    row={"SheerID University Name": uni_name,
                         "SheerID Website Domain": domain},
                    deps=deps,
                )
                box["res"] = discovery.run(ctx)
            if engine == "shadow":
                try:
                    jr_res = _judge_result()
                    _log_shadow(_aglog, box.get("res") or {}, jr_res)
                except Exception as e:  # noqa: BLE001
                    _aglog.warning("[shadow] judge run failed: %s", e)
        except Exception as e:  # noqa: BLE001
            box["err"] = e
        finally:
            if jr is not None:
                try:
                    jr.close()
                except Exception:
                    pass
            loop.call_soon_threadsafe(q.put_nowait, ProgressEvent("done"))

    fut = loop.run_in_executor(None, _run)
    try:
        while True:
            ev = await q.get()
            if ev.kind == "done":
                break
            yield ev
        await fut
    finally:
        ag_logger.removeHandler(handler)
        ag_logger.setLevel(prev_level)

    if "err" in box:
        yield ProgressEvent("error", f"{type(box['err']).__name__}: {box['err']}")
        return

    res = box.get("res") or {}
    # A stable org id so confirm/dispute + suppression work even on the paste-a-URL
    # flow (Discover live): default to genie:{domain} when the caller gives none.
    from . import db
    effective_orgid = (orgid or "").strip() or f"genie:{domain}"
    # Suppress anything a human already marked wrong for this org (rule-based
    # "training" — no ML). Match on normalized URL.
    disputed: set[str] = db.get_disputed(effective_orgid)
    # Global learned rules mined from all feedback (Levels 1 & 2), human-gated.
    from . import training
    active_rules = db.active_rules()
    vidx = db.verified_index()
    portals = []
    suppressed = 0          # this-org dispute suppression
    global_denied = 0       # global learned-rule denials
    global_flagged = 0
    for p in res.get("portals", []):
        purl = str(p.get("url", ""))
        _nu = db._norm_url(purl)
        was_disputed = bool(disputed) and _nu in disputed
        if suppress and was_disputed:
            suppressed += 1
            continue
        action, pattern = training.apply_rules(purl, active_rules) if active_rules else ("", "")
        if suppress and action == "deny":
            global_denied += 1
            continue
        src = str(p.get("discovery_source", ""))
        # In raw mode, show deny/dispute as a flag instead of dropping.
        flag = pattern if action in ("flag", "deny") else ""
        if not flag and was_disputed:
            flag = "previously-disputed"
        portals.append(Portal(
            url=purl, category=str(p.get("category", "")),
            source=src, reasoning=str(p.get("discovery_reasoning", "")),
            affiliated_from=("affiliated" if "affili" in src.lower() else ""),
            flag=flag,
            verified=db.is_url_live(vidx, purl),
        ))
        if action == "flag":
            global_flagged += 1
    # Refine generic categories using page content (the agent often defaults to
    # "Student Portal"). Only touch generic/blank labels; run concurrently.
    _GENERIC = {"", "Portal", "Student Portal", "ERP / Student Portal"}
    to_fix = [p for p in portals if (p.category or "") in _GENERIC]
    if to_fix:
        from . import categorize
        loop2 = asyncio.get_running_loop()
        try:
            results = await asyncio.gather(
                *[loop2.run_in_executor(None, lambda u=p.url: categorize.classify(u, timeout=10))
                  for p in to_fix])
            for p, (cat, score, _ev) in zip(to_fix, results):
                if score > 0 and cat != p.category:
                    p.category = cat
        except Exception:  # noqa: BLE001 — categorization is best-effort
            pass

    # Self-improving: record this run's validated portals as per-country
    # subdomain patterns, so future runs in this country probe them first.
    if country != "Global" and portals:
        try:
            from agent.stages.discovery_rules import registrable_root
            root = registrable_root(domain) or domain
            learned_now = 0
            for p in portals:
                if getattr(p, "flag", ""):
                    continue  # don't learn from flagged/disputed portals
                phost = urlparse(p.url).netloc.lower().split(":")[0]
                if phost.endswith("." + root) and (registrable_root(phost) or phost) == root:
                    label = phost[: -(len(root) + 1)]
                    if label:
                        _db.record_learned_pattern(country, label, p.category or "")
                        learned_now += 1
            if learned_now:
                yield ProgressEvent("log",
                    f"🧠 learned {learned_now} {country} portal pattern(s) from this run")
        except Exception:  # noqa: BLE001 — learning is best-effort
            pass

    if suppressed:
        yield ProgressEvent("log", f"🧞 suppressed {suppressed} portal(s) you previously marked wrong")
    if global_denied:
        yield ProgressEvent("log", f"🧞 dropped {global_denied} portal(s) via learned global rules")
    if global_flagged:
        yield ProgressEvent("log", f"🧞 flagged {global_flagged} portal(s) as likely-wrong (learned rules)")
    for p in portals:
        yield ProgressEvent("portal", p.url, p.to_dict())
    uni_verified = db.is_org_verified(vidx, orgid=effective_orgid,
                                      name=res.get("university_name", ""),
                                      portal_urls=[p.url for p in portals])
    yield ProgressEvent("result", f"{len(portals)} portal(s) found", {
        "university": res.get("university_name", ""),
        "domain": domain,
        "orgid": effective_orgid,
        "verified": uni_verified,
        "portals": [p.to_dict() for p in portals],
        "completed_with_timeout": bool(res.get("completed_with_timeout", False)),
    })

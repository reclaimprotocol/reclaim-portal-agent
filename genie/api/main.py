"""Genie API — thin FastAPI adapter over genie_core.

    GET  /health
    GET  /search?q=...&limit=20        -> Feature 1: portals we already have
    POST /discover  {url, include_affiliated}  -> Feature 2: live discovery
    GET  /stream/{job_id}              -> SSE progress + final result

Later, an MCP server will expose the SAME genie_core.search_portals /
discover_portals functions as tools — no logic lives here.

Run:  .venv/bin/uvicorn genie.api.main:app --reload --port 8000
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "genie" / "core"))

import csv  # noqa: E402
import io  # noqa: E402

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import Response  # noqa: E402
from pydantic import BaseModel  # noqa: E402
from sse_starlette.sse import EventSourceResponse  # noqa: E402

import genie_core  # noqa: E402

app = FastAPI(title="Genie API", version="0.1.0")

# Ensure schema + migrations are current on boot (idempotent).
genie_core.db.init_db()

# CORS for the Next.js dev server (and configurable origins in prod).
_origins = os.getenv("GENIE_CORS_ORIGINS", "http://localhost:3000").split(",")
app.add_middleware(
    CORSMiddleware, allow_origins=_origins, allow_methods=["*"],
    allow_headers=["*"], allow_credentials=False,
)

# In-memory job registry: job_id -> {"url","include_affiliated","name"}.
# Fine for a single-instance MVP; swap for Redis when it scales.
_JOBS: dict[str, dict] = {}


class DiscoverIn(BaseModel):
    url: str
    include_affiliated: bool = True
    name: str = ""
    orgid: str = ""
    suppress: bool = True   # False = investigate/raw (keep disputed+denied, just flag)


class ConfirmIn(BaseModel):
    orgid: str
    url: str
    category: str = ""
    university: str = ""
    domain: str = ""


class DisputeIn(BaseModel):
    orgid: str
    url: str
    reason: str = ""
    category: str = ""
    source: str = ""
    reasoning: str = ""


class CommentIn(BaseModel):
    comment: str = ""


class RuleIn(BaseModel):
    status: str | None = None   # 'active' | 'rejected' | 'proposed'
    action: str | None = None   # 'flag' | 'deny'


class RuleCreateIn(BaseModel):
    rule_type: str              # 'host' | 'pattern'
    pattern: str
    action: str = "deny"        # 'flag' | 'deny'


@app.get("/health")
def health() -> dict:
    return {"ok": True, "backend": "postgres" if genie_core.db.is_postgres() else "sqlite",
            **genie_core.db.stats()}


@app.get("/insights")
def insights() -> dict:
    """Aggregate dashboard stats for the Quick Insights page."""
    return genie_core.db.insights()


@app.get("/search")
def search(q: str, limit: int = 20, country: str | None = None, state: str | None = None) -> dict:
    results = genie_core.search_portals(q, limit=limit, country=country, state=state)
    return {"query": q, "country": country, "state": state, "count": len(results),
            "results": [r.to_dict() for r in results]}


@app.get("/portals")
def portals(offset: int = 0, limit: int = 50, category: str | None = None,
            q: str | None = None, country: str | None = None, state: str | None = None) -> dict:
    """Paginated flat browse of the portals DB (optional country/state/category/q filter)."""
    return genie_core.db.list_portals(offset=offset, limit=limit, category=category, q=q,
                                      country=country, state=state)


@app.get("/universities")
def universities(offset: int = 0, limit: int = 40, country: str | None = None,
                 state: str | None = None, q: str | None = None,
                 only_missing: bool = False) -> dict:
    """University-centric browse: each row carries its portals (with status),
    and no-portal universities are included so the UI can offer Discover."""
    return genie_core.db.list_universities(offset=offset, limit=limit, country=country,
                                           state=state, q=q, only_missing=only_missing)


@app.post("/confirm")
def confirm(body: ConfirmIn) -> dict:
    """Human-approve a discovered portal (persists it as confirmed)."""
    genie_core.db.confirm_portal(orgid=body.orgid, url=body.url, category=body.category,
                                 university=body.university, domain=body.domain, created_at=_now())
    return {"ok": True}


@app.post("/dispute")
def dispute(body: DisputeIn) -> dict:
    """Human-reject a portal — trains Genie to suppress it for this org."""
    genie_core.db.dispute_portal(orgid=body.orgid, url=body.url, reason=body.reason,
                                 category=body.category, source=body.source,
                                 reasoning=body.reasoning, created_at=_now())
    return {"ok": True, "trained": True}


@app.get("/training/disputes")
def training_disputes(offset: int = 0, limit: int = 50, q: str | None = None) -> dict:
    """The dispute review log — each wrong portal with agent reasoning + comment."""
    return genie_core.db.list_disputes(offset=offset, limit=limit, q=q)


@app.post("/training/disputes/{dispute_id}")
def training_dispute_comment(dispute_id: int, body: CommentIn) -> dict:
    """Add/edit the human comment on a disputed URL (for the improvement loop)."""
    genie_core.db.update_dispute(dispute_id, comment=body.comment)
    return {"ok": True}


@app.delete("/training/disputes/{dispute_id}")
def training_dispute_delete(dispute_id: int) -> dict:
    genie_core.db.delete_dispute(dispute_id)
    return {"ok": True}


@app.get("/training/stats")
def training_stats() -> dict:
    return genie_core.db.feedback_stats()


@app.post("/training/mine")
def training_mine() -> dict:
    """Aggregate the feedback log into proposed global rules (Levels 1 & 2)."""
    return genie_core.training.mine_rules(now=_now())


@app.get("/training/rules")
def training_rules(status: str | None = None) -> dict:
    return {"rules": genie_core.db.list_rules(status=status)}


@app.post("/training/rules/create")
def training_rule_create(body: RuleCreateIn) -> dict:
    """Create (or activate) a global rule from a single dispute — instant, no
    frequency threshold. 'Make a rule from this' action in the Disputes log."""
    try:
        rule = genie_core.db.create_rule(rule_type=body.rule_type, pattern=body.pattern,
                                         action=body.action, now=_now())
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "rule": rule}


@app.get("/training/tokens")
def training_tokens(url: str) -> dict:
    """Meaningful path-tokens of a URL (minus the stoplist) — for the UI's
    'block a path term' chips, so they match the miner exactly."""
    from genie_core import training
    return {"host": genie_core.db._host(url), "tokens": sorted(training.path_tokens(url))}


@app.post("/training/rules/{rule_id}")
def training_rule_update(rule_id: int, body: RuleIn) -> dict:
    """Approve (status=active), reject, or change a rule's action (flag/deny)."""
    genie_core.db.set_rule(rule_id, status=body.status, action=body.action, updated_at=_now())
    return {"ok": True}


@app.get("/university")
def university(orgid: str) -> dict:
    """Full profile for one university (details + portals + verified + logo domain)."""
    u = genie_core.db.get_university(orgid)
    if not u:
        raise HTTPException(404, "unknown orgid")
    return u


class WebsiteIn(BaseModel):
    orgid: str
    website: str


@app.post("/university/website")
def university_set_website(body: WebsiteIn) -> dict:
    """Manually set a university's website (for rows ingested without one)."""
    w = body.website.strip()
    if w and not w.lower().startswith("http"):
        w = "https://" + w
    genie_core.db.update_university_website(body.orgid, w)
    return {"ok": True, "website": w}


@app.get("/lookup")
def lookup(url: str) -> dict:
    """DB-first check for Discover live: portals we already have for this site."""
    if not url.strip():
        raise HTTPException(400, "url is required")
    return genie_core.db.lookup_by_domain(url)


@app.get("/export")
def export(country: str | None = None, state: str | None = None,
           category: str | None = None) -> Response:
    """Download the portals DB as CSV, filtered by country/state/category."""
    rows = genie_core.db.export_rows(country=country, state=state, category=category)
    cols = ["university", "domain", "category", "portal_url", "country", "state",
            "city", "org_type", "status", "source"]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols)
    w.writeheader()
    for r in rows:
        w.writerow({k: r.get(k, "") for k in cols})
    scope = "-".join(x for x in [country, state] if x) or "all"
    fname = f"genie-portals-{scope}.csv".replace(" ", "_")
    return Response(content=buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@app.get("/categories")
def categories() -> dict:
    return {"categories": genie_core.db.categories()}


class CategoryIn(BaseModel):
    category: str


@app.post("/portals/{portal_id}/category")
def portal_set_category(portal_id: int, body: CategoryIn) -> dict:
    """Human-correct a portal's category; persists to the DB."""
    genie_core.db.update_portal_category(portal_id, body.category)
    return {"ok": True}


@app.get("/countries")
def countries() -> dict:
    return {"countries": genie_core.db.country_counts()}


@app.get("/states")
def states(country: str = "India") -> dict:
    return {"states": genie_core.db.states(country=country)}


@app.get("/metrics")
def metrics(url: str, refresh: bool = False) -> dict:
    """OpenPageRank (authority + rank) + Cloudflare Radar (bucket) for one URL."""
    return genie_core.get_metrics(url, refresh=refresh)


class MetricsBatchIn(BaseModel):
    urls: list[str]
    refresh: bool = False


@app.post("/metrics/batch")
def metrics_batch(body: MetricsBatchIn) -> dict:
    urls = body.urls[:100]
    res = genie_core.get_metrics_batch(urls, refresh=body.refresh)
    for u, r in zip(urls, res):
        r["url"] = u
    return {"metrics": res}


@app.post("/discover")
def discover(body: DiscoverIn) -> dict:
    """Register a discovery job; the client then opens /stream/{job_id}."""
    if not body.url.strip():
        raise HTTPException(400, "url is required")
    job_id = uuid.uuid4().hex[:12]
    _JOBS[job_id] = {"url": body.url, "include_affiliated": body.include_affiliated,
                     "name": body.name, "orgid": body.orgid, "suppress": body.suppress}
    return {"job_id": job_id}


@app.get("/stream/{job_id}")
async def stream(job_id: str) -> EventSourceResponse:
    job = _JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "unknown job_id")

    async def gen():
        try:
            async for ev in genie_core.discover_portals(
                job["url"], include_affiliated=job["include_affiliated"],
                name=job["name"], orgid=job.get("orgid", ""),
                suppress=job.get("suppress", True)
            ):
                yield {"event": ev.kind, "data": json.dumps(ev.to_dict())}
        except Exception as e:  # noqa: BLE001
            yield {"event": "error", "data": json.dumps({"kind": "error", "message": str(e)})}
        finally:
            _JOBS.pop(job_id, None)
            yield {"event": "close", "data": "{}"}

    return EventSourceResponse(gen())

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
import base64
import hashlib
import hmac
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "genie" / "core"))

import csv  # noqa: E402
import io  # noqa: E402

from fastapi import Depends, FastAPI, HTTPException, Request  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import Response  # noqa: E402
from pydantic import BaseModel  # noqa: E402
from sse_starlette.sse import EventSourceResponse  # noqa: E402

import genie_core  # noqa: E402

# --- Auth: Google Sign-In (per-user) + API key (scripts) -----------------
# Primary auth is "Sign in with Google", restricted to a single email domain
# (ALLOWED_EMAIL_DOMAIN, default reclaimprotocol.org). The frontend obtains a
# Google ID token, POSTs it to /auth/google, and we return a signed session
# token the browser sends as `Authorization: Bearer <token>`.
# GENIE_API_KEY is kept as a fallback for scripts/CLI. If NOTHING is
# configured, auth is disabled (local dev).
_API_KEY = os.getenv("GENIE_API_KEY", "").strip()
_GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "").strip()
_SESSION_SECRET = os.getenv("GENIE_SESSION_SECRET", "").strip()
_ALLOWED_DOMAIN = os.getenv("ALLOWED_EMAIL_DOMAIN", "reclaimprotocol.org").strip().lower()
# Admin emails (comma-separated) — see the admin dashboard (who's logged in +
# what they searched). Defaults to Rohit.
_ADMIN_EMAILS = {e.strip().lower() for e in os.getenv(
    "GENIE_ADMIN_EMAILS", "rohit@reclaimprotocol.org").split(",") if e.strip()}
_OPEN_PATHS = {"/health", "/docs", "/redoc", "/openapi.json", "/auth/google", "/auth/config"}


def _is_admin(email: str) -> bool:
    return bool(email) and email.lower() in _ADMIN_EMAILS
if not (_API_KEY or _SESSION_SECRET):
    print("⚠️  No GENIE_API_KEY / GENIE_SESSION_SECRET — API auth DISABLED (dev).", file=sys.stderr)


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def make_session_token(email: str, ttl_seconds: int = 7 * 24 * 3600) -> str:
    """Signed session token (HS256, stdlib) — payload.signature."""
    payload = _b64e(json.dumps({"email": email, "exp": int(time.time()) + ttl_seconds}).encode())
    sig = _b64e(hmac.new(_SESSION_SECRET.encode(), payload.encode(), hashlib.sha256).digest())
    return f"{payload}.{sig}"


def verify_session_token(token: str) -> str | None:
    """Return the email if the token is valid + unexpired, else None."""
    if not _SESSION_SECRET or not token or "." not in token:
        return None
    payload, sig = token.split(".", 1)
    expected = _b64e(hmac.new(_SESSION_SECRET.encode(), payload.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        data = json.loads(_b64d(payload))
    except Exception:
        return None
    if int(data.get("exp", 0)) < int(time.time()):
        return None
    return data.get("email")


def verify_google_credential(credential: str) -> str | None:
    """Verify a Google ID token; return the verified email or None."""
    from google.oauth2 import id_token
    from google.auth.transport import requests as grequests
    try:
        info = id_token.verify_oauth2_token(credential, grequests.Request(), _GOOGLE_CLIENT_ID)
    except Exception:
        return None
    if not info.get("email_verified"):
        return None
    return (info.get("email") or "").lower()


async def require_auth(request: Request) -> None:
    if request.method == "OPTIONS" or request.url.path in _OPEN_PATHS:
        return
    if not (_API_KEY or _SESSION_SECRET):
        return  # auth disabled (dev)
    auth = request.headers.get("authorization", "")
    bearer = auth[7:] if auth[:7].lower() == "bearer " else ""
    # 1) session token (Google-signed-in user)
    tok = bearer or request.query_params.get("token", "")
    email = verify_session_token(tok)
    if email and email.endswith("@" + _ALLOWED_DOMAIN):
        return
    # 2) API-key fallback (scripts/CLI): X-API-Key, Bearer, or ?key=
    if _API_KEY:
        provided = request.headers.get("x-api-key", "") or bearer or request.query_params.get("key", "")
        if provided and hmac.compare_digest(provided, _API_KEY):
            return
    raise HTTPException(status_code=401, detail="authentication required")


# Global dependency: runs inside routing (after CORS middleware), so 401s
# still carry CORS headers and the browser sees the real status.
app = FastAPI(title="Genie API", version="0.1.0", dependencies=[Depends(require_auth)])

# Ensure schema + migrations are current on boot (idempotent).
genie_core.db.init_db()

# CORS for the Next.js dev server (and configurable origins in prod).
_origins = os.getenv("GENIE_CORS_ORIGINS", "http://localhost:3000").split(",")
app.add_middleware(
    CORSMiddleware, allow_origins=_origins, allow_methods=["*"],
    allow_headers=["*"], allow_credentials=False,
)

# --- Sign-in endpoints (open — see _OPEN_PATHS) --------------------------
class GoogleAuthIn(BaseModel):
    credential: str


@app.get("/auth/config")
def auth_config() -> dict:
    """Tells the frontend whether Google sign-in is on + the client id/domain."""
    return {"enabled": bool(_GOOGLE_CLIENT_ID and _SESSION_SECRET),
            "client_id": _GOOGLE_CLIENT_ID, "domain": _ALLOWED_DOMAIN}


@app.post("/auth/google")
def auth_google(body: GoogleAuthIn) -> dict:
    """Verify a Google ID token, enforce the allowed email domain, and return a
    session token the browser sends as `Authorization: Bearer <token>`."""
    if not (_GOOGLE_CLIENT_ID and _SESSION_SECRET):
        raise HTTPException(status_code=500, detail="sign-in not configured on the server")
    email = verify_google_credential(body.credential)
    if not email:
        raise HTTPException(status_code=401, detail="invalid Google credential")
    if not email.endswith("@" + _ALLOWED_DOMAIN):
        raise HTTPException(status_code=403, detail=f"only @{_ALLOWED_DOMAIN} accounts can access Genie")
    try:
        genie_core.db.log_login(email)
    except Exception:  # noqa: BLE001 — logging must never block sign-in
        pass
    return {"token": make_session_token(email), "email": email, "is_admin": _is_admin(email)}


def _request_email(request: Request) -> str:
    """The signed-in email from the Bearer/query token (or '' for API-key/dev)."""
    auth = request.headers.get("authorization", "")
    bearer = auth[7:] if auth[:7].lower() == "bearer " else ""
    return verify_session_token(bearer or request.query_params.get("token", "")) or ""


@app.get("/auth/me")
def auth_me(request: Request) -> dict:
    """Current identity for the UI (who am I, am I an admin)."""
    email = _request_email(request)
    return {"email": email, "is_admin": _is_admin(email)}


def require_admin(request: Request) -> str:
    email = _request_email(request)
    if not _is_admin(email):
        raise HTTPException(status_code=403, detail="admin only")
    return email


@app.get("/admin/activity")
def admin_activity(_: str = Depends(require_admin), limit: int = 200) -> dict:
    """Admin dashboard feed: recent logins + searches (with results)."""
    return genie_core.db.recent_activity(limit=limit)


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
    # Discovery config visibility (booleans only — never the key value) so we can
    # confirm live-discovery is wired without shelling into the server.
    return {"ok": True, "backend": "postgres" if genie_core.db.is_postgres() else "sqlite",
            "gemini_search": os.getenv("GEMINI_SEARCH_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on"),
            "openrouter_key": bool(os.getenv("OPENROUTER_API_KEY", "").strip()),
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
def discover(body: DiscoverIn, request: Request) -> dict:
    """Register a discovery job; the client then opens /stream/{job_id}."""
    if not body.url.strip():
        raise HTTPException(400, "url is required")
    job_id = uuid.uuid4().hex[:12]
    _JOBS[job_id] = {"url": body.url, "include_affiliated": body.include_affiliated,
                     "name": body.name, "orgid": body.orgid, "suppress": body.suppress,
                     "email": _request_email(request)}
    return {"job_id": job_id}


@app.get("/stream/{job_id}")
async def stream(job_id: str) -> EventSourceResponse:
    job = _JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "unknown job_id")

    async def gen():
        result_portals: list = []
        try:
            async for ev in genie_core.discover_portals(
                job["url"], include_affiliated=job["include_affiliated"],
                name=job["name"], orgid=job.get("orgid", ""),
                suppress=job.get("suppress", True)
            ):
                if ev.kind == "result":
                    try:
                        result_portals = (ev.to_dict().get("data") or {}).get("portals", [])
                    except Exception:  # noqa: BLE001
                        pass
                yield {"event": ev.kind, "data": json.dumps(ev.to_dict())}
        except Exception as e:  # noqa: BLE001
            yield {"event": "error", "data": json.dumps({"kind": "error", "message": str(e)})}
        finally:
            try:  # log the search for the admin dashboard (never block the stream)
                genie_core.db.log_search(
                    job.get("email", ""), job["url"], orgid=job.get("orgid", ""),
                    result_count=len(result_portals),
                    results=[p.get("url") for p in result_portals if isinstance(p, dict)])
            except Exception:  # noqa: BLE001
                pass
            _JOBS.pop(job_id, None)
            yield {"event": "close", "data": "{}"}

    return EventSourceResponse(gen())

"""Genie-V3 service · HTTP surface.

    GET  /health
    POST /runs                      body = the CSV itself -> {job_id}
    GET  /runs                      recent runs, newest first
    GET  /runs/{job_id}             progress
    GET  /runs/{job_id}/results.csv results (downloadable MID-RUN)

Run:  uvicorn service.api:app --port 8800

ONE PROCESS, ONE DISK
---------------------
There is no database and no separate worker: runs are directories, and the
work happens in a background task in this same process. That is a deliberate
consequence of dropping the DB — two containers cannot share a local
filesystem, so splitting them would buy nothing and cost coordination.

Runs execute ONE AT A TIME, queued. Not for correctness — for memory. Each run
already holds AGENT_CONCURRENCY headless Chromium instances open; letting two
batches overlap doubles that and this is the failure mode that presents as
network timeouts, i.e. as live portals recorded dead.

AUTH: one shared secret in `X-API-Key`. Call it from your dashboard's BACKEND.
A key shipped to frontend JS is a public key.
"""
from __future__ import annotations

import asyncio
import hmac
import logging
import os
import sys
from contextlib import asynccontextmanager, suppress
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI, HTTPException, Request  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse  # noqa: E402

from service import csvio, memory, runs  # noqa: E402
from service.csvio import CsvContractError  # noqa: E402

logger = logging.getLogger("genie.service")

#: Uvicorn configures handlers for its OWN loggers and leaves the root logger
#: alone, so without this every logger.info() in the service and the engine is
#: dropped and only WARNING+ reaches stderr via logging's lastResort handler.
#: Verified in the container: a completed run printed the uvicorn access line
#: and nothing else — no per-org result, no cascade escalation, no dead
#: endpoint. On a hosted deploy that is the entire debugging surface.
logging.basicConfig(
    level=getattr(logging, os.getenv("AGENT_LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    stream=sys.stdout,
    force=True,          # uvicorn may have already installed a root handler
)

API_KEY = (os.getenv("AGENT_API_KEY") or "").strip()
#: Running without a key must be a CHOICE, not an accident. An unset
#: AGENT_API_KEY previously disabled auth silently, so a deploy that forgot the
#: secret would serve uploaded organisation data to anyone and let strangers
#: queue hours of Chromium work. The service now refuses to start unless the
#: operator opts in explicitly.
ALLOW_NO_AUTH = (os.getenv("AGENT_ALLOW_NO_AUTH") or "").strip().lower() in ("1", "true", "yes")
#: Clamped: 0 or a negative value makes asyncio.Semaphore raise at startup for
#: what is only a typo in an env var.
CONCURRENCY = max(1, int(os.getenv("AGENT_CONCURRENCY", "4")))
MAX_ORGS = int(os.getenv("AGENT_MAX_ORGS_PER_RUN", "5000"))
MAX_UPLOAD_BYTES = int(os.getenv("AGENT_MAX_UPLOAD_BYTES", str(8 * 1024 * 1024)))
OPEN_PATHS = {"/health", "/docs", "/redoc", "/openapi.json"}

#: Created inside `lifespan`, never at import. A module-level asyncio.Queue()
#: binds to whichever event loop happens to exist when the module is imported,
#: which is not the loop the app later runs on — `put` then never wakes the
#: consumer and every run sits at `queued` forever. Silent on 3.10+, fatal
#: below it, and wrong in both.
_queue: asyncio.Queue | None = None


def _require_key(request: Request) -> None:
    if request.url.path in OPEN_PATHS or request.method == "OPTIONS":
        return
    if not API_KEY:
        return                          # explicitly opted in — see lifespan
    auth = request.headers.get("authorization", "")
    provided = (request.headers.get("x-api-key", "")
                or (auth[7:] if auth[:7].lower() == "bearer " else ""))
    if not (provided and hmac.compare_digest(provided, API_KEY)):
        raise HTTPException(401, "invalid or missing API key")


# --------------------------------------------------------------------------- #
#  Background execution                                                        #
# --------------------------------------------------------------------------- #
async def execute_run(job_id: str) -> None:
    """Process every org in a run, appending results as each one lands."""
    from service.discovery import process_org

    d = runs.run_dir(job_id)
    rows = csvio.parse_input((d / "input.csv").read_bytes())
    runs.update_status(job_id, status=runs.RUNNING, started_at=runs._now())
    logger.info("run %s started — %d org(s), concurrency %d",
                job_id, len(rows), CONCURRENCY)

    sem = asyncio.Semaphore(CONCURRENCY)
    write_lock = asyncio.Lock()

    async def one(row: csvio.InputRow) -> None:
        async with sem:
            out = await process_org(row)
        # Append + counter bump under one lock: results.csv and status.json
        # must not disagree, or a poller sees 40 done and 38 rows.
        async with write_lock:
            csvio.append_results(runs.results_path(job_id), out)
            runs.bump(job_id, {csvio.STATUS_NEW: "new_found",
                               csvio.STATUS_NONE: "none_found"}.get(
                                   out[0].get("status"), "failed"))

    try:
        await asyncio.gather(*(one(r) for r in rows))
        runs.update_status(job_id, status=runs.DONE, finished_at=runs._now())
        logger.info("run %s complete", job_id)
    except asyncio.CancelledError:
        runs.update_status(job_id, status=runs.INTERRUPTED,
                           finished_at=runs._now(), note="cancelled")
        raise


async def _consumer() -> None:
    assert _queue is not None
    while True:
        job_id = await _queue.get()
        try:
            await execute_run(job_id)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a broken run must not kill the loop
            logger.exception("run %s failed", job_id)
            runs.update_status(job_id, status=runs.INTERRUPTED,
                               finished_at=runs._now(), note="run aborted")
        finally:
            _queue.task_done()


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not API_KEY and not ALLOW_NO_AUTH:
        raise RuntimeError(
            "AGENT_API_KEY is not set. This service accepts uploads and runs "
            "expensive browser jobs, so it refuses to start unauthenticated. "
            "Set AGENT_API_KEY, or AGENT_ALLOW_NO_AUTH=1 for local development.")
    if not API_KEY:
        logger.warning("AGENT_ALLOW_NO_AUTH is set — this API is UNAUTHENTICATED")
    # Before anything imports the engine — memory_cache resolves its paths at
    # module import time, so a later seed would have no effect.
    seeded = memory.seed_memory_files()
    if seeded:
        logger.info("seeded L5 memory onto the disk: %s", "; ".join(seeded))
    runs.RUNS_DIR.mkdir(parents=True, exist_ok=True)
    stale = runs.reconcile_interrupted()
    if stale:
        logger.warning("marked %d run(s) interrupted after restart: %s",
                       len(stale), ", ".join(stale))
    purged = runs.purge_old()
    if purged:
        logger.info("purged %d run(s) past retention", len(purged))
    global _queue
    _queue = asyncio.Queue()
    task = asyncio.create_task(_consumer())
    yield
    # Await the cancellation. Without it shutdown races the consumer, and a run
    # in flight can be torn down between its status write and the results flush.
    # The consumer marks the run `interrupted` on CancelledError — give it the
    # chance to actually do that.
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


app = FastAPI(title="Genie-V3 portal discovery", version="1.0.0", lifespan=lifespan)

_origins = [o for o in os.getenv("AGENT_CORS_ORIGINS", "").split(",") if o.strip()]
if _origins:
    app.add_middleware(CORSMiddleware, allow_origins=_origins,
                       allow_methods=["*"], allow_headers=["*"])


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    try:
        _require_key(request)
    except HTTPException as e:
        return JSONResponse({"detail": e.detail}, status_code=e.status_code)
    return await call_next(request)


# --------------------------------------------------------------------------- #
#  Routes                                                                      #
# --------------------------------------------------------------------------- #
@app.get("/health")
def health() -> dict:
    return {"ok": True,
            "runs_dir": str(runs.RUNS_DIR),
            "concurrency": CONCURRENCY,
            "queued": _queue.qsize() if _queue is not None else 0,
            "openrouter_key": bool(os.getenv("OPENROUTER_API_KEY", "").strip()),
            "serper_key": bool(os.getenv("SERPER_API_KEY", "").strip())}


@app.post("/runs", status_code=202)
async def create_run(request: Request) -> dict:
    """Upload a CSV of organisations. Returns a job id immediately.

    The body IS the CSV — no multipart, so it works with
    `curl --data-binary @orgs.csv` and from any HTTP client without a form
    encoder.
    """
    body = await request.body()
    if not body:
        raise HTTPException(400, "request body must be the CSV")
    if len(body) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"CSV larger than {MAX_UPLOAD_BYTES} bytes")
    try:
        rows = csvio.parse_input(body)
    except CsvContractError as e:
        raise HTTPException(400, str(e)) from None
    if len(rows) > MAX_ORGS:
        raise HTTPException(413, f"at most {MAX_ORGS} organisations per run")

    job_id = runs.create(body, total=len(rows),
                         options={"requested_by": request.headers.get("x-requested-by", "")})
    csvio.open_results(runs.results_path(job_id))
    if _queue is None:
        raise HTTPException(503, "service still starting")
    await _queue.put(job_id)
    logger.info("run %s queued — %d org(s)", job_id, len(rows))
    return {"job_id": job_id, "total": len(rows), "status": runs.QUEUED,
            "results_url": f"/runs/{job_id}/results.csv"}


@app.get("/runs")
def list_runs(limit: int = 50) -> dict:
    return {"runs": runs.list_runs(limit=limit)}


def _status_or_404(job_id: str) -> dict:
    try:
        st = runs.read_status(job_id)
    except ValueError:
        raise HTTPException(400, "malformed job_id") from None
    if not st:
        raise HTTPException(404, "unknown job_id")
    return st


@app.get("/runs/{job_id}")
def run_status(job_id: str) -> dict:
    return _status_or_404(job_id)


@app.get("/runs/{job_id}/results.csv")
def run_results(job_id: str) -> FileResponse:
    """Downloadable at any point — partial while the run is still going."""
    _status_or_404(job_id)
    path = runs.results_path(job_id)
    if not path.exists():
        raise HTTPException(404, "no results yet")
    return FileResponse(path, media_type="text/csv", filename=f"{job_id}.csv")

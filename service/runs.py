"""Genie-V3 service · run store. The filesystem IS the database.

One directory per run, under AGENT_RUNS_DIR:

    runs/run_2026-09-19_a3f2c1/
        input.csv     exactly what was uploaded
        results.csv   appended as each org finishes — readable mid-run
        status.json   progress counters
        run.log       this run's log lines

No Postgres, no schema, no migrations. The tradeoff is that this only works
with ONE process on ONE disk, which is exactly the deployment shape here.

A run id carries its date (`run_2026-09-19_a3f2c1`) so the directory listing
sorts chronologically and a job id pasted into Slack is self-describing. The
suffix is random, not sequential, so two uploads in the same second cannot
collide.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
RUNS_DIR = Path(os.getenv("AGENT_RUNS_DIR", ROOT / "runs"))
RETENTION_DAYS = int(os.getenv("AGENT_RUN_RETENTION_DAYS", "90"))

QUEUED, RUNNING, DONE, INTERRUPTED = "queued", "running", "done", "interrupted"

#: `run_<date>_<suffix>` — anchored so a path segment can never traverse.
_RUN_ID_RE = re.compile(r"^run_\d{4}-\d{2}-\d{2}_[0-9a-f]{6}$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_run_id() -> str:
    return (f"run_{datetime.now(timezone.utc):%Y-%m-%d}_{secrets.token_hex(3)}")


def run_dir(job_id: str) -> Path:
    """Resolve a job id to its directory, rejecting anything malformed.

    The id arrives from the URL path, so it is untrusted input that becomes a
    filesystem path. Two independent defences, because one of them is a regex
    and regexes are easy to loosen by accident:

      1. the anchored pattern, which no `../` or absolute path can satisfy;
      2. containment — resolve the result and require it to sit under RUNS_DIR,
         which also catches a symlink inside the runs directory pointing out of
         it, something the pattern cannot see.

    CodeQL flags the taint flow from URL to path regardless of the regex; the
    containment check is what actually discharges it.
    """
    if not _RUN_ID_RE.match(job_id or ""):
        raise ValueError("malformed job_id")
    base = RUNS_DIR.resolve()
    candidate = (RUNS_DIR / job_id).resolve()
    if candidate != base and base not in candidate.parents:
        raise ValueError("job_id escapes the runs directory")
    return candidate


def create(input_csv: bytes, total: int, options: dict | None = None,
           attempts: int = 5) -> str:
    """Create a fresh run directory and return its id.

    The directory is created with `exist_ok=False` so a colliding id raises
    instead of overwriting. Six hex characters is only ~16M values, and the
    previous `exist_ok=True` meant a same-day collision would silently replace
    another run's input, status and results — a lost batch with no error
    anywhere. Retry on collision; give up rather than clobber.
    """
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    for _ in range(attempts):
        job_id = new_run_id()
        try:
            run_dir(job_id).mkdir(parents=True, exist_ok=False)
            break
        except FileExistsError:
            continue
    else:
        raise RuntimeError(f"could not allocate a free run id in {attempts} attempts")

    d = run_dir(job_id)
    (d / "input.csv").write_bytes(input_csv)
    write_status(job_id, {
        "job_id": job_id, "status": QUEUED, "created_at": _now(),
        "started_at": "", "finished_at": "",
        "total": total, "done": 0, "new_found": 0, "none_found": 0, "failed": 0,
        "options": options or {},
    })
    return job_id


def read_status(job_id: str) -> dict | None:
    p = run_dir(job_id) / "status.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def write_status(job_id: str, status: dict) -> None:
    """Atomic: a status file half-written when the container dies reads as
    corrupt JSON, and the run then looks lost rather than interrupted."""
    d = run_dir(job_id)
    tmp = d / ".status.tmp"
    tmp.write_text(json.dumps(status, indent=1))
    os.replace(tmp, d / "status.json")


def update_status(job_id: str, **fields: Any) -> dict:
    st = read_status(job_id) or {"job_id": job_id}
    st.update(fields)
    write_status(job_id, st)
    return st


def bump(job_id: str, outcome: str) -> dict:
    """Increment `done` plus the per-outcome counter, in one write."""
    st = read_status(job_id) or {"job_id": job_id}
    st["done"] = int(st.get("done", 0)) + 1
    st[outcome] = int(st.get(outcome, 0)) + 1
    write_status(job_id, st)
    return st


def results_path(job_id: str) -> Path:
    return run_dir(job_id) / "results.csv"


def list_runs(limit: int = 50) -> list[dict]:
    if not RUNS_DIR.exists():
        return []
    out = []
    for d in sorted(RUNS_DIR.iterdir(), reverse=True):
        if not d.is_dir() or not _RUN_ID_RE.match(d.name):
            continue
        st = read_status(d.name)
        if st:
            out.append({k: st.get(k) for k in
                        ("job_id", "status", "created_at", "finished_at",
                         "total", "done", "new_found", "none_found", "failed")})
        if len(out) >= limit:
            break
    return out


def reconcile_interrupted() -> list[str]:
    """Mark runs that were mid-flight when the process died.

    Nothing survives a container restart except these directories, so a run
    left at `running` would poll as in-progress forever. Its results.csv is
    still valid as far as it got — the orchestrator appends per org — so this
    marks the run `interrupted`, not failed, and the partial CSV stays
    downloadable.
    """
    touched = []
    for row in list_runs(limit=10_000):
        if row.get("status") in (QUEUED, RUNNING):
            update_status(row["job_id"], status=INTERRUPTED, finished_at=_now(),
                          note="process restarted while this run was in flight")
            touched.append(row["job_id"])
    return touched


def purge_old(days: int = RETENTION_DAYS) -> list[str]:
    """Delete run directories older than `days`. The disk is finite and every
    run keeps its input, output and log."""
    if days <= 0 or not RUNS_DIR.exists():
        return []
    cutoff, removed = time.time() - days * 86_400, []
    for d in RUNS_DIR.iterdir():
        if not d.is_dir() or not _RUN_ID_RE.match(d.name):
            continue
        try:
            if d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)
                removed.append(d.name)
        except OSError:
            continue
    return removed

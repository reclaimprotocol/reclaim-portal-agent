#!/usr/bin/env python3
"""Verify URLs from the **disabled orgs** and **reportedurl** tabs.

Two questions:
  1. are the DISABLED orgs' current portal URLs actually working logins?
  2. do any STUDENT-REPORTED urls work and give us a url we don't already have?

Each distinct URL is checked once (many orgs share a URL) with the same
review_portal rules used everywhere else — proxy per ccTLD, JS render, junk
filters. Results go to disabled_verify_results.json.

Sharded + resumable: each shard writes its own file; a wedged browser costs one
URL (pre-marked) not the run.
"""
from __future__ import annotations
import argparse, json, os, re, signal, sys, collections
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts"))
try:
    from dotenv import load_dotenv; load_dotenv(ROOT / ".env")
except Exception:  # noqa: BLE001
    pass
import _bootstrap  # noqa: F401,E402
from agent.config import load_config  # noqa: E402
from agent.sheets_client import SheetsClient  # noqa: E402
import agent.magic as M  # noqa: E402
import _run_portals_review as R  # noqa: E402

WORK = Path("/private/tmp/claude-501/-Users-mrunomi-projects-reclaim-portal-agent/"
            "20fa4b23-fcf2-4415-89e2-ac66b5f3f0ab/scratchpad/disabled_work.json")

ap = argparse.ArgumentParser()
ap.add_argument("--shard", type=int, default=0)
ap.add_argument("--of", type=int, default=1)
ap.add_argument("--report-remaining", action="store_true")
a = ap.parse_args()

w = json.loads(WORK.read_text())
known = set(w["known"])
targets: dict = {}          # url -> set(sources)
for o, us in w["disabled"].items():
    for u in us:
        targets.setdefault(u, set()).add("disabled")
for o, us in w["reported"].items():
    for u in us:
        if u in known:      # already ours; nothing to learn
            continue
        targets.setdefault(u, set()).add("reported")
urls = sorted(targets)

OUT = ROOT / (f"disabled_verify_shard{a.shard}.json" if a.of > 1 else "disabled_verify_results.json")
done: dict = {}
for p in ROOT.glob("disabled_verify_*.json"):
    try: done.update(json.loads(p.read_text()))
    except Exception: pass  # noqa: BLE001
mine = json.loads(OUT.read_text()) if OUT.exists() else {}

todo = [u for i, u in enumerate(urls) if i % a.of == a.shard and u not in done]
if a.report_remaining:
    print(len(todo)); sys.exit(0)
print(f"verify shard {a.shard}/{a.of}: {len(todo)} urls (of {len(urls)} total)", flush=True)

signal.signal(signal.SIGALRM, R._on_alarm)
to = int(os.getenv("ROW_TIMEOUT", "70"))
roots = {M._norm_host(u) for u in urls}
for i, u in enumerate(todo, 1):
    mine[u] = {"verdict": "amber", "note": "did not complete (browser hung)",
               "src": sorted(targets[u])}
    OUT.write_text(json.dumps(mine))
    try:
        signal.alarm(to)
        color, note, resolved = R.review_portal(u, roots)
        signal.alarm(0)
    except R._RowTimeout:
        signal.alarm(0); R._reset_renderer(); color, note, resolved = "amber", f"timed out (>{to}s)", None
    except Exception as e:  # noqa: BLE001
        signal.alarm(0); color, note, resolved = "amber", f"error ({type(e).__name__})", None
    mine[u] = {"verdict": color, "note": note, "resolved": resolved or "",
               "src": sorted(targets[u])}
    OUT.write_text(json.dumps(mine))
    print(f"  [{i}/{len(todo)}] [{color}] {u[:66]}", flush=True)
print("DONE", flush=True)

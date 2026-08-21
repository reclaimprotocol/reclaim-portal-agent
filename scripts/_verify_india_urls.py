#!/usr/bin/env python3
"""Second pass for URLs that review_portal skipped as 'Indian university — out of
scope'. These tabs (disabled orgs / reportedurl) are heavily Indian, so that
short-circuit left 632 URLs with no real verdict. Here the India rule is
bypassed — every URL is actually fetched and judged on login merit, per the
standing instruction to review all orgs including Indian ones.
"""
from __future__ import annotations
import argparse, json, os, signal, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts"))
try:
    from dotenv import load_dotenv; load_dotenv(ROOT / ".env")
except Exception:  # noqa: BLE001
    pass
import _bootstrap  # noqa: F401,E402
import agent.magic as M  # noqa: E402
import _run_portals_review as R  # noqa: E402

R._is_indian = lambda *a, **k: False          # the whole point of this pass

ap = argparse.ArgumentParser()
ap.add_argument("--shard", type=int, default=0)
ap.add_argument("--of", type=int, default=1)
ap.add_argument("--report-remaining", action="store_true")
a = ap.parse_args()

src = {}
for p in ROOT.glob("disabled_verify_*.json"):
    src.update(json.loads(p.read_text()))
urls = sorted(u for u, v in src.items() if "Indian university" in (v.get("note") or ""))

OUT = ROOT / (f"india_verify_shard{a.shard}.json" if a.of > 1 else "india_verify_results.json")
done = {}
for p in ROOT.glob("india_verify_*.json"):
    try: done.update(json.loads(p.read_text()))
    except Exception: pass  # noqa: BLE001
mine = json.loads(OUT.read_text()) if OUT.exists() else {}
todo = [u for i, u in enumerate(urls) if i % a.of == a.shard and u not in done]
if a.report_remaining:
    print(len(todo)); sys.exit(0)
print(f"india-pass shard {a.shard}/{a.of}: {len(todo)} of {len(urls)} urls", flush=True)
signal.signal(signal.SIGALRM, R._on_alarm)
to = int(os.getenv("ROW_TIMEOUT", "70"))
roots = {M._norm_host(u) for u in urls}
for i, u in enumerate(todo, 1):
    mine[u] = {"verdict": "amber", "note": "did not complete (browser hung)"}
    OUT.write_text(json.dumps(mine))
    try:
        signal.alarm(to)
        color, note, resolved = R.review_portal(u, roots)
        signal.alarm(0)
    except R._RowTimeout:
        signal.alarm(0); R._reset_renderer(); color, note, resolved = "amber", f"timed out (>{to}s)", None
    except Exception as e:  # noqa: BLE001
        signal.alarm(0); color, note, resolved = "amber", f"error ({type(e).__name__})", None
    mine[u] = {"verdict": color, "note": note, "resolved": resolved or ""}
    OUT.write_text(json.dumps(mine))
    print(f"  [{i}/{len(todo)}] [{color}] {u[:64]}", flush=True)
print("DONE", flush=True)

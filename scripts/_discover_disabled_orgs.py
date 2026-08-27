#!/usr/bin/env python3
"""Run Magic discovery for the **disabled orgs** and keep ONLY portals we have
never had before.

Every discovered URL is checked against the full known universe (every portal
column across all tabs + every batch CSV, ~6.5k URLs). Anything we've already
had is dropped — the point is to find endpoints we've never seen, not to
re-surface the ones that got disabled.

Results -> disabled_discovery_results.json (sharded, resumable).
"""
from __future__ import annotations
import argparse, json, os, re, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts"))
try:
    from dotenv import load_dotenv; load_dotenv(ROOT / ".env")
except Exception:  # noqa: BLE001
    pass
os.environ.setdefault("MAGIC_TNC", "0")
import _bootstrap  # noqa: F401,E402
from agent import magic as G  # noqa: E402

SP = Path("/private/tmp/claude-501/-Users-mrunomi-projects-reclaim-portal-agent/"
          "20fa4b23-fcf2-4415-89e2-ac66b5f3f0ab/scratchpad/disabled_work.json")

ap = argparse.ArgumentParser()
ap.add_argument("--targets", default=str(ROOT / "disabled_discovery_targets.json"))
ap.add_argument("--shard", type=int, default=0)
ap.add_argument("--of", type=int, default=1)
ap.add_argument("--report-remaining", action="store_true")
a = ap.parse_args()

def n(u): return (u or "").strip().rstrip("/").lower()
known = {n(u) for u in json.loads(SP.read_text())["known"]}

targets = json.loads(Path(a.targets).read_text())
OUT = ROOT / (f"disabled_discovery_shard{a.shard}.json" if a.of > 1 else "disabled_discovery_results.json")
done = {}
for p in ROOT.glob("disabled_discovery_shard*.json"):
    try: done.update(json.loads(p.read_text()))
    except Exception: pass  # noqa: BLE001
if (ROOT / "disabled_discovery_results.json").exists():
    try: done.update(json.loads((ROOT / "disabled_discovery_results.json").read_text()))
    except Exception: pass  # noqa: BLE001
mine = json.loads(OUT.read_text()) if OUT.exists() else {}

# shard the FULL list first, then drop completed — sharding a filtered list
# shifts positions between attempts and processes orgs twice (bug hit 2026-08-13)
slice_ = [t for i, t in enumerate(targets) if i % a.of == a.shard]
todo = [t for t in slice_ if t["orgId"] not in done]
if a.report_remaining:
    print(len(todo)); sys.exit(0)

print(f"discovery shard {a.shard}/{a.of}: {len(todo)} orgs", flush=True)
for i, t in enumerate(todo, 1):
    oid, name, web, country = t["orgId"], t["name"], t["website"], t.get("country", "")
    primary = next((d.strip() for d in re.split(r"[,\s]+", web) if d.strip()), "")
    try:
        portals = G.discover(name, primary, country) if primary else []
    except Exception as e:  # noqa: BLE001
        mine[oid] = {"error": type(e).__name__, "new": [], "seen": []}
        OUT.write_text(json.dumps(mine))
        print(f"  [{i}/{len(todo)}] {name[:26]:28} ERROR {type(e).__name__}", flush=True); continue
    fresh = [p["url"] for p in portals if n(p["url"]) not in known]
    seen  = [p["url"] for p in portals if n(p["url"]) in known]
    mine[oid] = {"new": fresh, "seen": seen, "country": country, "name": name}
    OUT.write_text(json.dumps(mine))
    print(f"  [{i}/{len(todo)}] {name[:26]:28} [{country}] {len(portals)} found -> {len(fresh)} NEW", flush=True)
    for u in fresh[:3]:
        print(f"        NEW {u[:80]}", flush=True)
print("DONE", flush=True)

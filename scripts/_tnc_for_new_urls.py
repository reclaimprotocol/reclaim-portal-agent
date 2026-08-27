#!/usr/bin/env python3
"""Find a T&C for each newly-found portal (perorg_new_final.json).

Uses the same waterfall as everywhere else, then validates the result against the
org's own domains / country before accepting it — otherwise the search happily
returns another institution's policy (41 wrong-country T&Cs had to be cleared
this month for exactly that reason).
"""
from __future__ import annotations
import argparse, collections, json, os, re, signal, sys
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts"))
try:
    from dotenv import load_dotenv; load_dotenv(ROOT / ".env")
except Exception:  # noqa: BLE001
    pass
import _bootstrap  # noqa: F401,E402
import agent.magic as M  # noqa: E402
import _run_portals_review as R  # noqa: E402

CC = {"br":"Brazil","ar":"Argentina","mx":"Mexico","cl":"Chile","ph":"Philippines","ng":"Nigeria",
      "pk":"Pakistan","bd":"Bangladesh","lk":"Sri Lanka","bo":"Bolivia","co":"Colombia","ve":"Venezuela",
      "uy":"Uruguay","pe":"Peru","in":"India","do":"Dominican Republic","gt":"Guatemala"}
BADTC = re.compile(r"dublincore|openarchives|w3\.org|freshworks|oracle\.com|linkedin|automattic|"
                   r"sites\.google|drive\.google|amazonaws|website-files|helpjuice|policies\.google", re.I)

ap = argparse.ArgumentParser()
ap.add_argument("--shard", type=int, default=0)
ap.add_argument("--of", type=int, default=1)
ap.add_argument("--report-remaining", action="store_true")
a = ap.parse_args()

items = json.loads((ROOT / "perorg_new_final.json").read_text())
targets = {t["orgId"]: t for t in json.loads((ROOT / "disabled_discovery_targets.json").read_text())}
OUT = ROOT / (f"newurl_tnc_shard{a.shard}.json" if a.of > 1 else "newurl_tnc_results.json")
done = {}
for p in ROOT.glob("newurl_tnc_*.json"):
    try: done.update(json.loads(p.read_text()))
    except Exception: pass  # noqa: BLE001
mine = json.loads(OUT.read_text()) if OUT.exists() else {}
todo = [(i, it) for i, it in enumerate(items) if i % a.of == a.shard and f"{it[0]}|{it[1]}" not in done]
if a.report_remaining:
    print(len(todo)); sys.exit(0)

def root(u):
    try: return M._registrable_root(M._norm_host(u if "://" in u else "http://" + u)) or ""
    except Exception: return ""  # noqa: BLE001
def cc_of(u):
    h = (urlsplit(u if "://" in u else "http://" + u).netloc or "").lower().split(":")[0]
    return CC.get(h.rsplit(".", 1)[-1], "")

print(f"tnc shard {a.shard}/{a.of}: {len(todo)} urls", flush=True)
signal.signal(signal.SIGALRM, R._on_alarm)
to = int(os.getenv("TNC_ROW_TIMEOUT", "90"))
cache: dict = {}
for k, (i, it) in enumerate(todo, 1):
    org, url, name = it[0], it[1], it[2]
    t = targets.get(org, {})
    dom = t.get("website", ""); country = t.get("country", "")
    key = f"{org}|{url}"
    mine[key] = {"tnc": "N/A", "why": "did not complete"}
    OUT.write_text(json.dumps(mine))
    try:
        signal.alarm(to)
        gnew, _tc, tf = R.review_or_fill_tnc("", url, dom, name, cache)
        signal.alarm(0)
    except R._RowTimeout:
        signal.alarm(0); R._reset_renderer(); gnew, tf = "N/A", "timeout"
    except Exception as e:  # noqa: BLE001
        signal.alarm(0); gnew, tf = "N/A", f"error {type(e).__name__}"
    val, why = "N/A", tf
    if str(gnew).lower().startswith("http"):
        own = {root(d) for d in re.split(r"[,\s]+", dom) if d.strip()} | {root(url)}
        tcc = cc_of(gnew)
        if root(gnew) in own:              why, val = "own domain", gnew
        elif BADTC.search(gnew):           why = f"rejected: generic/spec site — {gnew[:50]}"
        elif tcc and country and tcc != country:
            why = f"rejected: wrong country ({tcc} vs {country})"
        else:                              why, val = "vendor/parent (allowed)", gnew
    mine[key] = {"tnc": val, "why": why}
    OUT.write_text(json.dumps(mine))
    print(f"  [{k}/{len(todo)}] {org:10} {val[:52]:54} [{why[:26]}]", flush=True)
print("DONE", flush=True)

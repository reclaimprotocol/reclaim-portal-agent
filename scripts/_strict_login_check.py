#!/usr/bin/env python3
"""Strict pass over candidate URLs: does the page really expose a PASSWORD field
(static or rendered), or is it a login-shaped endpoint?

'login form present' alone is too loose — it fires on any form with 2+ inputs, so
university homepages with a search box score green. Students report homepages, so
that false-green dominates the reported-url candidates.
"""
from __future__ import annotations
import argparse, json, os, re, signal, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts"))
try:
    from dotenv import load_dotenv; load_dotenv(ROOT / ".env")
except Exception:  # noqa: BLE001
    pass
import _bootstrap  # noqa: F401,E402
import _run_portals_review as R  # noqa: E402
from agent.stages.js_renderer import _has_password_input  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--urls-json", required=True)
ap.add_argument("--shard", type=int, default=0)
ap.add_argument("--of", type=int, default=1)
ap.add_argument("--report-remaining", action="store_true")
a = ap.parse_args()
urls = sorted(set(json.loads(Path(a.urls_json).read_text())))
OUT = ROOT / (f"strict_check_shard{a.shard}.json" if a.of > 1 else "strict_check_results.json")
done = {}
for p in ROOT.glob("strict_check_*.json"):
    try: done.update(json.loads(p.read_text()))
    except Exception: pass  # noqa: BLE001
mine = json.loads(OUT.read_text()) if OUT.exists() else {}
todo = [u for i, u in enumerate(urls) if i % a.of == a.shard and u not in done]
if a.report_remaining:
    print(len(todo)); sys.exit(0)
print(f"strict shard {a.shard}/{a.of}: {len(todo)} urls", flush=True)
signal.signal(signal.SIGALRM, R._on_alarm)
to = int(os.getenv("ROW_TIMEOUT", "60"))
for i, u in enumerate(todo, 1):
    mine[u] = {"pwd": False, "how": "did not complete"}
    OUT.write_text(json.dumps(mine))
    pwd, how = False, ""
    try:
        signal.alarm(to)
        st, fin, html = R._fetch(u)
        if _has_password_input(html or ""):
            pwd, how = True, "password field (static)"
        else:
            res = R._render(u); rh = res.html if (res and res.ok) else ""
            if _has_password_input(rh or ""):
                pwd, how = True, "password field (rendered)"
            elif (st and st != 0) and R._LOGIN_URLISH.search(fin or u):
                pwd, how = True, "login-shaped endpoint, reachable"
            else:
                how = f"no password field (status {st})"
        signal.alarm(0)
    except R._RowTimeout:
        signal.alarm(0); R._reset_renderer(); how = "timeout"
    except Exception as e:  # noqa: BLE001
        signal.alarm(0); how = f"error {type(e).__name__}"
    mine[u] = {"pwd": pwd, "how": how}
    OUT.write_text(json.dumps(mine))
    print(f"  [{i}/{len(todo)}] {'PWD ' if pwd else '--- '}{u[:62]}", flush=True)
print("DONE", flush=True)

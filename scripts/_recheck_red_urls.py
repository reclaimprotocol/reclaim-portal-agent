#!/usr/bin/env python3
"""Re-check URLs currently marked NOT WORKING on the 'disabled orgs' tab.

Full waterfall per URL: HTTP fetch -> JS render -> login-link resolution, with
the India out-of-scope short-circuit disabled (these orgs are in scope) and a
longer timeout, since 'unreachable' often just means slow.
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
import agent.magic as M  # noqa: E402
import _run_portals_review as R  # noqa: E402
from agent.stages.js_renderer import _has_password_input, looks_like_block_page  # noqa: E402

R._is_indian = lambda *a, **k: False

ap = argparse.ArgumentParser()
ap.add_argument("--urls-json", required=True)
ap.add_argument("--shard", type=int, default=0)
ap.add_argument("--of", type=int, default=1)
ap.add_argument("--report-remaining", action="store_true")
a = ap.parse_args()
urls = sorted(set(json.loads(Path(a.urls_json).read_text())))
OUT = ROOT / (f"red_recheck_shard{a.shard}.json" if a.of > 1 else "red_recheck_results.json")
done = {}
for p in ROOT.glob("red_recheck_*.json"):
    if p.name.endswith("urls.json") or p.name.endswith("rows.json"):
        continue
    try: done.update(json.loads(p.read_text()))
    except Exception: pass  # noqa: BLE001
mine = json.loads(OUT.read_text()) if OUT.exists() else {}
todo = [u for i, u in enumerate(urls) if i % a.of == a.shard and u not in done]
if a.report_remaining:
    print(len(todo)); sys.exit(0)
print(f"recheck shard {a.shard}/{a.of}: {len(todo)} urls", flush=True)
signal.signal(signal.SIGALRM, R._on_alarm)
to = int(os.getenv("ROW_TIMEOUT", "110"))          # generous: 'unreachable' is often just slow
for i, u in enumerate(todo, 1):
    mine[u] = {"verdict": "amber", "note": "did not complete"}
    OUT.write_text(json.dumps(mine))
    try:
        signal.alarm(to)
        st, fin, html = R._fetch(u)
        pwd = _has_password_input(html or "")
        color = note = None
        if pwd:
            color, note = "green", f"password field (static, status {st})"
        else:
            res = R._render(u); rh = res.html if (res and res.ok) else ""
            if rh and looks_like_block_page(rh):
                color, note = "amber", "WAF/Cloudflare block — verify via local IP"
            elif _has_password_input(rh or ""):
                color, note = "green", "password field (JS-rendered)"
            elif (st and st != 0) and R._LOGIN_URLISH.search(fin or u):
                color, note = "green", f"login-shaped endpoint, reachable ({st})"
            else:
                ep = R._login_endpoint(fin or u, rh or html or "")
                if ep:
                    color, note = "green", f"resolved to login endpoint: {ep[:60]}"
                elif st and st != 0:
                    color, note = "amber", f"reachable (status {st}) but no login form found"
                else:
                    color, note = "red", f"still unreachable (status {st})"
        signal.alarm(0)
    except R._RowTimeout:
        signal.alarm(0); R._reset_renderer(); color, note = "amber", f"timed out (>{to}s)"
    except Exception as e:  # noqa: BLE001
        signal.alarm(0); color, note = "red", f"error {type(e).__name__}"
    mine[u] = {"verdict": color, "note": note}
    OUT.write_text(json.dumps(mine))
    print(f"  [{i}/{len(todo)}] [{color}] {u[:60]}  {note[:44]}", flush=True)
print("DONE", flush=True)

#!/usr/bin/env python3
"""Portal discovery for the **August4000** sheet.

Reads `all orgs` (A Active | B Org ID | C Org Name | D Country | E Org Type |
F Email Domains | …), runs Magic discovery per org with the org's own country so
the region packs and the country-matched proxy exit both activate, then keeps
only URLs that pass a STRICT login test:

    a real type="password" field (static or JS-rendered), or a login-shaped URL
    that answers 2xx/3xx.

Junk classes are rejected outright: admission/applicant portals, library/OPAC,
payment, webmail, logout, staging (qa/dev/test/uat), publisher SSO, social.

Writes `portals` (orgId | portal url | tnc url) — one row per accepted portal,
T&C left N/A for a later waterfall pass. Orgs where nothing survives are
recorded so `all orgs` can be shaded light red.

Sharded + resumable: each shard keeps its own done-file; work is sharded on the
FULL list then filtered, so positions stay stable across attempts.
"""
from __future__ import annotations
import argparse, json, os, re, signal, sys
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts"))
try:
    from dotenv import load_dotenv; load_dotenv(ROOT / ".env")
except Exception:  # noqa: BLE001
    pass
os.environ.setdefault("MAGIC_TNC", "0")
import _bootstrap  # noqa: F401,E402
from agent.config import load_config  # noqa: E402
from agent.sheets_client import SheetsClient  # noqa: E402
from agent import magic as G  # noqa: E402
import _run_portals_review as R  # noqa: E402
from agent.stages.js_renderer import _has_password_input  # noqa: E402

R._is_indian = lambda *a, **k: False        # every org here is in scope

SHEET = "1hDMn93A1xjXVUoK7H9PsNGjpVzZtLc8YXA3_Pfb3Bto"
SRC, OUT = "all orgs", "portals"

JUNK = re.compile(
    r"admission|applicant|ucanapply|/apply|entrance|prospectus|enrollonline|"
    r"opac|koha|pergamum|elibro|/library|biblioteca|digilib|"
    r"feepay|/payment|/pagar|checkout|"
    r"/logout|logout=true|/privacy|/terms|"
    r"webmail|roundcube|/owa|"
    r"elsevier|springer|ioppublishing|ebsco|jstor|turnitin|"
    r"facebook\.com|twitter\.com|linkedin\.com|youtube\.com", re.I)
STAGING = re.compile(r"(?:^|[.-])(uat|qa|test|testing|staging|homolog|dev|sandbox)(?:[.-]|\.)", re.I)

ap = argparse.ArgumentParser()
ap.add_argument("--start-row", type=int, default=2, help="1-based sheet row (row 1 = header)")
ap.add_argument("--count", type=int, default=200)
ap.add_argument("--shard", type=int, default=0)
ap.add_argument("--of", type=int, default=1)
ap.add_argument("--report-remaining", action="store_true")
a = ap.parse_args()

sc = SheetsClient.from_config(load_config()); sc.sheet_id = SHEET
svc = sc._service.spreadsheets()
last = a.start_row + a.count - 1
rows = svc.values().get(spreadsheetId=SHEET,
                        range=f"'{SRC}'!A{a.start_row}:I{last}").execute().get("values", [])
targets = []
for off, r in enumerate(rows):
    r = (r + [""] * 9)[:9]
    oid = (r[1] or "").strip()
    if not oid:
        continue
    targets.append({"row": a.start_row + off, "orgId": oid, "name": (r[2] or "").strip(),
                    "country": (r[3] or "").strip(), "domains": (r[5] or "").strip()})

DONE = ROOT / (f"aug4000_done_shard{a.shard}.json" if a.of > 1 else "aug4000_done.json")
done = {}
for p in ROOT.glob("aug4000_done*.json"):
    try: done.update(json.loads(p.read_text()))
    except Exception: pass  # noqa: BLE001
mine = json.loads(DONE.read_text()) if DONE.exists() else {}

slice_ = [t for i, t in enumerate(targets) if i % a.of == a.shard]
todo = [t for t in slice_ if t["orgId"] not in done]
if a.report_remaining:
    print(len(todo)); sys.exit(0)

def strict_login(u: str):
    """(ok, evidence) — a real password field, or a login-shaped url answering 2xx/3xx."""
    try:
        signal.alarm(int(os.getenv("ROW_TIMEOUT", "75")))
        st, fin, html = R._fetch(u)
        if _has_password_input(html or ""):
            signal.alarm(0); return True, f"password field (static, {st})"
        res = R._render(u); rh = res.html if (res and res.ok) else ""
        if _has_password_input(rh or ""):
            signal.alarm(0); return True, "password field (rendered)"
        if isinstance(st, int) and 200 <= st < 400 and R._LOGIN_URLISH.search(fin or u):
            signal.alarm(0); return True, f"login-shaped endpoint, reachable ({st})"
        signal.alarm(0); return False, f"no login form (status {st})"
    except R._RowTimeout:
        signal.alarm(0); R._reset_renderer(); return False, "timeout"
    except Exception as e:  # noqa: BLE001
        signal.alarm(0); return False, f"error {type(e).__name__}"

signal.signal(signal.SIGALRM, R._on_alarm)
print(f"aug4000 shard {a.shard}/{a.of}: {len(todo)} orgs", flush=True)
for n, t in enumerate(todo, 1):
    oid, name, dom, country = t["orgId"], t["name"], t["domains"], t["country"]
    primary = next((d.strip() for d in re.split(r"[,\s]+", dom) if d.strip()), "")
    if not primary:
        mine[oid] = {"row": t["row"], "portals": [], "why": "no email domain"}
        DONE.write_text(json.dumps(mine))
        print(f"  [{n}/{len(todo)}] {name[:26]:28} SKIP (no domain)", flush=True); continue
    # Pre-mark BEFORE discovery, not just before the per-URL login test. A page
    # that wedges the headless browser survives SIGALRM, so the driver's watchdog
    # SIGKILLs the worker — and with no marker written the org is picked up again
    # on the next attempt and wedges again, forever. Shard 1 of rows 802-1001
    # burned 6 attempts (~45 min) on one Brazilian org this way. Marking first
    # costs that one org instead of the whole shard, and records why so it can be
    # revisited deliberately.
    mine[oid] = {"row": t["row"], "name": name, "country": country, "portals": [],
                 "why": "discovery did not complete (worker wedged) — revisit"}
    DONE.write_text(json.dumps(mine))
    try:
        signal.alarm(int(os.getenv("DISCOVER_TIMEOUT", "300")))
        found = G.discover(name, primary, country) or []
        signal.alarm(0)
    except R._RowTimeout:
        signal.alarm(0); R._reset_renderer()
        mine[oid] = {"row": t["row"], "name": name, "country": country, "portals": [],
                     "why": "discovery timed out"}
        DONE.write_text(json.dumps(mine))
        print(f"  [{n}/{len(todo)}] {name[:26]:28} DISCOVER TIMEOUT", flush=True); continue
    except Exception as e:  # noqa: BLE001
        mine[oid] = {"row": t["row"], "portals": [], "why": f"discover error {type(e).__name__}"}
        DONE.write_text(json.dumps(mine))
        print(f"  [{n}/{len(todo)}] {name[:26]:28} ERROR {type(e).__name__}", flush=True); continue
    kept = []
    for p in found:
        u = p["url"]
        host = (urlsplit(u).netloc or "").lower()
        if JUNK.search(u) or STAGING.search(host):
            continue
        ok, ev = strict_login(u)
        if ok:
            kept.append({"url": u, "evidence": ev, "category": p.get("category", "")})
    mine[oid] = {"row": t["row"], "name": name, "country": country,
                 "portals": kept, "found_raw": len(found)}
    DONE.write_text(json.dumps(mine))
    if kept:
        # One row per portal. T&C column is left EMPTY on purpose — this pass is
        # portals only; a separate waterfall run fills column C later.
        svc.values().append(spreadsheetId=SHEET, range=f"'{OUT}'!A:B", valueInputOption="RAW",
                            insertDataOption="INSERT_ROWS",
                            body={"values": [[oid, k["url"]] for k in kept]}).execute()
    print(f"  [{n}/{len(todo)}] {name[:26]:28} [{country[:10]:10}] {len(found)} found -> {len(kept)} LOGIN", flush=True)
    for k in kept[:3]:
        print(f"        {k['url'][:78]}  [{k['evidence'][:26]}]", flush=True)
print("DONE", flush=True)

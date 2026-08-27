#!/usr/bin/env python3
"""Fill the **Pending928** tab's 'portal url' column (col F).

Two phases, so we don't pay for discovery we've already done:
  --reuse     copy the best known portal from Portals TnC / India Portals /
              Portals for orgs we've already covered (free, instant).
  --discover  run Magic discovery for the rest, using each org's own country
              (col D) so the region packs activate. Sharded + resumable.

Only the FIRST/best portal goes in col F (one row per org). The full portal list
per org is kept in pending928_portals.json so extra ones can be added later.
Indian orgs are INCLUDED here (this is a pending-delivery list, not the 9July
discovery sweep) but reuse from the India Portals tab first.
"""
from __future__ import annotations
import argparse, collections, json, os, re, sys
from pathlib import Path

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

TAB = "Pending928"
COL_PORTAL = "F"
STORE = ROOT / "pending928_portals.json"

ap = argparse.ArgumentParser()
ap.add_argument("--reuse", action="store_true")
ap.add_argument("--discover", action="store_true")
ap.add_argument("--shard", type=int, default=0)
ap.add_argument("--of", type=int, default=1)
ap.add_argument("--report-remaining", action="store_true")
a = ap.parse_args()

sc = SheetsClient.from_config(load_config()); sc.sheet_id = R.SHEET
svc = sc._service.spreadsheets()
pend = [(r + [""] * 7)[:7] for r in
        svc.values().get(spreadsheetId=R.SHEET, range=f"'{TAB}'!A2:G100000").execute().get("values", [])]

def known_portals():
    """orgId -> [portal urls], best-first (working verdicts first)."""
    out = collections.defaultdict(list)
    g = svc.values().get(spreadsheetId=R.SHEET, range=f"'{R.TAB}'!A1:K").execute().get("values", [])
    hdr = g[0]; W = max(len(x) for x in g)
    idx = {(x or "").strip().lower(): i for i, x in enumerate(hdr)}
    O, P, V = idx["organization id"], idx["portal url"], idx["portal human review"]
    GOOD = ("ok —", "resolved to exact", "login on same page", "kept —")
    scored = collections.defaultdict(list)
    for r in g[1:]:
        r = (r + [""] * W)[:W]
        o, p, v = (r[O] or "").strip(), (r[P] or "").strip(), (r[V] or "").strip().lower()
        if o and p and p != "(none found)":
            scored[o].append((0 if any(v.startswith(x) for x in GOOD) else 1, p))
    for o, lst in scored.items():
        out[o] = [p for _, p in sorted(lst)]
    for tab, pcol in (("India Portals", 3), ("Portals", 3)):
        for r in svc.values().get(spreadsheetId=R.SHEET, range=f"'{tab}'!A2:F100000").execute().get("values", []):
            r = (r + [""] * 6)[:6]
            o, p = (r[0] or "").strip(), (r[pcol] or "").strip()
            if o and p and p != "(none found)" and p not in out[o]:
                out[o].append(p)
    return out

store = json.loads(STORE.read_text()) if STORE.exists() else {}

if a.reuse:
    known = known_portals()
    updates, n = [], 0
    for i, r in enumerate(pend, start=2):
        oid = (r[0] or "").strip()
        if not oid or (r[5] or "").strip():
            continue
        if known.get(oid):
            updates.append({"range": f"'{TAB}'!{COL_PORTAL}{i}", "values": [[known[oid][0]]]})
            store.setdefault(oid, {"source": "reused", "portals": known[oid]})
            n += 1
    for j in range(0, len(updates), 500):
        R._retry(lambda: svc.values().batchUpdate(spreadsheetId=R.SHEET,
                 body={"valueInputOption": "RAW", "data": updates[j:j+500]}).execute())
    STORE.write_text(json.dumps(store, indent=1))
    print(f"reuse: filled {n} rows from existing tabs", flush=True)

if a.discover or a.report_remaining:
    todo = [(i, r) for i, r in enumerate(pend, start=2)
            if (r[0] or "").strip() and not (r[5] or "").strip()
            and (r[0] or "").strip() not in store]
    todo = [t for n, t in enumerate(todo) if n % a.of == a.shard]
    if a.report_remaining:
        print(len(todo)); sys.exit(0)
    print(f"discover shard {a.shard}/{a.of}: {len(todo)} orgs", flush=True)
    for n, (row, r) in enumerate(todo, 1):
        oid, name, web, country = (r[0] or "").strip(), (r[1] or "").strip(), (r[2] or "").strip(), (r[3] or "").strip()
        primary = next((d.strip() for d in re.split(r"[,\s]+", web) if d.strip()), "")
        if not primary:
            print(f"  [{n}/{len(todo)}] {name[:26]:26} SKIP (no website)", flush=True); continue
        try:
            portals = G.discover(name, primary, country)
        except Exception as e:  # noqa: BLE001
            print(f"  [{n}/{len(todo)}] {name[:26]:26} ERROR {type(e).__name__}", flush=True); continue
        urls = [p["url"] for p in portals]
        store[oid] = {"source": "discovered", "portals": urls}
        STORE.write_text(json.dumps(store, indent=1))
        if urls:
            R._retry(lambda: svc.values().update(
                spreadsheetId=R.SHEET, range=f"'{TAB}'!{COL_PORTAL}{row}",
                valueInputOption="RAW", body={"values": [[urls[0]]]}).execute())
        print(f"  [{n}/{len(todo)}] {name[:26]:26} [{country}] -> {len(urls)} portals", flush=True)
    print("DONE", flush=True)

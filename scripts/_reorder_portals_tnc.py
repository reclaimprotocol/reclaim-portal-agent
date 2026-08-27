#!/usr/bin/env python3
"""Reorder the **Portals TnC** tab into three org groups:

  1. orgs with a portal AND a T&C
  2. orgs with a portal but NO T&C
  3. orgs with no portal at all ("(none found)" / blank)

Grouping is per ORG — every row of an org stays together, and orgs keep their
existing relative order within a group (stable). Inside an org, rows carrying a
T&C come first. All 8 columns (A-H) move with the row; nothing is dropped.

Snapshots the whole tab before writing.  --dry-run reports the split only.
"""
from __future__ import annotations
import argparse, json, sys, time
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
import _run_portals_review as R  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--dry-run", action="store_true")
a = ap.parse_args()

sc = SheetsClient.from_config(load_config()); sc.sheet_id = R.SHEET
svc = sc._service.spreadsheets()
header = svc.values().get(spreadsheetId=R.SHEET, range=f"'{R.TAB}'!A1:H1").execute().get("values", [[]])[0]
rows = svc.values().get(spreadsheetId=R.SHEET, range=f"'{R.TAB}'!A2:H").execute().get("values", [])
rows = [(r + [""] * 8)[:8] for r in rows]
print(f"read {len(rows)} data rows")

def has_portal(r): 
    p = (r[3] or "").strip()
    return bool(p) and p != "(none found)"
def has_tnc(r):
    return (r[6] or "").strip().lower().startswith("http")

order, by_org = [], {}
for r in rows:
    oid = (r[0] or "").strip()
    if oid not in by_org:
        by_org[oid] = []; order.append(oid)
    by_org[oid].append(r)

g1, g2, g3 = [], [], []
for oid in order:
    rs = by_org[oid]
    # T&C-bearing rows first inside the org, otherwise stable
    rs = sorted(rs, key=lambda r: (not has_tnc(r),))
    if any(has_portal(r) and has_tnc(r) for r in rs):
        g1.append((oid, rs))
    elif any(has_portal(r) for r in rs):
        g2.append((oid, rs))
    else:
        g3.append((oid, rs))

def nrows(g): return sum(len(rs) for _, rs in g)
print(f"\n  1. portal + T&C : {len(g1):5} orgs  {nrows(g1):5} rows")
print(f"  2. portal, no T&C: {len(g2):5} orgs  {nrows(g2):5} rows")
print(f"  3. no portal     : {len(g3):5} orgs  {nrows(g3):5} rows")
out = [r for _, rs in g1 + g2 + g3 for r in rs]
print(f"  total            : {len(g1)+len(g2)+len(g3):5} orgs  {len(out):5} rows")
assert len(out) == len(rows), f"row count changed! {len(out)} vs {len(rows)}"

if a.dry_run:
    print("\ndry-run — no changes"); sys.exit(0)

snap = ROOT / f"portals_tnc_reorder_snapshot_{time.strftime('%Y%m%dT%H%M%SZ')}.json"
snap.write_text(json.dumps({"header": header, "rows": rows}))
print(f"\nsnapshot -> {snap.name}")

svc.values().clear(spreadsheetId=R.SHEET, range=f"'{R.TAB}'!A2:H").execute()
svc.values().update(spreadsheetId=R.SHEET, range=f"'{R.TAB}'!A2",
                    valueInputOption="RAW", body={"values": out}).execute()
print(f"rewrote '{R.TAB}': {len(out)} rows in group order")

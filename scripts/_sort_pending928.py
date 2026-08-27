#!/usr/bin/env python3
"""Sort the **Pending928** tab into three blocks:

  1. portal + T&C   (fully deliverable)
  2. portal, no T&C
  3. no portal at all

One row per org here, so no org-grouping is needed. Existing order is preserved
inside each block (stable), and all columns move with the row. Columns are
resolved from the header, never hardcoded.
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

TAB = "Pending928"
ap = argparse.ArgumentParser()
ap.add_argument("--dry-run", action="store_true")
a = ap.parse_args()

sc = SheetsClient.from_config(load_config()); sc.sheet_id = R.SHEET
svc = sc._service.spreadsheets()
grid = svc.values().get(spreadsheetId=R.SHEET, range=f"'{TAB}'!A1:Z").execute().get("values", [])
header = grid[0]; W = max(len(r) for r in grid)
rows = [(r + [""] * W)[:W] for r in grid[1:]]
def col(n):
    for i, h in enumerate(header):
        if (h or "").strip().lower() == n.lower(): return i
    raise SystemExit(f"column {n!r} not found in {header}")
P, T = col("portal url"), col("tnc url")

def has(v):
    v = (v or "").strip()
    return bool(v) and v.upper() != "N/A"

g1 = [r for r in rows if has(r[P]) and has(r[T])]
g2 = [r for r in rows if has(r[P]) and not has(r[T])]
g3 = [r for r in rows if not has(r[P])]
out = g1 + g2 + g3
print(f"  1. portal + T&C  : {len(g1)}")
print(f"  2. portal, no T&C: {len(g2)}")
print(f"  3. no portal     : {len(g3)}")
print(f"  total            : {len(out)} (was {len(rows)})")
assert len(out) == len(rows), "row count changed!"
if a.dry_run:
    print("dry-run — no changes"); sys.exit(0)

snap = ROOT / f"pending928_sort_snapshot_{time.strftime('%Y%m%dT%H%M%SZ')}.json"
snap.write_text(json.dumps({"header": header, "rows": rows}))
print("snapshot ->", snap.name)
svc.values().clear(spreadsheetId=R.SHEET, range=f"'{TAB}'!A2:Z").execute()
svc.values().update(spreadsheetId=R.SHEET, range=f"'{TAB}'!A2",
                    valueInputOption="RAW", body={"values": out}).execute()
print(f"rewrote {len(out)} rows in block order")

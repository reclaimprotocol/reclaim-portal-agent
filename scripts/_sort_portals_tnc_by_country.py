#!/usr/bin/env python3
"""Sort **Portals TnC** rows by country (col B), alphabetically.

Rows of the same org stay together and keep their internal order; orgs within a
country keep their existing relative order (stable sort), so only the country
grouping changes. Blank-country rows go last. All columns move with the row.

Columns are resolved from the HEADER, not hardcoded — the tab has been
rearranged once already (country inserted at B, T&C Type added at J).

    .venv/bin/python scripts/_sort_portals_tnc_by_country.py --dry-run
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
grid = svc.values().get(spreadsheetId=R.SHEET, range=f"'{R.TAB}'!A1:Z").execute().get("values", [])
header = grid[0]
width = max(len(r) for r in grid)
rows = [(r + [""] * width)[:width] for r in grid[1:]]
print("header:", header)

def col(name, default=None):
    for i, h in enumerate(header):
        if (h or "").strip().lower() == name:
            return i
    if default is None:
        raise SystemExit(f"column {name!r} not found")
    return default

C_ORG, C_CTRY = col("organization id"), col("country")
print(f"orgId=col{C_ORG}  country=col{C_CTRY}  width={width}  data rows={len(rows)}")

order, by_org = [], {}
for r in rows:
    oid = (r[C_ORG] or "").strip()
    if oid not in by_org:
        by_org[oid] = []; order.append(oid)
    by_org[oid].append(r)

def org_country(rs):
    for r in rs:
        c = (r[C_CTRY] or "").strip()
        if c:
            return c
    return ""

# stable: sort only on (blank-last, country); ties keep original org order
keyed = [(org_country(by_org[o]), n, o) for n, o in enumerate(order)]
keyed.sort(key=lambda t: ((t[0] == ""), t[0].lower(), t[1]))
out = [r for _, _, o in keyed for r in by_org[o]]
assert len(out) == len(rows), f"row count changed {len(out)} vs {len(rows)}"

import collections
cc = collections.Counter(org_country(by_org[o]) or "(blank)" for o in order)
print("\norgs per country:")
for c, n in cc.most_common(20):
    print(f"  {n:5}  {c}")
if a.dry_run:
    print("\ndry-run — no changes"); sys.exit(0)

snap = ROOT / f"portals_tnc_countrysort_snapshot_{time.strftime('%Y%m%dT%H%M%SZ')}.json"
snap.write_text(json.dumps({"header": header, "rows": rows}))
print(f"\nsnapshot -> {snap.name}")
svc.values().clear(spreadsheetId=R.SHEET, range=f"'{R.TAB}'!A2:Z").execute()
svc.values().update(spreadsheetId=R.SHEET, range=f"'{R.TAB}'!A2",
                    valueInputOption="RAW", body={"values": out}).execute()
print(f"rewrote {len(out)} rows sorted by country")

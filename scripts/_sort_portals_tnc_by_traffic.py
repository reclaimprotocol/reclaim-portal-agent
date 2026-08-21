#!/usr/bin/env python3
"""Sort **Portals TnC** by country in TRAFFIC order (busiest country first),
then by each org's own 30-day request band within the country.

Country order is computed from 9July: sum of band midpoints per country, so the
sheet leads with Brazil, Philippines, Argentina, Mexico, Chile, Nigeria...
Rows of an org stay together; ties keep the existing order (stable).
Columns are resolved from the header — never hardcoded.
"""
from __future__ import annotations
import argparse, collections, json, sys, time
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

# 'Oct-49' is a spreadsheet date-autocorrect of the band '10-49' (2,199 rows).
MID = {"<10": 5, "10-49": 30, "Oct-49": 30, "50-99": 75, "100-249": 175,
       "250-500": 375, "500-999": 750, ">1000": 1500}
BAND_RANK = {">1000": 0, "500-999": 1, "250-500": 2, "100-249": 3,
             "50-99": 4, "10-49": 5, "Oct-49": 5, "<10": 6}

sc = SheetsClient.from_config(load_config()); sc.sheet_id = R.SHEET
svc = sc._service.spreadsheets()

est = collections.Counter()
for r in svc.values().get(spreadsheetId=R.SHEET, range="'9July'!A2:G100000").execute().get("values", []):
    r = (r + [""] * 7)[:7]
    c, b = (r[3] or "").strip(), (r[6] or "").strip()
    if c and b in MID:
        est[c] += MID[b]
rank = {c: i for i, (c, _) in enumerate(est.most_common())}
print("country traffic order:", [c for c, _ in est.most_common()][:8], "...")

grid = svc.values().get(spreadsheetId=R.SHEET, range=f"'{R.TAB}'!A1:Z").execute().get("values", [])
header = grid[0]
width = max(len(r) for r in grid)
rows = [(r + [""] * width)[:width] for r in grid[1:]]

def col(name):
    for i, h in enumerate(header):
        if (h or "").strip().lower() == name.lower():
            return i
    raise SystemExit(f"column {name!r} not found in {header}")

C_ORG, C_CTRY, C_REQ = col("Organization ID"), col("country"), col("Requests last 30d")
print(f"orgId=col{C_ORG} country=col{C_CTRY} requests=col{C_REQ} | rows={len(rows)}")

order, by_org = [], {}
for r in rows:
    oid = (r[C_ORG] or "").strip()
    if oid not in by_org:
        by_org[oid] = []; order.append(oid)
    by_org[oid].append(r)

def first(rs, i):
    for r in rs:
        if (r[i] or "").strip():
            return (r[i] or "").strip()
    return ""

keyed = []
for n, o in enumerate(order):
    rs = by_org[o]
    c = first(rs, C_CTRY); b = first(rs, C_REQ)
    keyed.append((rank.get(c, 999), BAND_RANK.get(b, 9), n, o))
keyed.sort()
out = [r for _, _, _, o in keyed for r in by_org[o]]
assert len(out) == len(rows), f"row count changed {len(out)} vs {len(rows)}"

seen, blocks = set(), []
for _, _, _, o in keyed:
    c = first(by_org[o], C_CTRY) or "(blank)"
    if c not in seen:
        seen.add(c); blocks.append(c)
print("\nnew country order:", blocks[:12], "...")
if a.dry_run:
    print("dry-run — no changes"); sys.exit(0)

snap = ROOT / f"portals_tnc_trafficsort_snapshot_{time.strftime('%Y%m%dT%H%M%SZ')}.json"
snap.write_text(json.dumps({"header": header, "rows": rows}))
print(f"snapshot -> {snap.name}")
svc.values().clear(spreadsheetId=R.SHEET, range=f"'{R.TAB}'!A2:Z").execute()
svc.values().update(spreadsheetId=R.SHEET, range=f"'{R.TAB}'!A2",
                    valueInputOption="RAW", body={"values": out}).execute()
print(f"rewrote {len(out)} rows: country by traffic, then org band")

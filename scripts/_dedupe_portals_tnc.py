#!/usr/bin/env python3
"""Delete genuinely redundant rows from **Portals TnC**.

Two classes only — anything else is left alone:
  1. EXACT duplicates: same (orgId, portal, T&C). Keeps the best copy.
  2. T&C-less siblings: a row with no T&C whose SAME portal already has a T&C
     on another row (the empty row adds nothing).

NOT touched: same portal with a DIFFERENT T&C — that's the intended one-row-per-
T&C shape (639 such rows), so a naive "dedupe by (org,portal)" would destroy real
data.

"Best copy" = a working-login verdict beats a non-working one, a reviewed row
beats an unreviewed one, then the earliest row wins. Snapshots before deleting.
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

sc = SheetsClient.from_config(load_config()); sc.sheet_id = R.SHEET
svc = sc._service.spreadsheets()
grid = svc.values().get(spreadsheetId=R.SHEET, range=f"'{R.TAB}'!A1:Z").execute().get("values", [])
header = grid[0]; W = max(len(r) for r in grid)
rows = [(r + [""] * W)[:W] for r in grid[1:]]
def col(n):
    for i, h in enumerate(header):
        if (h or "").strip().lower() == n.lower(): return i
    raise SystemExit(f"column {n!r} not found")
O, P, V, T = (col("Organization ID"), col("Portal URL"),
              col("Portal Human review"), col("T&C URL"))

def norm(u): return (u or "").strip().rstrip("/").lower()
GOOD = ("ok —", "resolved to exact", "login on same page", "kept —")
def score(i):
    r = rows[i - 2]; v = (r[V] or "").strip().lower()
    return (0 if any(v.startswith(x) for x in GOOD) else 1, 0 if v else 1, i)

trip, pair = collections.defaultdict(list), collections.defaultdict(list)
for i, r in enumerate(rows, start=2):
    o, p, t = (r[O] or "").strip(), norm(r[P]), norm(r[T])
    if not o or not p or p == "(none found)":
        continue
    trip[(o, p, t)].append(i); pair[(o, p)].append(i)

delete = set()
for k, ix in trip.items():
    if len(ix) > 1:
        keep = min(ix, key=score)
        delete |= {i for i in ix if i != keep}
n_exact = len(delete)
for (o, p), ix in pair.items():
    ts = {i: norm(rows[i - 2][T]) for i in ix}
    if any(t.startswith("http") for t in ts.values()):
        for i, t in ts.items():
            if not t.startswith("http"):
                delete.add(i)
n_na = len(delete) - n_exact
print(f"rows to delete: {len(delete)}  (exact duplicates {n_exact}, T&C-less siblings {n_na})")
print(f"tab rows before: {len(rows)}  ->  after: {len(rows)-len(delete)}")
if a.dry_run:
    for i in sorted(delete)[:10]:
        r = rows[i - 2]
        print(f"   row{i}: {r[O]:10} {(r[P] or '')[:48]:48} tnc={(r[T] or '')[:34]}")
    print("dry-run — nothing deleted"); sys.exit(0)

snap = ROOT / f"portals_tnc_dedupe_snapshot_{time.strftime('%Y%m%dT%H%M%SZ')}.json"
snap.write_text(json.dumps({"header": header,
                            "deleted": [{"row": i, "values": rows[i - 2]} for i in sorted(delete)]}))
print("snapshot ->", snap.name)
gid = next(s["properties"]["sheetId"] for s in svc.get(spreadsheetId=R.SHEET).execute()["sheets"]
           if s["properties"]["title"] == R.TAB)
reqs = [{"deleteDimension": {"range": {"sheetId": gid, "dimension": "ROWS",
                                       "startIndex": i - 1, "endIndex": i}}}
        for i in sorted(delete, reverse=True)]
for i in range(0, len(reqs), 500):
    svc.batchUpdate(spreadsheetId=R.SHEET, body={"requests": reqs[i:i+500]}).execute()
print(f"deleted {len(reqs)} rows")

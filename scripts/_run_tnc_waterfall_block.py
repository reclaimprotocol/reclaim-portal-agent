#!/usr/bin/env python3
"""Run the T&C waterfall over a row range of **Portals TnC** — for orgs that have
a working portal but no T&C yet (col G = N/A / blank).

Reuses _run_portals_review.review_or_fill_tnc, so it applies the same waterfall
(portal host -> linked university -> parent domain) and the same validation as
the review pass, and shares its verdict cache across rows of one org.

Safety, learned from the review runs:
  * pre-marks col H BEFORE fetching, so a page that wedges the headless browser
    costs one row instead of stalling the pass forever (the driver's watchdog
    SIGKILLs it and the next pass skips the row);
  * per-row SIGALRM cap;
  * only touches rows whose G is empty/N/A -> resumable and idempotent.

    .venv/bin/python scripts/_run_tnc_waterfall_block.py --start-row 3679 --end-row 4853
    .venv/bin/python scripts/_run_tnc_waterfall_block.py --report-remaining
"""
from __future__ import annotations
import argparse, os, signal, sys
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
import agent.magic as M  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--start-row", type=int, default=3679)
ap.add_argument("--end-row", type=int, default=4853)
ap.add_argument("--batch", type=int, default=int(os.getenv("TNC_BATCH", "40")))
ap.add_argument("--shard", type=int, default=0)
ap.add_argument("--of", type=int, default=1)
ap.add_argument("--report-remaining", action="store_true")
a = ap.parse_args()

sc = SheetsClient.from_config(load_config()); sc.sheet_id = R.SHEET
svc = sc._service.spreadsheets()
rows = svc.values().get(spreadsheetId=R.SHEET, range=f"'{R.TAB}'!A2:H").execute().get("values", [])

# A row where the waterfall finds nothing keeps G = "N/A", so col G alone can't
# say "already tried" — filtering on it re-queues the same rows forever (the
# driver re-ran the first 40 every invocation until this was fixed). Track
# attempts locally, keyed on (orgId, portal) so the marker survives row moves.
import json  # noqa: E402
DONE_FILE = (ROOT / f"tnc_waterfall_done_shard{a.shard}.json") if a.of > 1 else (ROOT / "tnc_waterfall_done.json")
attempted = set()
for _p in list(ROOT.glob("tnc_waterfall_done*.json")):   # union across shards
    try:
        attempted |= set(json.loads(_p.read_text()))
    except Exception:  # noqa: BLE001
        pass
mine = set(json.loads(DONE_FILE.read_text())) if DONE_FILE.exists() else set()

def _key(r):
    return f"{(r[0] or '').strip()}|{(r[3] or '').strip()}"

todo = []
for i, r in enumerate(rows, start=2):
    if not (a.start_row <= i <= a.end_row):
        continue
    r = (r + [""] * 8)[:8]
    portal = (r[3] or "").strip(); g = (r[6] or "").strip()
    if not portal or portal == "(none found)":
        continue
    if g.lower().startswith("http"):
        continue                      # already has a T&C
    if _key(r) in attempted:
        continue                      # already searched, came back empty
    todo.append((i, r))

groups: dict = {}
for i, r in todo:
    k = f"{(r[0] or '').strip()}|{M._registrable_root(M._norm_host((r[3] or '').strip())) or ''}"
    groups.setdefault(k, []).append((i, r))
gkeys = [k for n, k in enumerate(sorted(groups)) if n % a.of == a.shard]

if a.report_remaining:
    print(sum(len(groups[k]) for k in gkeys)); sys.exit(0)

print(f"T&C waterfall shard {a.shard}/{a.of}: {len(gkeys)} (org,portal-root) groups, "
      f"{sum(len(groups[k]) for k in gkeys)} rows", flush=True)
if a.batch and len(gkeys) > a.batch:
    print(f"(processing {a.batch} groups this invocation; driver resumes)", flush=True)
    gkeys = gkeys[: a.batch]

row_timeout = int(os.getenv("TNC_ROW_TIMEOUT", "90"))
signal.signal(signal.SIGALRM, R._on_alarm)
cache: dict = {}
n_found = 0
for n, gk in enumerate(gkeys, 1):
    members = groups[gk]
    row, r = members[0]
    oid, name, dom, portal, e, cat, g, h = r
    # pre-mark every row of the group so a wedging page can't be retried forever
    R._retry(lambda: svc.values().batchUpdate(spreadsheetId=R.SHEET, body={
        "valueInputOption": "RAW",
        "data": [{"range": f"'{R.TAB}'!H{i}",
                  "values": [["T&C search did not complete (page hung) — verify manually"]]}
                 for i, _ in members]}).execute())
    try:
        signal.alarm(row_timeout)
        gnew, _tc, tf = R.review_or_fill_tnc(g, portal, dom, name, cache)
        signal.alarm(0)
    except R._RowTimeout:
        signal.alarm(0); R._reset_renderer()
        gnew, tf = g or "N/A", f"T&C search timed out (>{row_timeout}s) — N/A"
    except Exception as exc:  # noqa: BLE001
        signal.alarm(0)
        gnew, tf = g or "N/A", f"T&C search error ({type(exc).__name__})"
    # one lookup, applied to every row sharing this org + portal root
    data = []
    for i, _rr in members:
        data.append({"range": f"'{R.TAB}'!H{i}", "values": [[tf]]})
        if gnew and gnew != g:
            data.append({"range": f"'{R.TAB}'!G{i}", "values": [[gnew]]})
    R._retry(lambda: svc.values().batchUpdate(spreadsheetId=R.SHEET,
             body={"valueInputOption": "RAW", "data": data}).execute())
    for _i, rr in members:
        mine.add(_key(rr))
    DONE_FILE.write_text(json.dumps(sorted(mine)))
    hit = str(gnew).lower().startswith("http")
    n_found += hit
    print(f"  [{n}/{len(gkeys)}] row{row} x{len(members)} {name[:20]:20} "
          f"{'T&C ' + str(gnew)[:40] if hit else 'none'}", flush=True)
print(f"DONE ({n_found}/{len(gkeys)} groups got a T&C)", flush=True)

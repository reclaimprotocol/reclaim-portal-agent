#!/usr/bin/env python3
"""Retry portal discovery for Portals TnC orgs whose portals ALL failed re-verify.

Reads the org list from a JSON produced by the re-verify audit (row/orgId/name/
domains/country). Columns are resolved from the header — the tab layout has
changed twice, so nothing is hardcoded.

For each org: run Magic discovery with the org's own country (region packs), then
  * replace the org's FIRST row's portal cell with the best new portal and clear
    its verdict so a review pass picks it up;
  * append any extra portals as new rows;
  * leave the org untouched if discovery still finds nothing.
Resumable: an org that already has a fresh (unreviewed) portal is skipped.
"""
from __future__ import annotations
import argparse, json, os, re, sys
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

ap = argparse.ArgumentParser()
ap.add_argument("--orgs-json", required=True)
ap.add_argument("--shard", type=int, default=0)
ap.add_argument("--of", type=int, default=1)
ap.add_argument("--report-remaining", action="store_true")
a = ap.parse_args()

targets = json.loads(Path(a.orgs_json).read_text())
DONE = ROOT / f"discover_failed_done_shard{a.shard}.json" if a.of > 1 else ROOT / "discover_failed_done.json"
done = set()
for p in ROOT.glob("discover_failed_done*.json"):
    try: done |= set(json.loads(p.read_text()))
    except Exception: pass  # noqa: BLE001
mine = set(json.loads(DONE.read_text())) if DONE.exists() else set()

# Shard on the FULL target list, then drop what's done. Sharding the already-
# filtered list is a bug: as orgs complete, the modulo positions shift and an org
# can move into another shard and be processed twice (it appended 8 duplicate
# rows on the 76-org run). Positions must be stable across attempts.
todo = [t for n, t in enumerate(targets) if n % a.of == a.shard]
todo = [t for t in todo if t["orgId"] not in done]
if a.report_remaining:
    print(len(todo)); sys.exit(0)

sc = SheetsClient.from_config(load_config()); sc.sheet_id = R.SHEET
svc = sc._service.spreadsheets()
grid = svc.values().get(spreadsheetId=R.SHEET, range=f"'{R.TAB}'!A1:Z").execute().get("values", [])
header = grid[0]; W = max(len(r) for r in grid)
rows = [(r + [""] * W)[:W] for r in grid[1:]]
def col(name):
    for i, h in enumerate(header):
        if (h or "").strip().lower() == name.lower(): return i
    raise SystemExit(f"column {name!r} not found")
C_ORG, C_PORTAL, C_VERDICT = col("Organization ID"), col("Portal URL"), col("Portal Human review")
C_CAT = col("Category")
A1 = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"

rowmap = {}
for i, r in enumerate(rows, start=2):
    rowmap.setdefault((r[C_ORG] or "").strip(), []).append(i)

print(f"discovery shard {a.shard}/{a.of}: {len(todo)} orgs", flush=True)
n_found = 0
for n, t in enumerate(todo, 1):
    oid, name, dom, country = t["orgId"], t["name"], t["domains"], t["country"]
    primary = next((d.strip() for d in re.split(r"[,\s]+", dom) if d.strip()), "")
    if not primary:
        print(f"  [{n}/{len(todo)}] {name[:26]:26} SKIP (no domain)", flush=True); continue
    try:
        portals = G.discover(name, primary, country)
    except Exception as e:  # noqa: BLE001
        print(f"  [{n}/{len(todo)}] {name[:26]:26} ERROR {type(e).__name__}", flush=True); continue
    mine.add(oid); DONE.write_text(json.dumps(sorted(mine)))
    if not portals:
        print(f"  [{n}/{len(todo)}] {name[:26]:26} [{country}] -> still none", flush=True); continue
    first_row = min(rowmap.get(oid, [0])) or None
    if first_row:
        R._retry(lambda: svc.values().batchUpdate(spreadsheetId=R.SHEET, body={
            "valueInputOption": "RAW", "data": [
                {"range": f"'{R.TAB}'!{A1[C_PORTAL]}{first_row}", "values": [[portals[0]['url']]]},
                {"range": f"'{R.TAB}'!{A1[C_VERDICT]}{first_row}", "values": [[""]]},
                {"range": f"'{R.TAB}'!{A1[C_CAT]}{first_row}", "values": [[portals[0].get('category','')]]}]}).execute())
    if len(portals) > 1:
        base = rows[first_row - 2] if first_row else [""] * W
        extra = []
        for p in portals[1:]:
            nr = list(base)
            nr[C_PORTAL] = p["url"]; nr[C_VERDICT] = ""; nr[C_CAT] = p.get("category", "")
            extra.append(nr)
        R._retry(lambda: svc.values().append(
            spreadsheetId=R.SHEET, range=f"'{R.TAB}'!A:Z", valueInputOption="RAW",
            insertDataOption="INSERT_ROWS", body={"values": extra}).execute())
    n_found += len(portals)
    print(f"  [{n}/{len(todo)}] {name[:26]:26} [{country}] -> {len(portals)} portals", flush=True)
    for p in portals[:3]:
        print(f"        {p['url'][:82]}", flush=True)
print(f"DONE — {n_found} portals", flush=True)

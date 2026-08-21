#!/usr/bin/env python3
"""Write portals_review_window.json with every org reviewed in this pass.

_prune_dead_portals.py only prunes orgs listed in that cache, and the rolling
review driver clears it on each window advance — so after a multi-window review
the cache holds just the LAST 50 orgs. This rebuilds it to cover the full range
(default: every org with a row at/after --from-row), so the prune sees the whole
reviewed set. Orgs are included whole (all their rows), which the prune needs for
its per-org "does a working portal survive?" decision.
"""
from __future__ import annotations
import argparse, json, sys
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
ap.add_argument("--from-row", type=int, default=3507)
ap.add_argument("--out", default=str(ROOT / "portals_review_window.json"))
a = ap.parse_args()

sc = SheetsClient.from_config(load_config()); sc.sheet_id = R.SHEET
rows = sc._service.spreadsheets().values().get(
    spreadsheetId=R.SHEET, range=f"'{R.TAB}'!A2:H").execute().get("values", [])

orgs, unreviewed = [], 0
seen = set()
for i, r in enumerate(rows, start=2):
    if i < a.from_row:
        continue
    r = (r + [""] * 8)[:8]
    oid = (r[0] or "").strip()
    if not oid:
        continue
    if oid not in seen:
        seen.add(oid); orgs.append(oid)
    portal = (r[3] or "").strip()
    if portal and portal != "(none found)" and not (r[4] or "").strip():
        # Indian orgs are out of scope for the reviewer (_run_portals_review
        # never windows them), so their rows stay permanently unreviewed. Don't
        # count them as outstanding or a waiting prune would never start.
        if not R._is_indian((r[2] or ""), portal):
            unreviewed += 1

Path(a.out).write_text(json.dumps(orgs))
print(f"window cache -> {Path(a.out).name}: {len(orgs)} orgs (rows {a.from_row}+)")
print(f"still-unreviewed rows in range: {unreviewed}")

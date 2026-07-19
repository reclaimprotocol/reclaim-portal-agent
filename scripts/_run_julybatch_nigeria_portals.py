#!/usr/bin/env python3
"""Run Genie's Magic over the JulyBatch **Nigeria** tab and fill the **Portal
URL** column IN-PLACE (one cell per university, newline-joined if several).

  Nigeria: A University name | B Website | C Portal URL

Idempotent / resumable: rows whose Portal URL cell is already non-empty are
skipped, so re-running only processes the rest. Each row is written as soon as
it's discovered, so an interruption never loses completed work. A university
with no portal gets "(none found)". T&C is OFF (separate step).

Usage:
    .venv/bin/python scripts/_run_julybatch_nigeria_portals.py
    .venv/bin/python scripts/_run_julybatch_nigeria_portals.py --limit 2   # smoke test
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts"))
try:
    from dotenv import load_dotenv; load_dotenv(ROOT / ".env")
except Exception:  # noqa: BLE001
    pass

os.environ.setdefault("MAGIC_TNC", "0")  # T&C is a separate step

import _bootstrap  # noqa: F401,E402
from agent.config import load_config  # noqa: E402
from agent.sheets_client import SheetsClient  # noqa: E402
from agent import magic as G  # noqa: E402

SHEET = "1sDK_1VnRHIuUqBComrvwS1JvSmB_l0_4Rsf9rfezFNw"
PORTAL_COL = "C"  # in-place: A University name | B Website | C Portal URL


def _retry(fn, n=4):
    for i in range(n):
        try:
            return fn()
        except Exception:  # noqa: BLE001
            if i == n - 1:
                raise
            time.sleep(2 * (i + 1))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tab", default="Nigeria", help="sheet tab to fill in-place (A name | B website | C portal)")
    ap.add_argument("--country", default="", help="country hint for Magic (defaults to the tab name)")
    ap.add_argument("--limit", type=int, default=0, help="process at most N universities (0 = all remaining)")
    ap.add_argument("--report-remaining", action="store_true",
                    help="print the count of rows still missing a Portal URL and exit")
    args = ap.parse_args()
    tab = args.tab
    country = args.country or args.tab

    cfg = load_config()
    sc = SheetsClient.from_config(cfg)
    sc.sheet_id = SHEET
    svc = sc._service.spreadsheets()

    rows = _retry(lambda: svc.values().get(spreadsheetId=SHEET, range=f"{tab}!A2:C").execute()).get("values", [])

    todo = []
    for i, r in enumerate(rows):
        rownum = i + 2
        name = (r[0].strip() if r and r[0] else "")
        website = (r[1].strip() if len(r) > 1 and r[1] else "")
        portal = (r[2].strip() if len(r) > 2 and r[2] else "")
        if not name or not website or portal:
            continue
        todo.append((rownum, name, website))

    if args.report_remaining:
        print(len(todo))
        return

    if args.limit:
        todo = todo[: args.limit]

    print(f"{tab}: {sum(1 for r in rows if r and r[0].strip())} unis | "
          f"processing {len(todo)} without a Portal URL", flush=True)

    for i, (rownum, name, website) in enumerate(todo, 1):
        try:
            portals = G.discover(name, website, country)
            cell = "\n".join(p["url"] for p in portals) if portals else "(none found)"
            _retry(lambda: svc.values().update(
                spreadsheetId=SHEET, range=f"{tab}!{PORTAL_COL}{rownum}",
                valueInputOption="USER_ENTERED", body={"values": [[cell]]}).execute())
            print(f"  [{i}/{len(todo)}] row{rownum} {name[:32]:32} -> {len(portals)} portals", flush=True)
        except Exception as e:  # noqa: BLE001 — one uni must not kill the batch; leave for retry
            print(f"  [{i}/{len(todo)}] row{rownum} {name[:32]:32} ERROR ({type(e).__name__}: {e})", flush=True)

    print("DONE", flush=True)


if __name__ == "__main__":
    main()

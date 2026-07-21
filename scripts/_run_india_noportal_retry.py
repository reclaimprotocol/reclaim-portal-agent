#!/usr/bin/env python3
"""Fresh (cache-bypassed) Magic retry over the **India No Portals** tab —
the 166 India orgs that came back with no portal. Fills a Portal URL column
in place.

  India No Portals: A orgId | B name | C website | D portal url

A cached re-run would just return the same "(none found)", so this calls
Magic's discovery with use_cache=False to force a fresh harvest+judge with the
current code. Idempotent/resumable: rows whose D cell is filled are skipped.
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
os.environ.setdefault("MAGIC_TNC", "0")

import _bootstrap  # noqa: F401,E402
from _inactive import INACTIVE  # noqa: E402 — org ids to exclude from all runs
from agent.config import load_config  # noqa: E402
from agent.sheets_client import SheetsClient  # noqa: E402
from agent import magic as G  # noqa: E402

SHEET = "1sDK_1VnRHIuUqBComrvwS1JvSmB_l0_4Rsf9rfezFNw"
TAB = "India No Portals"
COUNTRY = "India"
PORTAL_COL = "D"  # A orgId | B name | C website | D portal url


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
    ap.add_argument("--report-remaining", action="store_true")
    args = ap.parse_args()

    cfg = load_config()
    sc = SheetsClient.from_config(cfg)
    sc.sheet_id = SHEET
    svc = sc._service.spreadsheets()

    # ensure header for the portal column
    _retry(lambda: svc.values().update(
        spreadsheetId=SHEET, range=f"'{TAB}'!{PORTAL_COL}1", valueInputOption="RAW",
        body={"values": [["portal url"]]}).execute())

    rows = _retry(lambda: svc.values().get(
        spreadsheetId=SHEET, range=f"'{TAB}'!A2:D").execute()).get("values", [])

    todo = []
    for i, r in enumerate(rows):
        rownum = i + 2
        oid = (r[0].strip() if r and r[0] else "")
        name = (r[1].strip() if len(r) > 1 and r[1] else "")
        website = (r[2].strip() if len(r) > 2 and r[2] else "")
        portal = (r[3].strip() if len(r) > 3 and r[3] else "")
        if not oid or not website or portal or oid in INACTIVE:
            continue
        todo.append((rownum, name or oid, website))

    if args.report_remaining:
        print(len(todo))
        return

    print(f"{TAB}: fresh retry on {len(todo)} orgs", flush=True)
    for i, (rownum, name, website) in enumerate(todo, 1):
        try:
            primary = website.split(",")[0].strip()
            portals, _ = G._discover_once(name, primary, COUNTRY, use_cache=False)
            cell = "\n".join(p["url"] for p in portals) if portals else "(none found)"
            _retry(lambda: svc.values().update(
                spreadsheetId=SHEET, range=f"'{TAB}'!{PORTAL_COL}{rownum}",
                valueInputOption="USER_ENTERED", body={"values": [[cell]]}).execute())
            print(f"  [{i}/{len(todo)}] row{rownum} {name[:32]:32} -> {len(portals)} portals", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"  [{i}/{len(todo)}] row{rownum} {name[:32]:32} ERROR ({type(e).__name__}: {e})", flush=True)

    print("DONE", flush=True)


if __name__ == "__main__":
    main()

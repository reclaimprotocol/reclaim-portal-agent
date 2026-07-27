#!/usr/bin/env python3
"""Fill T&C for the recovered portals on the **India No Portals** tab, in place.

  India No Portals: A orgId | B name | C website | D portal url | E tnc url

Column D holds the recovered portal(s) (newline-joined). For each row we run
magic_tnc.find_tnc on every portal and write the governing T&C URL(s) into
column E (newline-joined, aligned to the portals; "N/A" for a portal with no
T&C, and a single "N/A" for a (none found)/blank portal row).

Idempotent/resumable: rows whose E cell is already filled are skipped. Inactive
org IDs are skipped. Bounded by --end-row (default 68).
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts"))
try:
    from dotenv import load_dotenv; load_dotenv(ROOT / ".env")
except Exception:  # noqa: BLE001
    pass

import _bootstrap  # noqa: F401,E402
from _inactive import INACTIVE  # noqa: E402
from agent.config import load_config  # noqa: E402
from agent.sheets_client import SheetsClient  # noqa: E402
from agent import magic_tnc as T  # noqa: E402

SHEET = "1sDK_1VnRHIuUqBComrvwS1JvSmB_l0_4Rsf9rfezFNw"
TAB = "India No Portals"
COUNTRY = "India"
TNC_COL = "E"


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
    ap.add_argument("--end-row", type=int, default=100000, help="last sheet row to process (inclusive)")
    ap.add_argument("--report-remaining", action="store_true")
    args = ap.parse_args()

    cfg = load_config()
    sc = SheetsClient.from_config(cfg)
    sc.sheet_id = SHEET
    svc = sc._service.spreadsheets()

    _retry(lambda: svc.values().update(
        spreadsheetId=SHEET, range=f"'{TAB}'!{TNC_COL}1", valueInputOption="RAW",
        body={"values": [["tnc url"]]}).execute())

    rows = _retry(lambda: svc.values().get(
        spreadsheetId=SHEET, range=f"'{TAB}'!A2:E{args.end_row}").execute()).get("values", [])

    todo = []
    for i, r in enumerate(rows):
        rownum = i + 2
        oid = (r[0].strip() if r and r[0] else "")
        name = (r[1].strip() if len(r) > 1 and r[1] else "")
        website = (r[2].strip() if len(r) > 2 and r[2] else "")
        portal = (r[3].strip() if len(r) > 3 and r[3] else "")
        tnc = (r[4].strip() if len(r) > 4 and r[4] else "")
        # only rows with a REAL portal and no T&C yet (skip blank/none-found/inactive)
        if not oid or oid in INACTIVE or tnc or not portal or portal == "(none found)":
            continue
        todo.append((rownum, name or oid, website, portal))

    if args.report_remaining:
        print(len(todo))
        return

    print(f"{TAB}: T&C for {len(todo)} rows (up to row {args.end_row})", flush=True)
    cache: dict = {}
    for i, (rownum, name, website, portal) in enumerate(todo, 1):
        try:
            if not portal or portal == "(none found)":
                cell = "N/A"
                nfound = 0
            else:
                uni_domain = next((d.strip() for d in re.split(r"[,\s]+", website) if d.strip()), "")
                lines = []
                nfound = 0
                for p in [x.strip() for x in portal.splitlines() if x.strip()]:
                    res = T.find_tnc(p, uni_domain, name, COUNTRY, cache=cache)
                    items = res.get("tncs") or []
                    if items:
                        lines.append(items[0]["url"]); nfound += 1
                    else:
                        lines.append("N/A")
                cell = "\n".join(lines) if lines else "N/A"
            _retry(lambda: svc.values().update(
                spreadsheetId=SHEET, range=f"'{TAB}'!{TNC_COL}{rownum}",
                valueInputOption="USER_ENTERED", body={"values": [[cell]]}).execute())
            print(f"  [{i}/{len(todo)}] row{rownum} {name[:30]:30} -> {nfound} tnc", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"  [{i}/{len(todo)}] row{rownum} {name[:30]:30} ERROR ({type(e).__name__}: {e})", flush=True)

    print("DONE", flush=True)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Run Genie's Magic over a row range of the JulyBatch **9July** tab and append
discovered student-login portals to the **Portals** tab.

  9July  : A OrgID | B Name | C Email Domains | D Country | ...
  Portals: A OrgID | B Name | C Domains | D Portal URL | E Category

Each org's OWN Country (col D) is passed to Magic so region packs activate.
Indian universities are SKIPPED (already covered in the India Portals tab).
OrgIDs already present in Portals are also skipped. One row per portal; an org
with no portal gets a single "(none found)" row. T&C is OFF (separate step).

Usage:
    # next 10 orgs starting at sheet row 463
    .venv/bin/python scripts/_run_julybatch_9july_portals.py --start-row 463 --count 10
"""
from __future__ import annotations

import argparse
import os
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

os.environ.setdefault("MAGIC_TNC", "0")  # T&C is a separate tab/step

import _bootstrap  # noqa: F401,E402
from agent.config import load_config  # noqa: E402
from agent.sheets_client import SheetsClient  # noqa: E402
from agent import magic as G  # noqa: E402

SHEET = "1sDK_1VnRHIuUqBComrvwS1JvSmB_l0_4Rsf9rfezFNw"
SRC_TAB = "9July"
OUT_TAB = "Portals"


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
    ap.add_argument("--start-row", type=int, default=463, help="1-based sheet row to start at (row 1 = header)")
    ap.add_argument("--count", type=int, default=10, help="how many sheet rows to process")
    ap.add_argument("--report-remaining", action="store_true",
                    help="print how many rows in the range still need portals, then exit")
    args = ap.parse_args()

    cfg = load_config()
    sc = SheetsClient.from_config(cfg)
    sc.sheet_id = SHEET
    svc = sc._service.spreadsheets()

    def append(values):
        _retry(lambda: svc.values().append(
            spreadsheetId=SHEET, range=f"{OUT_TAB}!A:E",
            valueInputOption="USER_ENTERED", insertDataOption="INSERT_ROWS",
            body={"values": values}).execute())

    last = args.start_row + args.count - 1
    rows = _retry(lambda: sc._get_values(SRC_TAB, f"A{args.start_row}:D{last}"))
    done_rows = _retry(lambda: sc._get_values(OUT_TAB, "A2:A100000"))
    done = {(r[0] or "").strip() for r in done_rows if r and (r[0] or "").strip()}

    if args.report_remaining:
        rem = 0
        for r in rows:
            oid = (r[0].strip() if r and r[0] else "")
            country = (r[3].strip() if len(r) > 3 and r[3] else "")
            domains = (r[2].strip() if len(r) > 2 and r[2] else "")
            if oid and country.lower() != "india" and oid not in done and domains.strip():
                rem += 1
        print(rem)
        return

    print(f"9July rows {args.start_row}..{last} | Portals already done: {len(done)}", flush=True)
    n_ok = n_skip_india = n_skip_done = 0
    for off, r in enumerate(rows):
        row_no = args.start_row + off
        oid = (r[0].strip() if r and r[0] else "")
        name = (r[1].strip() if len(r) > 1 and r[1] else "")
        domains = (r[2].strip() if len(r) > 2 and r[2] else "")
        country = (r[3].strip() if len(r) > 3 and r[3] else "")
        if not oid:
            continue
        if country.lower() == "india":
            n_skip_india += 1
            print(f"  row{row_no} {name[:30]:30} SKIP (India)", flush=True)
            continue
        if oid in done:
            n_skip_done += 1
            print(f"  row{row_no} {name[:30]:30} SKIP (already in Portals)", flush=True)
            continue
        primary = next((d.strip() for d in re.split(r"[,\s]+", domains) if d.strip()), "")
        if not primary:
            print(f"  row{row_no} {name[:30]:30} SKIP (no domain)", flush=True)
            continue
        try:
            portals = G.discover(name, primary, country)
            out = ([[oid, name, domains, p["url"], p.get("category", "")] for p in portals]
                   if portals else [[oid, name, domains, "(none found)", ""]])
            append(out)
            done.add(oid)
            n_ok += 1
            print(f"  row{row_no} {name[:30]:30} [{country}] -> {len(portals)} portals", flush=True)
        except Exception as e:  # noqa: BLE001 — one org must not kill the batch
            print(f"  row{row_no} {name[:30]:30} ERROR ({type(e).__name__}: {e})", flush=True)

    print(f"DONE (discovered {n_ok}, skipped india {n_skip_india}, skipped done {n_skip_done})", flush=True)


if __name__ == "__main__":
    main()

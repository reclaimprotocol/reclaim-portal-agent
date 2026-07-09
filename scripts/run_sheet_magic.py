#!/usr/bin/env python3
"""Run the judge agent over a range of universities in a Google Sheet tab and
write the portals it finds to a "<Tab> Portals" tab (one row per portal:
University Name | Website | Portal URL | Category).

The source tab must have columns A=University Name, B=Website (row 1 = header).

Examples:
  # first 10 universities of the Brazil tab
  .venv/bin/python scripts/run_sheet_magic.py --tab Brazil --start 1 --count 10

  # the next 10 (universities 11-20)
  .venv/bin/python scripts/run_sheet_magic.py --tab Mexico --start 11 --count 10

  # a different spreadsheet
  .venv/bin/python scripts/run_sheet_magic.py --tab Brazil --start 1 --count 5 --sheet <SHEET_ID>
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except Exception:  # noqa: BLE001
    pass

import _bootstrap  # noqa: F401,E402
from agent.config import load_config  # noqa: E402
from agent.sheets_client import SheetsClient  # noqa: E402
from agent import magic as G  # noqa: E402

DEFAULT_SHEET = "1_V2-9XVua309pXmVUTpw-k5uPDT1bU2cTlRGk5uWzSs"


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
    ap.add_argument("--tab", required=True, help="source tab (e.g. Brazil)")
    ap.add_argument("--start", type=int, default=1, help="1-based university index to start at")
    ap.add_argument("--count", type=int, default=10, help="how many universities")
    ap.add_argument("--country", default="", help="country hint (defaults to the tab name)")
    ap.add_argument("--sheet", default=DEFAULT_SHEET, help="spreadsheet id")
    ap.add_argument("--out", default="", help="output tab (default '<tab> Portals')")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("agent.global").setLevel(logging.INFO)

    cfg = load_config()
    sc = SheetsClient.from_config(cfg)
    sc.sheet_id = args.sheet
    svc = sc._service.spreadsheets()
    country = args.country or args.tab
    out = args.out or f"{args.tab} Portals"

    def append(values):
        _retry(lambda: svc.values().append(
            spreadsheetId=args.sheet, range=f"{out}!A:D",
            valueInputOption="USER_ENTERED", insertDataOption="INSERT_ROWS",
            body={"values": values}).execute())

    # ensure the output tab exists with a header
    meta = _retry(lambda: svc.get(spreadsheetId=args.sheet).execute())
    titles = [s["properties"]["title"] for s in meta["sheets"]]
    if out not in titles:
        _retry(lambda: svc.batchUpdate(spreadsheetId=args.sheet, body={
            "requests": [{"addSheet": {"properties": {"title": out}}}]}).execute())
        _retry(lambda: svc.values().update(
            spreadsheetId=args.sheet, range=f"{out}!A1:D1", valueInputOption="USER_ENTERED",
            body={"values": [["University Name", "Website", "Portal URL", "Category"]]}).execute())

    # rows: university #N lives on sheet row N+1 (row 1 is the header)
    first, last = args.start + 1, args.start + args.count
    rows = _retry(lambda: sc._get_values(args.tab, f"A{first}:B{last}"))
    print(f"running judge on {len(rows)} unis from {args.tab} (#{args.start}..#{args.start+args.count-1}) -> {out!r}")
    for row in rows:
        name = row[0] if row else ""
        website = row[1] if len(row) > 1 else ""
        if not website:
            continue
        try:
            portals = G.discover(name, website, country)  # Genie's Magic — built-in zero-retry
            append([[name, website, p["url"], p.get("category", "")] for p in portals]
                   if portals else [[name, website, "(none found)", ""]])
            print(f"  {name} -> {len(portals)} portals")
        except Exception as e:  # noqa: BLE001 — one uni failing must not kill the batch
            print(f"  {name} SKIPPED ({type(e).__name__}: {e})")
    print("done")


if __name__ == "__main__":
    main()

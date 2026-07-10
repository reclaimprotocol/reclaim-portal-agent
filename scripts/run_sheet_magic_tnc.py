#!/usr/bin/env python3
"""Backfill Magic T&C for portals already discovered in a sheet's Portals tab.

Reads a portal tab with columns:
  A Organization ID | B Organization Name | C Email Domains | D Portal URL | E Category
and writes three more:
  F T&C URL | G T&C Level | H T&C Type
for every portal row, using agent.magic_tnc.find_tnc (LLM-judge level cascade).
A per-run cache reuses each university/vendor's T&C across its portals.

Usage:
  .venv/bin/python scripts/run_sheet_magic_tnc.py                 # all rows
  .venv/bin/python scripts/run_sheet_magic_tnc.py --start 2 --count 100
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
from agent.config import load_config  # noqa: E402
from agent.sheets_client import SheetsClient  # noqa: E402
from agent import magic_tnc as T  # noqa: E402

DEFAULT_SHEET = "1sDK_1VnRHIuUqBComrvwS1JvSmB_l0_4Rsf9rfezFNw"


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
    ap.add_argument("--sheet", default=DEFAULT_SHEET)
    ap.add_argument("--tab", default="Portals")
    ap.add_argument("--start", type=int, default=2, help="first sheet row (data starts at 2)")
    ap.add_argument("--count", type=int, default=100000)
    args = ap.parse_args()

    cfg = load_config(); sc = SheetsClient.from_config(cfg); sc.sheet_id = args.sheet
    svc = sc._service.spreadsheets()

    # headers for the three new columns
    _retry(lambda: svc.values().update(spreadsheetId=args.sheet, range=f"{args.tab}!F1:H1",
        valueInputOption="USER_ENTERED",
        body={"values": [["T&C URL", "T&C Level", "T&C Type"]]}).execute())

    last = args.start + args.count - 1
    rows = _retry(lambda: sc._get_values(args.tab, f"A{args.start}:E{last}"))
    cache: dict = {}
    for i, r in enumerate(rows):
        row_num = args.start + i
        name = (r[1] if len(r) > 1 else "").strip()
        domains = (r[2] if len(r) > 2 else "").strip()
        portal = (r[3] if len(r) > 3 else "").strip()
        if not portal or portal == "(none found)":
            _retry(lambda rn=row_num: svc.values().update(spreadsheetId=args.sheet,
                range=f"{args.tab}!F{rn}:H{rn}", valueInputOption="USER_ENTERED",
                body={"values": [["N/A", "N/A", ""]]}).execute())
            continue
        uni_domain = next((d.strip() for d in re.split(r"[,\s]+", domains) if d.strip()), "")
        try:
            t = T.find_tnc(portal, uni_domain, name, cache=cache)
        except Exception as e:  # noqa: BLE001
            t = {"tnc_level": f"error:{type(e).__name__}"}
        url = t.get("tnc_url", "") or ("N/A" if t.get("tnc_level") == "N/A" else "")
        _retry(lambda rn=row_num, u=url, t=t: svc.values().update(spreadsheetId=args.sheet,
            range=f"{args.tab}!F{rn}:H{rn}", valueInputOption="USER_ENTERED",
            body={"values": [[u, t.get("tnc_level", ""), t.get("tnc_type", "")]]}).execute())
        print(f"row {row_num} {name[:30]:30} -> {t.get('tnc_level')} {url[:60]}", flush=True)
    print("TNC DONE", flush=True)


if __name__ == "__main__":
    main()

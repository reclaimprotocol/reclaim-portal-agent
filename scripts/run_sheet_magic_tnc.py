#!/usr/bin/env python3
"""Backfill Magic T&C into a Portals tab, ONE ROW PER T&C DOCUMENT.

Source columns (read from the tab):
  A Organization ID | B Organization Name | C Email Domains | D Portal URL | E Category
The tab is rebuilt with:
  A..E (unchanged) | F T&C URL | G T&C Level | H T&C Type
and a portal that has BOTH a Terms page and a Privacy page becomes TWO rows
(same A..E, different F/G/H). Portals with no T&C get one row with N/A.

A per-run cache reuses each university/vendor's T&C across its portals. The
source A..E is snapshotted to /tmp before the tab is cleared, so nothing is lost
if the run is interrupted.

Usage:
  .venv/bin/python scripts/run_sheet_magic_tnc.py            # full rebuild
"""
from __future__ import annotations

import argparse
import json
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
HEADER = ["Organization ID", "Organization Name", "Email Domains",
          "Portal URL", "Category", "T&C URL", "T&C Level", "T&C Type"]


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
    args = ap.parse_args()

    cfg = load_config(); sc = SheetsClient.from_config(cfg); sc.sheet_id = args.sheet
    svc = sc._service.spreadsheets()

    src = _retry(lambda: sc._get_values(args.tab, "A2:E100000"))
    src = [r for r in src if r and (r[0] if r else "").strip()]
    Path("/tmp/tnc_src_snapshot.json").write_text(json.dumps(src))  # safety
    print(f"source portals: {len(src)} (snapshot saved)", flush=True)

    # header + wipe old data, then append rebuilt rows in chunks
    _retry(lambda: svc.values().update(spreadsheetId=args.sheet, range=f"{args.tab}!A1:H1",
        valueInputOption="USER_ENTERED", body={"values": [HEADER]}).execute())
    _retry(lambda: svc.values().clear(spreadsheetId=args.sheet, range=f"{args.tab}!A2:H100000").execute())

    def flush(rows):
        if rows:
            _retry(lambda: svc.values().append(spreadsheetId=args.sheet, range=f"{args.tab}!A:H",
                valueInputOption="USER_ENTERED", insertDataOption="INSERT_ROWS",
                body={"values": rows}).execute())

    cache: dict = {}
    buf: list = []
    for idx, r in enumerate(src):
        oid = (r[0] if r else "").strip()
        name = (r[1] if len(r) > 1 else "").strip()
        domains = (r[2] if len(r) > 2 else "").strip()
        portal = (r[3] if len(r) > 3 else "").strip()
        cat = (r[4] if len(r) > 4 else "").strip()
        base = [oid, name, domains, portal, cat]
        if not portal or portal == "(none found)":
            buf.append(base + ["N/A", "N/A", ""])
        else:
            uni_domain = next((d.strip() for d in re.split(r"[,\s]+", domains) if d.strip()), "")
            try:
                res = T.find_tnc(portal, uni_domain, name, cache=cache)
            except Exception as e:  # noqa: BLE001
                res = {"tncs": [], "tnc_level": f"error:{type(e).__name__}"}
            items = res.get("tncs") or []
            if items:
                for it in items:                       # one row per T&C doc
                    buf.append(base + [it["url"], res.get("tnc_level", ""), it.get("type", "")])
            else:
                buf.append(base + ["N/A", res.get("tnc_level", "N/A"), ""])
            print(f"{idx+1}/{len(src)} {name[:26]:26} {portal[:40]:40} -> "
                  f"{len(items) or 'N/A'} tnc", flush=True)
        if len(buf) >= 20:
            flush(buf); buf = []
    flush(buf)
    print("TNC REBUILD DONE", flush=True)


if __name__ == "__main__":
    main()

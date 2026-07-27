#!/usr/bin/env python3
"""Backfill T&C for the JulyBatch **Portals** tab into the **Portals TnC** tab,
matching its existing 8-column format (one row per T&C document):

  Portals    : A orgId | B name | C domains | D portal url | E category
  Portals TnC: A orgId | B name | C domains | D portal url |
               E Portal Human review | F Category | G T&C URL | H Tnc Human review

For each (org, portal) in Portals not yet in Portals TnC, run
magic_tnc.find_tnc and append one row per T&C doc (Terms + Privacy => 2 rows);
"N/A" in G when none. Human-review columns E and H are left blank. Inactive
orgs are skipped. Resumable/idempotent: (org, portal) pairs already present in
Portals TnC are skipped, and each pair is appended atomically so an interruption
never leaves a half-written pair.
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
SRC = "Portals"
OUT = "Portals TnC"


def _retry(fn, n=4):
    for i in range(n):
        try:
            return fn()
        except Exception:  # noqa: BLE001
            if i == n - 1:
                raise
            time.sleep(2 * (i + 1))


def _todo(svc):
    src = _retry(lambda: svc.values().get(spreadsheetId=SHEET, range=f"'{SRC}'!A2:F").execute()).get("values", [])
    done_rows = _retry(lambda: svc.values().get(spreadsheetId=SHEET, range=f"'{OUT}'!A2:D").execute()).get("values", [])
    done = {((r[0].strip() if r and r[0] else ""), (r[3].strip() if len(r) > 3 and r[3] else ""))
            for r in done_rows if r and r[0]}
    out = []
    for r in src:
        oid = (r[0].strip() if r and r[0] else "")
        portal = (r[3].strip() if len(r) > 3 and r[3] else "")
        if not oid or oid in INACTIVE or not portal or portal == "(none found)":
            continue
        if (oid, portal) in done:
            continue
        out.append((oid,
                    (r[1].strip() if len(r) > 1 and r[1] else ""),
                    (r[2].strip() if len(r) > 2 and r[2] else ""),
                    portal,
                    (r[4].strip() if len(r) > 4 and r[4] else "")))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report-remaining", action="store_true")
    args = ap.parse_args()

    sc = SheetsClient.from_config(load_config()); sc.sheet_id = SHEET
    svc = sc._service.spreadsheets()
    todo = _todo(svc)
    if args.report_remaining:
        print(len(todo)); return

    print(f"Portals TnC backfill: {len(todo)} (org,portal) pairs to process", flush=True)
    cache: dict = {}
    for i, (oid, name, domains, portal, cat) in enumerate(todo, 1):
        uni_domain = next((d.strip() for d in re.split(r"[,\s]+", domains) if d.strip()), "")
        try:
            res = T.find_tnc(portal, uni_domain, name, "", cache=cache)
            items = res.get("tncs") or []
        except Exception as e:  # noqa: BLE001
            items = []
            print(f"      find_tnc error {portal[:45]} ({type(e).__name__})", flush=True)
        if items:
            rows = [[oid, name, domains, portal, "", cat, it["url"], ""] for it in items]
        else:
            rows = [[oid, name, domains, portal, "", cat, "N/A", ""]]
        _retry(lambda: svc.values().append(
            spreadsheetId=SHEET, range=f"'{OUT}'!A:H",
            valueInputOption="USER_ENTERED", insertDataOption="INSERT_ROWS",
            body={"values": rows}).execute())
        if i % 10 == 0 or i == len(todo):
            print(f"  {i}/{len(todo)} {name[:26]:26} -> {len(items) or 'N/A'} tnc", flush=True)

    print("DONE", flush=True)


if __name__ == "__main__":
    main()

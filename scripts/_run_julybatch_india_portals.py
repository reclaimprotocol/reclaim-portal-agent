#!/usr/bin/env python3
"""Run Genie's Magic over the JulyBatch **India** tab and append discovered
student-login portals to the **India Portals** tab, matching its existing
format exactly:

    India        : A OrgID | B Org Name | C Email Domains | D Country
    India Portals: A OrgID | B Org Name | C Email Domains | D Portal URL | E Category

Resumable / idempotent: OrgIDs already present in India Portals are skipped, so
re-running only processes the remaining orgs. One row per discovered portal; an
org with no portal gets a single "(none found)" row. A per-org exception is
logged and the org is left unprocessed (so a later run retries it) rather than
writing a bogus row.

T&C is intentionally OFF here (MAGIC_TNC=0) — the India Portals TnC tab is a
separate backfill step (run_sheet_magic_tnc.py).

Usage:
    .venv/bin/python scripts/_run_julybatch_india_portals.py
    .venv/bin/python scripts/_run_julybatch_india_portals.py --limit 5   # smoke test
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
SRC_TAB = "India"
OUT_TAB = "India Portals"
COUNTRY = "India"


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
    ap.add_argument("--limit", type=int, default=0, help="process at most N orgs (0 = all remaining)")
    ap.add_argument("--report-remaining", action="store_true",
                    help="print how many orgs still need portals, then exit")
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

    src = _retry(lambda: sc._get_values(SRC_TAB, "A2:D100000"))
    done_rows = _retry(lambda: sc._get_values(OUT_TAB, "A2:A100000"))
    done = {(r[0] or "").strip() for r in done_rows if r and (r[0] or "").strip()}

    remaining = []
    for r in src:
        oid = (r[0].strip() if r and r[0] else "")
        if not oid or oid in done:
            continue
        name = (r[1].strip() if len(r) > 1 and r[1] else "")
        domains = (r[2].strip() if len(r) > 2 and r[2] else "")
        remaining.append((oid, name, domains))

    if args.report_remaining:
        print(len(remaining))
        return

    if args.limit:
        remaining = remaining[: args.limit]

    print(f"India tab: {len(src)} orgs | already done: {len(done)} | "
          f"processing: {len(remaining)}", flush=True)

    for i, (oid, name, domains) in enumerate(remaining, 1):
        primary = next((d.strip() for d in re.split(r"[,\s]+", domains) if d.strip()), "")
        if not primary:
            print(f"  [{i}/{len(remaining)}] {name[:30]:30} SKIP (no domain)", flush=True)
            continue
        try:
            portals = G.discover(name, primary, COUNTRY)
            if portals:
                rows = [[oid, name, domains, p["url"], p.get("category", "")] for p in portals]
            else:
                rows = [[oid, name, domains, "(none found)", ""]]
            append(rows)
            done.add(oid)
            print(f"  [{i}/{len(remaining)}] {name[:30]:30} -> {len(portals)} portals", flush=True)
        except Exception as e:  # noqa: BLE001 — one org must not kill the batch; leave it for retry
            print(f"  [{i}/{len(remaining)}] {name[:30]:30} ERROR ({type(e).__name__}: {e})", flush=True)

    print("DONE", flush=True)


if __name__ == "__main__":
    main()

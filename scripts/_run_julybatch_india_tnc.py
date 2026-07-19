#!/usr/bin/env python3
"""Backfill Magic T&C for the JulyBatch **India Portals** tab into the
**India Portals TnC** tab, matching its existing bespoke format:

  India Portals    : A OrgID | B Name | C Domains | D Portal URL | E Category
  India Portals TnC: A OrgID | B Name | C Domains | D Portal URL |
                     E Portal Human review | F Category | G T&C URL | H Tnc Human review

For each portal we run agent.magic_tnc.find_tnc and emit ONE ROW PER T&C
document (Terms + Privacy → two rows). A portal with no T&C gets one row with
G="N/A"; a "(none found)" portal gets one row with F="", G="N/A". The human
review columns E and H are left blank for a human to fill.

Resumable / idempotent: OrgIDs already present in India Portals TnC are skipped,
so re-running only processes the remaining orgs. Rows for an org are appended
atomically (all at once) and the org is only marked done after a successful
write, so a transient failure leaves it for the next run.

Usage:
    .venv/bin/python scripts/_run_julybatch_india_tnc.py
    .venv/bin/python scripts/_run_julybatch_india_tnc.py --limit 2   # smoke test
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

SHEET = "1sDK_1VnRHIuUqBComrvwS1JvSmB_l0_4Rsf9rfezFNw"
SRC_TAB = "India Portals"
OUT_TAB = "India Portals TnC"
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
                    help="print how many orgs still need T&C, then exit")
    args = ap.parse_args()

    cfg = load_config()
    sc = SheetsClient.from_config(cfg)
    sc.sheet_id = SHEET
    svc = sc._service.spreadsheets()

    def append(values):
        _retry(lambda: svc.values().append(
            spreadsheetId=SHEET, range=f"{OUT_TAB}!A:H",
            valueInputOption="USER_ENTERED", insertDataOption="INSERT_ROWS",
            body={"values": values}).execute())

    src = _retry(lambda: sc._get_values(SRC_TAB, "A2:E100000"))
    done_rows = _retry(lambda: sc._get_values(OUT_TAB, "A2:A100000"))
    done = {(r[0] or "").strip() for r in done_rows if r and (r[0] or "").strip()}

    # Group India Portals rows by OrgID, preserving order.
    orgs: list[str] = []
    by_org: dict[str, dict] = {}
    for r in src:
        oid = (r[0].strip() if r and r[0] else "")
        if not oid:
            continue
        if oid not in by_org:
            by_org[oid] = {"name": (r[1].strip() if len(r) > 1 and r[1] else ""),
                           "domains": (r[2].strip() if len(r) > 2 and r[2] else ""),
                           "portals": []}
            orgs.append(oid)
        by_org[oid]["portals"].append((
            (r[3].strip() if len(r) > 3 and r[3] else ""),   # portal url
            (r[4].strip() if len(r) > 4 and r[4] else ""),   # category
        ))

    remaining = [o for o in orgs if o not in done]
    if args.report_remaining:
        print(len(remaining))
        return
    if args.limit:
        remaining = remaining[: args.limit]

    print(f"India Portals orgs: {len(orgs)} | tnc already done: {len(done)} | "
          f"processing: {len(remaining)}", flush=True)

    cache: dict = {}  # per-run uni/vendor T&C memo, reused across all orgs
    for i, oid in enumerate(remaining, 1):
        info = by_org[oid]
        name, domains, portals = info["name"], info["domains"], info["portals"]
        uni_domain = next((d.strip() for d in re.split(r"[,\s]+", domains) if d.strip()), "")
        out_rows: list[list[str]] = []
        n_tnc = 0
        for portal, cat in portals:
            if not portal or portal == "(none found)":
                out_rows.append([oid, name, domains, portal or "(none found)", "", "", "N/A", ""])
                continue
            try:
                res = T.find_tnc(portal, uni_domain, name, COUNTRY, cache=cache)
                items = res.get("tncs") or []
            except Exception as e:  # noqa: BLE001 — one portal must not lose the org
                items = []
                print(f"      find_tnc error on {portal[:50]} ({type(e).__name__}: {e})", flush=True)
            if items:
                for it in items:
                    out_rows.append([oid, name, domains, portal, "", cat, it["url"], ""])
                    n_tnc += 1
            else:
                out_rows.append([oid, name, domains, portal, "", cat, "N/A", ""])
        try:
            append(out_rows)
            done.add(oid)
            print(f"  [{i}/{len(remaining)}] {name[:30]:30} {len(portals)} portals -> "
                  f"{len(out_rows)} rows, {n_tnc} tnc", flush=True)
        except Exception as e:  # noqa: BLE001 — leave org for retry on write failure
            print(f"  [{i}/{len(remaining)}] {name[:30]:30} WRITE-ERROR "
                  f"({type(e).__name__}: {e})", flush=True)

    print("DONE", flush=True)


if __name__ == "__main__":
    main()

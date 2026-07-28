#!/usr/bin/env python3
"""Retry T&C (waterfall) for the N/A rows in **Portals TnC** and upgrade them
in place when a governing policy is now found.

Portals TnC: A oid | B name | C domains | D portal | E rev | F cat | G tnc | H rev

For each row whose G == "N/A" we re-run magic_tnc.find_tnc — which cascades the
FULL waterfall (exact page -> parent paths -> portal-root homepage -> university
homepage -> LLM search, stop-at-first-hit). If a T&C is found, the row's G is
updated to the first doc and any extra docs (Terms + Privacy) are appended as
new rows; still-nothing stays N/A. find_tnc's built-in _is_valid_tnc keeps junk
out. Inactive orgs are skipped.

Resumable / terminating: a local cache records every (org,portal) already
retried this pass, so the run stops once each N/A has been re-tried once (it
does NOT loop forever on permanent N/As). --report-remaining prints how many
N/A rows are still un-retried.
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
from _inactive import INACTIVE  # noqa: E402
from agent.config import load_config  # noqa: E402
from agent.sheets_client import SheetsClient  # noqa: E402
from agent import magic_tnc as T  # noqa: E402

SHEET = "1sDK_1VnRHIuUqBComrvwS1JvSmB_l0_4Rsf9rfezFNw"
TAB = "Portals TnC"
CACHE = ROOT / "portals_tnc_na_retry_cache.json"


def _retry(fn, n=4):
    for i in range(n):
        try:
            return fn()
        except Exception:  # noqa: BLE001
            if i == n - 1:
                raise
            time.sleep(2 * (i + 1))


def _load_cache() -> set:
    if CACHE.exists():
        try:
            return set(json.loads(CACHE.read_text()))
        except Exception:  # noqa: BLE001
            return set()
    return set()


def _na_rows(svc):
    rows = _retry(lambda: svc.values().get(spreadsheetId=SHEET, range=f"'{TAB}'!A2:H").execute()).get("values", [])
    out = []
    for i, r in enumerate(rows):
        oid = (r[0].strip() if r and r[0] else "")
        g = (r[6].strip() if len(r) > 6 and r[6] else "")
        if oid and oid not in INACTIVE and g.upper() == "N/A":
            out.append({
                "row": i + 2, "oid": oid,
                "name": (r[1].strip() if len(r) > 1 and r[1] else ""),
                "domains": (r[2].strip() if len(r) > 2 and r[2] else ""),
                "portal": (r[3].strip() if len(r) > 3 and r[3] else ""),
                "cat": (r[5].strip() if len(r) > 5 and r[5] else ""),
            })
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report-remaining", action="store_true")
    args = ap.parse_args()

    sc = SheetsClient.from_config(load_config()); sc.sheet_id = SHEET
    svc = sc._service.spreadsheets()
    cache = _load_cache()
    na = _na_rows(svc)
    todo = [x for x in na if f"{x['oid']}|{x['portal']}" not in cache]

    if args.report_remaining:
        print(len(todo)); return

    print(f"Portals TnC N/A retry: {len(na)} N/A rows | {len(todo)} un-retried this pass", flush=True)
    tcache: dict = {}   # per-run uni/vendor T&C memo
    found = 0
    for i, x in enumerate(todo, 1):
        key = f"{x['oid']}|{x['portal']}"
        uni_domain = next((d.strip() for d in re.split(r"[,\s]+", x["domains"]) if d.strip()), "")
        items = []
        try:
            res = T.find_tnc(x["portal"], uni_domain, x["name"], "", cache=tcache)
            items = res.get("tncs") or []
        except Exception as e:  # noqa: BLE001
            print(f"      find_tnc error {x['portal'][:40]} ({type(e).__name__})", flush=True)
        if items:
            found += 1
            # upgrade the N/A row's G to the first doc
            _retry(lambda: svc.values().update(
                spreadsheetId=SHEET, range=f"'{TAB}'!G{x['row']}",
                valueInputOption="USER_ENTERED", body={"values": [[items[0]["url"]]]}).execute())
            # append any additional docs as new rows
            extra = [[x["oid"], x["name"], x["domains"], x["portal"], "", x["cat"], it["url"], ""]
                     for it in items[1:]]
            if extra:
                _retry(lambda: svc.values().append(
                    spreadsheetId=SHEET, range=f"'{TAB}'!A:H",
                    valueInputOption="USER_ENTERED", insertDataOption="INSERT_ROWS",
                    body={"values": extra}).execute())
            print(f"  [{i}/{len(todo)}] {x['name'][:26]:26} -> FOUND {items[0]['url'][:50]}", flush=True)
        cache.add(key)
        if i % 20 == 0 or i == len(todo):
            CACHE.write_text(json.dumps(sorted(cache)))
            print(f"  ...{i}/{len(todo)} retried, {found} recovered", flush=True)
    CACHE.write_text(json.dumps(sorted(cache)))
    print(f"DONE: retried {len(todo)}, recovered {found} T&C", flush=True)


if __name__ == "__main__":
    main()

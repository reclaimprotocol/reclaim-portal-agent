#!/usr/bin/env python3
"""Export noLogin -> 26June.csv with columns orgId, urls.

urls = T&C URLs from level columns C..H. Deduped: each (orgId, url) pair once.
'n/a'/blank and the login Portal (col B) are ignored.
"""
from __future__ import annotations

import csv

import _bootstrap  # noqa: F401

from agent.config import load_config
from agent.sheets_client import SheetsClient
from run_portal_sheet import PORTAL_SHEET_ID

TNC_COLS = range(2, 8)  # C..H
OUT = "26June.csv"


def _norm(s) -> str:
    return str(s or "").strip()


config = load_config()
sheets = SheetsClient(
    sheet_id=PORTAL_SHEET_ID, universities_tab="x", portals_tab="x",
    credentials_path=config.google_credentials_path, token_path=config.google_token_path,
)
rows = sheets._get_values("'noLogin'", "3:100000")  # data row 3+

pairs, seen, orgs = [], set(), set()
for r in rows:
    orgid = _norm(r[0]) if r else ""
    if not orgid:
        continue
    for i in TNC_COLS:
        u = _norm(r[i]) if len(r) > i else ""
        if not u.lower().startswith("http"):
            continue
        key = (orgid, u)
        if key in seen:
            continue
        seen.add(key)
        orgs.add(orgid)
        pairs.append(key)

pairs.sort(key=lambda p: ((0, int(p[0])) if p[0].isdigit() else (1, p[0]), p[1]))

with open(OUT, "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["orgId", "urls"])
    w.writerows(pairs)

print(f"Data rows scanned         : {len(rows)}")
print(f"Distinct orgs with a T&C  : {len(orgs)}")
print(f"Distinct (orgId, url) rows: {len(pairs)}  -> {OUT}")

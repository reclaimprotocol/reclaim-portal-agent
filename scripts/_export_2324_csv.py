#!/usr/bin/env python3
"""Export 23-24June -> 23-24June.csv with columns orgid, tnc_url.

T&C URLs are taken from the level columns C..H. Output is deduped: each
(orgid, tnc_url) pair appears once; an orgid repeats once per DISTINCT T&C URL.
'n/a' / blank cells and the login Portal (col B) are ignored.
"""
from __future__ import annotations

import csv

import _bootstrap  # noqa: F401

from agent.config import load_config
from agent.sheets_client import SheetsClient
from _run_dated_tab_portals import _resolve_tab_title
from run_portal_sheet import PORTAL_SHEET_ID

TNC_COLS = range(2, 8)  # C..H (0-based)
OUT = "23-24June.csv"


def _norm(s) -> str:
    return str(s or "").strip()


config = load_config()
sheets = SheetsClient(
    sheet_id=PORTAL_SHEET_ID, universities_tab="x", portals_tab="x",
    credentials_path=config.google_credentials_path, token_path=config.google_token_path,
)
q = f"'{_resolve_tab_title(sheets, '23-24June')}'"
rows = sheets._get_values(q, "3:100000")  # data starts row 3

pairs = []          # ordered unique (orgid, tnc_url)
seen = set()
orgs = set()
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

print(f"Data rows scanned          : {len(rows)}")
print(f"Distinct orgids            : {len(orgs)}")
print(f"Distinct (orgid, tnc_url)  : {len(pairs)}  -> rows in {OUT}")
print(f"Wrote {OUT}")

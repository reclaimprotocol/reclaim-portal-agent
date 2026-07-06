#!/usr/bin/env python3
"""Export every 'Yes'-marked org in FinalActivationSheet to activation_sheet.csv.

Columns: orgid, approved_url   (approved_url = the login/portal URL, col C).
One row per UNIQUE (orgid, login-url); orgid repeats when an org has several
distinct login portals. Also prints the unique-org count.
"""
from __future__ import annotations

import csv

import _bootstrap  # noqa: F401

from agent.config import load_config
from agent.sheets_client import SheetsClient
from _run_dated_tab_portals import _resolve_tab_title
from run_portal_sheet import PORTAL_SHEET_ID

SHEERID_COL, ORGID_COL, PORTAL_COL = 0, 1, 2
OUT = "activation_sheet.csv"


def _norm(s) -> str:
    return str(s or "").strip()


config = load_config()
sheets = SheetsClient(
    sheet_id=PORTAL_SHEET_ID, universities_tab="x", portals_tab="x",
    credentials_path=config.google_credentials_path, token_path=config.google_token_path,
)
final_q = f"'{_resolve_tab_title(sheets, 'FinalActivationSheet')}'"
rows = sheets._get_values(final_q, "3:100000")

pairs = []          # ordered unique (orgid, login_url)
seen = set()
orgs = set()
yes_row_count = 0
for r in rows:
    if _norm(r[SHEERID_COL]).lower() != "yes":
        continue
    yes_row_count += 1
    orgid = _norm(r[ORGID_COL]) if len(r) > ORGID_COL else ""
    portal = _norm(r[PORTAL_COL]) if len(r) > PORTAL_COL else ""
    if not orgid or not portal:
        continue
    orgs.add(orgid)
    key = (orgid, portal)
    if key in seen:
        continue
    seen.add(key)
    pairs.append(key)

# sort: group by org (numeric where possible), then url
def _sk(p):
    o = p[0]
    return (0, int(o)) if o.isdigit() else (1, o), p[1]


pairs.sort(key=lambda p: ((0, int(p[0])) if p[0].isdigit() else (1, p[0]), p[1]))

with open(OUT, "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["orgid", "approved_url"])
    w.writerows(pairs)

print(f"'Yes' rows in FinalActivationSheet : {yes_row_count}")
print(f"Unique orgs marked 'Yes'           : {len(orgs)}")
print(f"Unique (orgid, login-url) pairs     : {len(pairs)}  -> rows in {OUT}")
print(f"Wrote {OUT}")

#!/usr/bin/env python3
"""Cross-verify ApprovedbyUrl <-> FinalActivationSheet are in sync.

Replays the activation rule (rows 2..40 = portal match on col C; rows 41+ =
T&C-URL match across cols D..I, expanded to the full org+portal block) and
checks the actual sheet state:
  1. every approved pair resolves to >=1 FinalActivationSheet row
  2. every expected row has "Yes" in col A
  3. every expected row is shaded light green (col A background)
  4. any "Yes"/green row NOT backed by an approved pair (orphan) is flagged
"""
from __future__ import annotations

import _bootstrap  # noqa: F401

from agent.config import load_config
from agent.sheets_client import SheetsClient
from _run_dated_tab_portals import _resolve_tab_title
from run_portal_sheet import PORTAL_SHEET_ID

ORGID_COL, PORTAL_COL = 1, 2
TNC_COLS = range(3, 9)
PORTAL_MODE_LAST_ROW = 40  # ApprovedbyUrl rows 2..40 are login-portal URLs
GREEN = (0.7843, 0.9215, 0.7843)


def _norm(s) -> str:
    return str(s or "").strip()


def _close(c, target, eps=0.02) -> bool:
    if not c:
        return False
    return (abs(c.get("red", 0) - target[0]) < eps
            and abs(c.get("green", 0) - target[1]) < eps
            and abs(c.get("blue", 0) - target[2]) < eps)


config = load_config()
sheets = SheetsClient(
    sheet_id=PORTAL_SHEET_ID, universities_tab="x", portals_tab="x",
    credentials_path=config.google_credentials_path, token_path=config.google_token_path,
)
appr_q = f"'{_resolve_tab_title(sheets, 'ApprovedbyUrl')}'"
final_title = _resolve_tab_title(sheets, "FinalActivationSheet")
final_q = f"'{final_title}'"

# --- FinalActivationSheet rows + portal-block index ---
final_rows = sheets._get_values(final_q, "3:100000")
block_of_row, rows_of_block = {}, {}
tnc_index, portal_index = {}, {}   # (org,url)->rows
yes_rows = set()
for ridx, r in enumerate(final_rows):
    row = ridx + 3
    org = _norm(r[ORGID_COL]) if len(r) > ORGID_COL else ""
    portal = _norm(r[PORTAL_COL]) if len(r) > PORTAL_COL else ""
    if _norm(r[0]).lower() == "yes":
        yes_rows.add(row)
    if not org or not portal:
        continue
    block_of_row[row] = (org, portal)
    rows_of_block.setdefault((org, portal), []).append(row)
    portal_index.setdefault((org, portal), []).append(row)
    for i in TNC_COLS:
        u = _norm(r[i]) if len(r) > i else ""
        if u.lower().startswith("http"):
            tnc_index.setdefault((org, u), []).append(row)

# --- approved pairs ---
appr_rows = sheets._get_values(appr_q, "2:100000")
expected = set()
unmatched = []
for ridx, r in enumerate(appr_rows):
    appr_row = ridx + 2
    org = _norm(r[ORGID_COL]) if len(r) > ORGID_COL else ""
    url = _norm(r[PORTAL_COL]) if len(r) > PORTAL_COL else ""
    if not org or not url:
        continue
    if appr_row <= PORTAL_MODE_LAST_ROW:
        hits = portal_index.get((org, url), [])
    else:
        hits = tnc_index.get((org, url), [])
    if not hits:
        unmatched.append((appr_row, org, url))
        continue
    for h in hits:                       # expand to full portal block
        expected.update(rows_of_block.get(block_of_row.get(h), [h]))

# --- background colors of col A for all data rows ---
resp = sheets._service.spreadsheets().get(
    spreadsheetId=PORTAL_SHEET_ID, ranges=[f"{final_q}!A3:A{len(final_rows)+2}"],
    includeGridData=True,
    fields="sheets.data.rowData.values.userEnteredFormat.backgroundColor",
).execute()
green_rows = set()
gd = resp["sheets"][0].get("data", [{}])[0].get("rowData", [])
for ridx, rd in enumerate(gd):
    vals = rd.get("values") or [{}]
    bg = vals[0].get("userEnteredFormat", {}).get("backgroundColor")
    if _close(bg, GREEN):
        green_rows.add(ridx + 3)

# --- reconcile ---
missing_yes = sorted(expected - yes_rows)
missing_green = sorted(expected - green_rows)
orphan_yes = sorted(yes_rows - expected)
orphan_green = sorted(green_rows - expected)

print("=" * 70)
print(f"  FinalActivationSheet data rows : {len(final_rows)}")
print(f"  Expected (approved) rows       : {len(expected)}")
print(f"  Actual 'Yes' rows              : {len(yes_rows)}")
print(f"  Actual green rows              : {len(green_rows)}")
print("=" * 70)
print(f"  Approved pairs with NO match   : {len(unmatched)}")
for ar, org, url in unmatched:
    print(f"     appr-row {ar}  org {org}  {url}")
print(f"  Expected rows MISSING 'Yes'    : {len(missing_yes)}  {missing_yes[:30]}")
print(f"  Expected rows MISSING green    : {len(missing_green)}  {missing_green[:30]}")
print(f"  Orphan 'Yes' (not approved)    : {len(orphan_yes)}  {orphan_yes[:30]}")
print(f"  Orphan green (not approved)    : {len(orphan_green)}  {orphan_green[:30]}")
print("-" * 70)
if not missing_yes and not missing_green:
    print("SYNCED: every approved pair is reflected (Yes + green) in FinalActivationSheet.")
else:
    print("NOT fully synced — see missing rows above.")

#!/usr/bin/env python3
"""False-positive audit: verify NO row was marked Yes/green without a real
approved T&C match in its portal block.

For every actually-green row in FinalActivationSheet, classify it:
  DIRECT  - the row itself contains an approved T&C URL (tnc) / is an approved
            portal (rows 2..40 portal mode)
  SIBLING - row shares (org, portal) with a DIRECT row (legit multi-T&C block)
  UNEXPLAINED - neither -> FALSE POSITIVE (should not be green)
"""
from __future__ import annotations

import _bootstrap  # noqa: F401

from agent.config import load_config
from agent.sheets_client import SheetsClient
from _run_dated_tab_portals import _resolve_tab_title
from run_portal_sheet import PORTAL_SHEET_ID

ORGID_COL, PORTAL_COL = 1, 2
TNC_COLS = range(3, 9)
PORTAL_MODE_LAST_ROW = 40
GREEN = (0.7843, 0.9215, 0.7843)


def _norm(s) -> str:
    return str(s or "").strip()


def _close(c, t, eps=0.02) -> bool:
    return bool(c) and all(abs(c.get(k, 0) - v) < eps for k, v in zip(("red", "green", "blue"), t))


config = load_config()
sheets = SheetsClient(
    sheet_id=PORTAL_SHEET_ID, universities_tab="x", portals_tab="x",
    credentials_path=config.google_credentials_path, token_path=config.google_token_path,
)
appr_q = f"'{_resolve_tab_title(sheets, 'ApprovedbyUrl')}'"
final_q = f"'{_resolve_tab_title(sheets, 'FinalActivationSheet')}'"

final_rows = sheets._get_values(final_q, "3:100000")
block_of_row, rows_of_block, tnc_index, portal_index = {}, {}, {}, {}
for ridx, r in enumerate(final_rows):
    row = ridx + 3
    org = _norm(r[ORGID_COL]) if len(r) > ORGID_COL else ""
    portal = _norm(r[PORTAL_COL]) if len(r) > PORTAL_COL else ""
    if not org or not portal:
        continue
    block_of_row[row] = (org, portal)
    rows_of_block.setdefault((org, portal), []).append(row)
    portal_index.setdefault((org, portal), []).append(row)
    for i in TNC_COLS:
        u = _norm(r[i]) if len(r) > i else ""
        if u.lower().startswith("http"):
            tnc_index.setdefault((org, u), []).append(row)

# DIRECT rows + the set of blocks that contain a direct hit
appr_rows = sheets._get_values(appr_q, "2:100000")
direct_rows, direct_blocks = set(), set()
for ridx, r in enumerate(appr_rows):
    appr_row = ridx + 2
    org = _norm(r[ORGID_COL]) if len(r) > ORGID_COL else ""
    url = _norm(r[PORTAL_COL]) if len(r) > PORTAL_COL else ""
    if not org or not url:
        continue
    hits = portal_index.get((org, url), []) if appr_row <= PORTAL_MODE_LAST_ROW else tnc_index.get((org, url), [])
    for h in hits:
        direct_rows.add(h)
        direct_blocks.add(block_of_row.get(h))

# actual green rows from the sheet
resp = sheets._service.spreadsheets().get(
    spreadsheetId=PORTAL_SHEET_ID, ranges=[f"{final_q}!A3:A{len(final_rows)+2}"],
    includeGridData=True,
    fields="sheets.data.rowData.values.userEnteredFormat.backgroundColor",
).execute()
gd = resp["sheets"][0].get("data", [{}])[0].get("rowData", [])
green_rows = {ridx + 3 for ridx, rd in enumerate(gd)
              if _close((rd.get("values") or [{}])[0].get("userEnteredFormat", {}).get("backgroundColor"), GREEN)}

direct = sibling = unexplained = 0
bad = []
for row in sorted(green_rows):
    if row in direct_rows:
        direct += 1
    elif block_of_row.get(row) in direct_blocks:
        sibling += 1
    else:
        unexplained += 1
        bad.append((row, block_of_row.get(row)))

print("=" * 70)
print(f"  Green rows audited : {len(green_rows)}")
print(f"    DIRECT  (row's own T&C URL is approved) : {direct}")
print(f"    SIBLING (same portal block as a direct) : {sibling}")
print(f"    UNEXPLAINED (no approved match in block) : {unexplained}")
print("=" * 70)
if bad:
    print("FALSE POSITIVES — green but no approved T&C match in their portal block:")
    for row, blk in bad:
        print(f"   row {row}  {blk}")
else:
    print("CLEAN: every green row traces to a real approved T&C match in its portal block.")

#!/usr/bin/env python3
"""Mark SheerID-approved portals in FinalActivationSheet.

Reads (Org ID col B, Portal col C) seed pairs from the ApprovedbyUrl tab
(rows 2..--end, default 40), matches them against FinalActivationSheet by
(orgid + portal URL), and for every matched row:
  * writes "Yes" into column A ("Sheerid Approved")
  * shades columns A..L light green

Layout (resolved by inspection, not header-search — these tabs are fixed):
  ApprovedbyUrl       : row1 header, data row2+; B=Org ID, C=Portal
  FinalActivationSheet: row1 banner (A1='Sheerid Approved'), row2 header,
                        data row3+; A=SheerID, B=Org id, C=Portal

Usage:
  python scripts/_activate_from_approved.py --dry-run
  python scripts/_activate_from_approved.py            # writes + colors
  python scripts/_activate_from_approved.py --end 40
"""
from __future__ import annotations

import re

import _bootstrap  # noqa: F401
import click

from agent.config import load_config
from agent.sheets_client import SheetsClient
from _run_dated_tab_portals import _resolve_tab_title
from run_portal_sheet import PORTAL_SHEET_ID

APPROVED_TAB = "ApprovedbyUrl"
FINAL_TAB = "FinalActivationSheet"

LIGHT_GREEN = {"red": 0.7843, "green": 0.9215, "blue": 0.7843}  # ~ Google "light green 2"
SHEERID_COL = 0   # A (0-based) in FinalActivationSheet
ORGID_COL = 1     # B
PORTAL_COL = 2    # C
# The six waterfall T&C-URL columns in FinalActivationSheet: D..I (0-based 3..8).
TNC_COLS = range(3, 9)
COLOR_END_COL = 12  # color A..L (0-based exclusive end)


def _tab_gid(sheets: SheetsClient, title: str) -> int:
    meta = sheets._service.spreadsheets().get(spreadsheetId=PORTAL_SHEET_ID).execute()
    for sh in meta["sheets"]:
        if sh["properties"]["title"] == title:
            return int(sh["properties"]["sheetId"])
    raise SystemExit(f"tab {title!r} not found")


def _norm(s) -> str:
    return str(s or "").strip()


def _col_index(letter: str) -> int:
    """'A'->0, 'B'->1, 'C'->2 ... (single/multi letter)."""
    idx = 0
    for ch in letter.strip().upper():
        idx = idx * 26 + (ord(ch) - ord("A") + 1)
    return idx - 1


@click.command()
@click.option("--approved-tab", default=APPROVED_TAB, show_default=True, help="Source tab of approved orgs/urls.")
@click.option("--start", type=int, default=2, show_default=True, help="First approved-tab sheet row to read (inclusive).")
@click.option("--end", type=int, default=40, show_default=True, help="Last approved-tab sheet row to read (inclusive).")
@click.option("--orgid-col", default="B", show_default=True, help="Column letter holding the OrgID in the approved tab.")
@click.option("--url-col", default="C", show_default=True, help="Column letter holding the URL(s) in the approved tab.")
@click.option("--multi", is_flag=True, help="The url-col cell may hold several URLs (newline/comma separated); split them into separate seeds.")
@click.option("--match", type=click.Choice(["portal", "tnc"]), default="portal", show_default=True,
              help="portal: url-col is a login portal, match FinalActivationSheet col C. "
                   "tnc: url-col is a T&C URL, match any of FinalActivationSheet cols D..I.")
@click.option("--dry-run", is_flag=True, help="Report matches; do NOT write or color.")
def main(approved_tab: str, start: int, end: int, orgid_col: str, url_col: str,
         multi: bool, match: str, dry_run: bool) -> None:
    config = load_config()
    sheets = SheetsClient(
        sheet_id=PORTAL_SHEET_ID, universities_tab="x", portals_tab="x",
        credentials_path=config.google_credentials_path, token_path=config.google_token_path,
    )
    appr_title = _resolve_tab_title(sheets, approved_tab)
    final_title = _resolve_tab_title(sheets, FINAL_TAB)
    appr_q = f"'{appr_title}'"
    final_q = f"'{final_title}'"
    appr_orgid_idx = _col_index(orgid_col)
    appr_url_idx = _col_index(url_col)

    # --- read approved seeds (rows start..end) ---
    appr_rows = sheets._get_values(appr_q, f"{start}:{end}")
    seeds: list[tuple[str, str]] = []
    seen = set()
    for r in appr_rows:
        orgid = _norm(r[appr_orgid_idx]) if len(r) > appr_orgid_idx else ""
        cell = _norm(r[appr_url_idx]) if len(r) > appr_url_idx else ""
        if not orgid or not cell:
            continue
        # A cell may carry several URLs (newline- or comma-separated).
        urls = (re.split(r"[\r\n,]+", cell) if multi else [cell])
        for u in urls:
            u = u.strip()
            if not u:
                continue
            key = (orgid, u)
            if key in seen:
                continue
            seen.add(key)
            seeds.append(key)

    # --- read FinalActivationSheet data (row 3+) ---
    final_rows = sheets._get_values(final_q, "3:100000")
    # Portal-block index: rows sharing the same (org, portal) are the SAME
    # portal split across multiple T&C rows (Terms + Privacy + Disclaimer...).
    # An approval of any one of those T&C URLs approves the whole portal, so we
    # expand a matched row to every sibling row of its (org, portal) block.
    block_of_row: dict[int, tuple[str, str]] = {}
    rows_of_block: dict[tuple[str, str], list[int]] = {}
    for ridx, r in enumerate(final_rows):
        sheet_row = ridx + 3
        orgid = _norm(r[ORGID_COL]) if len(r) > ORGID_COL else ""
        portal = _norm(r[PORTAL_COL]) if len(r) > PORTAL_COL else ""
        if not orgid or not portal:
            continue
        block_of_row[sheet_row] = (orgid, portal)
        rows_of_block.setdefault((orgid, portal), []).append(sheet_row)

    if match == "portal":
        # map (orgid, portal[col C]) -> list of 1-based sheet rows
        index: dict[tuple[str, str], list[int]] = {}
        for ridx, r in enumerate(final_rows):
            sheet_row = ridx + 3
            orgid = _norm(r[ORGID_COL]) if len(r) > ORGID_COL else ""
            portal = _norm(r[PORTAL_COL]) if len(r) > PORTAL_COL else ""
            if not orgid or not portal:
                continue
            index.setdefault((orgid, portal), []).append(sheet_row)

        def find(orgid: str, url: str) -> list[int]:
            return index.get((orgid, url), [])
    else:  # tnc: match orgid + url present in ANY of cols D..I
        # map orgid -> list of (sheet_row, {tnc urls in D..I})
        by_org: dict[str, list[tuple[int, set]]] = {}
        for ridx, r in enumerate(final_rows):
            sheet_row = ridx + 3
            orgid = _norm(r[ORGID_COL]) if len(r) > ORGID_COL else ""
            if not orgid:
                continue
            urls = {_norm(r[i]) for i in TNC_COLS if len(r) > i and _norm(r[i]).lower().startswith("http")}
            by_org.setdefault(orgid, []).append((sheet_row, urls))

        def find(orgid: str, url: str) -> list[int]:
            return [row for row, urls in by_org.get(orgid, []) if url in urls]

    matched_rows: list[int] = []
    unmatched: list[tuple[str, str]] = []
    click.echo("=" * 70)
    click.echo(f"  ApprovedbyUrl       : {appr_title!r}  (read rows {start}..{end}, match={match})")
    click.echo(f"  FinalActivationSheet: {final_title!r}  ({len(final_rows)} data rows)")
    click.echo(f"  Unique approved pairs: {len(seeds)}")
    click.echo("=" * 70)
    for orgid, url in seeds:
        hits = find(orgid, url)
        if hits:
            matched_rows.extend(hits)
            click.echo(f"  MATCH org {orgid} -> rows {hits}  {url[:60]}")
        else:
            unmatched.append((orgid, url))
            click.echo(f"  NO MATCH org {orgid}  {url[:70]}")

    direct_rows = sorted(set(matched_rows))
    # Expand each directly-matched row to its full portal block (sibling T&C
    # rows of the same org+portal). Portal mode already matches the whole
    # block, so this only adds rows in tnc mode.
    expanded: set[int] = set()
    for row in direct_rows:
        blk = block_of_row.get(row)
        expanded.update(rows_of_block.get(blk, [row]) if blk else [row])
    matched_rows = sorted(expanded)
    added = sorted(expanded - set(direct_rows))
    click.echo("-" * 70)
    click.echo(f"  directly matched rows: {len(direct_rows)}   "
               f"+ sibling portal rows: {len(added)}   = {len(matched_rows)} total")
    click.echo(f"  unmatched approved pairs: {len(unmatched)}")
    if added:
        click.echo(f"  sibling rows added: {added}")

    # How many of these rows are ALREADY marked "Yes" (from a previous run)?
    already_yes = sorted(
        row for row in matched_rows
        if row - 3 < len(final_rows) and _norm(final_rows[row - 3][SHEERID_COL]).lower() == "yes"
    )
    new_rows = sorted(set(matched_rows) - set(already_yes))
    click.echo(f"  already 'Yes' (marked before): {len(already_yes)}   "
               f"newly marked this run: {len(new_rows)}")
    if new_rows:
        click.echo(f"  NEW rows: {new_rows}")

    if dry_run:
        click.echo("DRY RUN — no writes.")
        return
    if not matched_rows:
        click.echo("Nothing to write.")
        return

    # --- write "Yes" into column A for matched rows ---
    data = [{"range": f"{final_q}!A{row}", "values": [["Yes"]]} for row in matched_rows]
    sheets._service.spreadsheets().values().batchUpdate(
        spreadsheetId=PORTAL_SHEET_ID,
        body={"valueInputOption": "USER_ENTERED", "data": data},
    ).execute()

    # --- color matched rows light green (A..L), grouped into contiguous ranges ---
    gid = _tab_gid(sheets, final_title)
    ranges: list[tuple[int, int]] = []  # (start_row_1based, end_row_1based inclusive)
    for row in matched_rows:
        if ranges and row == ranges[-1][1] + 1:
            ranges[-1] = (ranges[-1][0], row)
        else:
            ranges.append((row, row))
    requests = [{
        "repeatCell": {
            "range": {
                "sheetId": gid,
                "startRowIndex": a - 1, "endRowIndex": b,
                "startColumnIndex": 0, "endColumnIndex": COLOR_END_COL,
            },
            "cell": {"userEnteredFormat": {"backgroundColor": LIGHT_GREEN}},
            "fields": "userEnteredFormat.backgroundColor",
        }
    } for a, b in ranges]
    sheets._service.spreadsheets().batchUpdate(
        spreadsheetId=PORTAL_SHEET_ID, body={"requests": requests},
    ).execute()

    click.echo(f"Done. wrote 'Yes' + light-green to {len(matched_rows)} rows "
               f"in {len(ranges)} contiguous block(s).")
    if unmatched:
        click.echo("Unmatched approved pairs (no row in FinalActivationSheet):")
        for orgid, portal in unmatched:
            click.echo(f"  org {orgid}  {portal}")


if __name__ == "__main__":
    main()

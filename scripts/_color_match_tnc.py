#!/usr/bin/env python3
"""Color rows in a waterfall tab light green when ANY of their T&C-level cells
(C..H) holds a URL present in a given list file.

  23-24June layout: row1 blank banner, row2 header, data row3+.
    A=Org id, B=Portal(login), C..H = the six T&C level columns.

Usage:
  python scripts/_color_match_tnc.py --tab 23-24June --list <file> --dry-run
  python scripts/_color_match_tnc.py --tab 23-24June --list <file>
"""
from __future__ import annotations

import _bootstrap  # noqa: F401
import click

from agent.config import load_config
from agent.sheets_client import SheetsClient
from _run_dated_tab_portals import _resolve_tab_title
from run_portal_sheet import PORTAL_SHEET_ID

LIGHT_GREEN = {"red": 0.7843, "green": 0.9215, "blue": 0.7843}
TNC_COLS = range(2, 8)   # C..H (0-based) in this layout
HEADER_ROW = 2
COLOR_END_COL = 11       # color A..K


def _norm(s) -> str:
    return str(s or "").strip()


def _tab_gid(sheets: SheetsClient, title: str) -> int:
    meta = sheets._service.spreadsheets().get(spreadsheetId=PORTAL_SHEET_ID).execute()
    for sh in meta["sheets"]:
        if sh["properties"]["title"] == title:
            return int(sh["properties"]["sheetId"])
    raise SystemExit(f"tab {title!r} not found")


@click.command()
@click.option("--tab", "tab_arg", default="23-24June", show_default=True)
@click.option("--list", "list_path", required=True, help="File with one URL per line to compare against.")
@click.option("--dry-run", is_flag=True, help="Report matched rows; do NOT color.")
def main(tab_arg: str, list_path: str, dry_run: bool) -> None:
    config = load_config()
    sheets = SheetsClient(
        sheet_id=PORTAL_SHEET_ID, universities_tab="x", portals_tab="x",
        credentials_path=config.google_credentials_path, token_path=config.google_token_path,
    )
    title = _resolve_tab_title(sheets, tab_arg)
    final_q = f"'{title}'"

    with open(list_path) as f:
        wanted = {ln.strip() for ln in f if ln.strip().lower().startswith("http")}

    data_first = HEADER_ROW + 1
    rows = sheets._get_values(final_q, f"{data_first}:100000")
    matched_rows: list[int] = []
    hit_urls: set[str] = set()
    for ridx, r in enumerate(rows):
        sheet_row = data_first + ridx
        cells = {_norm(r[i]) for i in TNC_COLS if len(r) > i}
        inter = cells & wanted
        if inter:
            matched_rows.append(sheet_row)
            hit_urls |= inter

    click.echo("=" * 70)
    click.echo(f"  Tab            : {title!r}")
    click.echo(f"  List URLs      : {len(wanted)}")
    click.echo(f"  Data rows      : {len(rows)}")
    click.echo(f"  Matched rows   : {len(matched_rows)}")
    click.echo(f"  Distinct list URLs that hit ≥1 row : {len(hit_urls)} / {len(wanted)}")
    missed = sorted(wanted - hit_urls)
    if missed:
        click.echo(f"  List URLs with NO row match ({len(missed)}):")
        for u in missed:
            click.echo(f"     {u}")
    click.echo("=" * 70)

    if dry_run:
        click.echo("DRY RUN — no coloring.")
        return
    if not matched_rows:
        click.echo("Nothing to color.")
        return

    gid = _tab_gid(sheets, title)
    ranges: list[tuple[int, int]] = []
    for row in matched_rows:
        if ranges and row == ranges[-1][1] + 1:
            ranges[-1] = (ranges[-1][0], row)
        else:
            ranges.append((row, row))
    requests = [{
        "repeatCell": {
            "range": {"sheetId": gid, "startRowIndex": a - 1, "endRowIndex": b,
                      "startColumnIndex": 0, "endColumnIndex": COLOR_END_COL},
            "cell": {"userEnteredFormat": {"backgroundColor": LIGHT_GREEN}},
            "fields": "userEnteredFormat.backgroundColor",
        }
    } for a, b in ranges]
    sheets._service.spreadsheets().batchUpdate(
        spreadsheetId=PORTAL_SHEET_ID, body={"requests": requests}).execute()
    click.echo(f"Done. colored {len(matched_rows)} rows light green ({len(ranges)} block(s)).")


if __name__ == "__main__":
    main()

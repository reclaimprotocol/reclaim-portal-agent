#!/usr/bin/env python3
"""Reorder noLogin data rows (row 3+) into three buckets, stable within each:
  1. has_tnc - at least one T&C-level cell (C..H) is an http URL
  2. empty   - all C..H blank (rows that had no login URL, never processed)
  3. all_na  - C..H contain 'n/a' (processed, no T&C found)
Banner (row 1) + header (row 2) untouched. noLogin has no cell formatting, so a
values rewrite is lossless.
"""
from __future__ import annotations

import _bootstrap  # noqa: F401
import click

from agent.config import load_config
from agent.sheets_client import SheetsClient, _col_letter
from run_portal_sheet import PORTAL_SHEET_ID

WIDTH = 11  # A..K


def _cat(r: list) -> int:
    cells = [str(r[i]).strip() for i in range(2, 8) if len(r) > i]
    if any(c.lower().startswith("http") for c in cells):
        return 0  # has_tnc
    if any(c.lower() == "n/a" for c in cells):
        return 2  # all_na
    return 1      # empty


@click.command()
@click.option("--dry-run", is_flag=True)
def main(dry_run: bool) -> None:
    config = load_config()
    sheets = SheetsClient(
        sheet_id=PORTAL_SHEET_ID, universities_tab="x", portals_tab="x",
        credentials_path=config.google_credentials_path, token_path=config.google_token_path,
    )
    q = "'noLogin'"
    rows = sheets._get_values(q, "3:100000")
    # stable sort by bucket (Python sort is stable -> original order kept within)
    ordered = sorted(rows, key=_cat)
    padded = [list(r) + [""] * (WIDTH - len(r)) if len(r) < WIDTH else list(r)[:WIDTH] for r in ordered]
    counts = {0: 0, 1: 0, 2: 0}
    for r in rows:
        counts[_cat(r)] += 1
    names = {0: "has_tnc", 1: "empty", 2: "all_na"}
    click.echo(f"rows={len(rows)}  " + "  ".join(f"{names[k]}={counts[k]}" for k in (0, 1, 2)))
    if dry_run:
        click.echo("DRY RUN — no write.")
        return
    end_row = 2 + len(padded)
    sheets._service.spreadsheets().values().update(
        spreadsheetId=PORTAL_SHEET_ID,
        range=f"{q}!A3:{_col_letter(WIDTH)}{end_row}",
        valueInputOption="USER_ENTERED",
        body={"values": padded},
    ).execute()
    click.echo(f"Done. reordered {len(padded)} rows (A3:{_col_letter(WIDTH)}{end_row}).")


if __name__ == "__main__":
    main()

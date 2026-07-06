#!/usr/bin/env python3
"""Export a dated waterfall tab's T&C URLs to a CSV of (orgId, urls).

One CSV row per T&C URL found on the tab (cols C-H), de-duped per (org, url).
Rows with no T&C URL are skipped. Matches the 19June.csv format: two columns
`orgId,urls`. Reuses run_portal_sheet's hardwired PORTAL_SHEET_ID.

Usage:
    python scripts/export_tnc_csv.py --tab "20 June"
    python scripts/export_tnc_csv.py --tab "20 June" --out 20June.csv
    python scripts/export_tnc_csv.py --tab 19June --keep-portal   # add portal col
"""
from __future__ import annotations

import csv

import _bootstrap  # noqa: F401
import click

from agent.config import load_config
from agent.sheets_client import SheetsClient
from run_portal_sheet import PORTAL_SHEET_ID
from _run_dated_tab_portals import _resolve_tab_title

HEADER_ROW = 2          # row 1 is the merged "TNCs" banner
DATA_FIRST = HEADER_ROW + 1
LEVEL_COLS = range(2, 8)  # cols C..H hold the T&C URL


@click.command()
@click.option("--tab", "tab_arg", default="20 June", show_default=True)
@click.option("--out", "out_path", default=None, help="Output CSV path (default <tab>.csv).")
@click.option("--keep-portal", is_flag=True, help="Include the portal URL as a middle column.")
def main(tab_arg, out_path, keep_portal):
    config = load_config()
    sheets = SheetsClient(
        sheet_id=PORTAL_SHEET_ID, universities_tab="x", portals_tab="x",
        credentials_path=config.google_credentials_path, token_path=config.google_token_path,
    )
    title = _resolve_tab_title(sheets, tab_arg)
    rows = sheets._get_values(f"'{title}'", f"{DATA_FIRST}:100000")

    out_path = out_path or f"{title.strip().replace(' ', '')}.csv"
    seen: set = set()
    out_rows: list[tuple] = []
    for raw in rows:
        p = list(raw) + [""] * 11
        org = str(p[0]).strip()
        portal = str(p[1]).strip().replace("\n", "")
        if not org:
            continue
        for k in LEVEL_COLS:
            u = str(p[k]).strip()
            if not u.lower().startswith("http"):
                continue
            key = (org, u)
            if key in seen:
                continue
            seen.add(key)
            out_rows.append((org, portal, u) if keep_portal else (org, u))

    header = ["orgId", "portal", "urls"] if keep_portal else ["orgId", "urls"]
    with open(out_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(out_rows)
    click.echo(f"wrote {len(out_rows)} rows -> {out_path}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Export the "15June" tab to a 2-column CSV: orgid, tnc_url.

15June layout: A=SheerID OrgID, E="ReclaimProtocol Terms of Use URL" holding one
or more newline-separated T&C URLs per row. We emit one CSV row per
(orgid, tnc_url); the orgid repeats across its multiple T&C URLs. Exact
duplicate (orgid, tnc_url) pairs are de-duped.

Usage:
    python scripts/_export_15june_csv.py
    python scripts/_export_15june_csv.py --out 15june.csv
"""
from __future__ import annotations

import csv
import re

import _bootstrap  # noqa: F401
import click

from agent.config import load_config
from agent.sheets_client import SheetsClient, _col_letter
from _run_dated_tab_portals import _resolve_tab_title
from run_portal_sheet import PORTAL_SHEET_ID

DEFAULT_TAB = "15June"
ORGID_HEADER = "SheerID OrgID"
TC_URL_HEADER = "ReclaimProtocol Terms of Use URL"


def _find_col(header, title, fallback):
    for i, h in enumerate(header):
        if str(h).strip().lower() == title.strip().lower():
            return i
    return fallback


@click.command()
@click.option("--tab", "tab_arg", default=DEFAULT_TAB, show_default=True)
@click.option("--out", "out_path", default="15june.csv", show_default=True, help="Output CSV path.")
@click.option("--orgid-header", default=ORGID_HEADER, show_default=True, help="OrgID column header.")
@click.option("--tcurl-header", default=TC_URL_HEADER, show_default=True, help="T&C-URL column header (newline-separated).")
@click.option("--filter-header", default=None, help="Only include rows where this column equals --filter-equals.")
@click.option("--filter-equals", default=None, help="Required value for --filter-header (case-insensitive).")
def main(tab_arg, out_path, orgid_header, tcurl_header, filter_header, filter_equals):
    config = load_config()
    sheets = SheetsClient(
        sheet_id=PORTAL_SHEET_ID, universities_tab="x", portals_tab="x",
        credentials_path=config.google_credentials_path, token_path=config.google_token_path,
    )
    title = _resolve_tab_title(sheets, tab_arg)
    qtab = f"'{title}'"
    header = (sheets._get_values(qtab, "1:1") or [[]])[0]
    orgid_idx = _find_col(header, orgid_header, 0)
    tc_idx = _find_col(header, tcurl_header, 4)
    filter_idx = _find_col(header, filter_header, None) if filter_header else None
    if filter_header and filter_idx is None:
        raise SystemExit(f"filter column {filter_header!r} not found; header={header}")
    pad = max(orgid_idx, tc_idx, filter_idx or 0) + 1

    rows = sheets._get_values(qtab, "2:100000")
    seen: set[tuple[str, str]] = set()
    out_rows: list[tuple[str, str]] = []
    skipped_no_url = skipped_no_orgid = skipped_filter = 0
    for raw in rows:
        p = list(raw) + [""] * pad
        if filter_idx is not None and str(p[filter_idx]).strip().lower() != filter_equals.strip().lower():
            skipped_filter += 1
            continue
        orgid_cell = str(p[orgid_idx]).strip()
        tc_cell = str(p[tc_idx])
        urls = [u.strip() for u in tc_cell.split("\n") if u.strip().startswith("http")]
        if not orgid_cell:
            skipped_no_orgid += 1
            continue
        if not urls:
            skipped_no_url += 1
            continue
        for orgid in (t.strip() for t in re.split(r"[,\s]+", orgid_cell) if t.strip()):
            for url in urls:
                key = (orgid, url)
                if key in seen:
                    continue
                seen.add(key)
                out_rows.append(key)

    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["orgId", "urls"])
        w.writerows(out_rows)

    click.echo(f"Tab        : {title!r}")
    click.echo(f"T&C column : {_col_letter(tc_idx + 1)} ({header[tc_idx]!r})")
    if filter_header:
        click.echo(f"Filter     : {filter_header!r} == {filter_equals!r}  (skipped {skipped_filter} non-matching)")
    click.echo(f"Wrote {len(out_rows)} (orgid, tnc_url) rows -> {out_path}")
    click.echo(f"Skipped: no-url={skipped_no_url} no-orgid={skipped_no_orgid}")


if __name__ == "__main__":
    main()

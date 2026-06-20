#!/usr/bin/env python3
"""Export the "17&18June" tab to a 2-column CSV: orgid, tnc_url.

Each sheet row already carries one T&C URL (in whichever level column C-H is
populated). We emit one CSV row per (orgid, tnc_url). A column-A cell with
several comma/space-separated OrgIDs is expanded to one CSV row per OrgID.
Exact duplicate (orgid, tnc_url) pairs are de-duped; the orgid otherwise
repeats across its multiple T&C URLs.

Usage:
    python scripts/_export_17_18june_csv.py
    python scripts/_export_17_18june_csv.py --out /tmp/tnc.csv
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

DEFAULT_TAB = "17&18June"
HEADER_ROW = 2
ORGID_HEADER = "Org id"
# Columns that may hold the T&C URL (level columns C..H), scanned in order.
TCURL_HEADERS = (
    "Exact URL (1)", "Parent URL (2-4)", "Parent domain (5-6)",
    "Linked Parent University Home page (8)", "Vendor Home page (7)",
    "Unlinked Parent University Home page (8)",
)


def _find_col(header, title):
    for i, h in enumerate(header):
        if str(h).strip().lower() == title.strip().lower():
            return i
    return None


@click.command()
@click.option("--tab", "tab_arg", default=DEFAULT_TAB, show_default=True)
@click.option("--out", "out_path", default="17_18June_tnc.csv", show_default=True, help="Output CSV path.")
def main(tab_arg, out_path):
    config = load_config()
    sheets = SheetsClient(
        sheet_id=PORTAL_SHEET_ID, universities_tab="x", portals_tab="x",
        credentials_path=config.google_credentials_path, token_path=config.google_token_path,
    )
    title = _resolve_tab_title(sheets, tab_arg)
    qtab = f"'{title}'"
    header = (sheets._get_values(qtab, f"{HEADER_ROW}:{HEADER_ROW}") or [[]])[0]
    orgid_idx = _find_col(header, ORGID_HEADER) or 0
    tcurl_idxs = [i for i in (_find_col(header, h) for h in TCURL_HEADERS) if i is not None]
    if not tcurl_idxs:
        raise SystemExit(f"no T&C-level columns found; header={header}")

    rows = sheets._get_values(qtab, f"{HEADER_ROW + 1}:100000")
    pad = max(orgid_idx, *tcurl_idxs) + 1
    seen: set[tuple[str, str]] = set()
    out_rows: list[tuple[str, str]] = []
    skipped_no_url = skipped_no_orgid = 0
    for raw in rows:
        p = list(raw) + [""] * pad
        orgid_cell = str(p[orgid_idx]).strip()
        tc_url = next((str(p[i]).strip() for i in tcurl_idxs if str(p[i]).strip().startswith("http")), "")
        if not orgid_cell:
            skipped_no_orgid += 1
            continue
        if not tc_url:
            skipped_no_url += 1
            continue
        for orgid in (t.strip() for t in re.split(r"[,\s]+", orgid_cell) if t.strip()):
            key = (orgid, tc_url)
            if key in seen:
                continue
            seen.add(key)
            out_rows.append(key)

    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["orgId", "urls"])
        w.writerows(out_rows)

    cols = ", ".join(_col_letter(i + 1) for i in tcurl_idxs)
    click.echo(f"Tab        : {title!r}")
    click.echo(f"T&C columns: {cols}")
    click.echo(f"Wrote {len(out_rows)} (orgid, tnc_url) rows -> {out_path}")
    click.echo(f"Skipped: no-url={skipped_no_url} no-orgid={skipped_no_orgid}")


if __name__ == "__main__":
    main()

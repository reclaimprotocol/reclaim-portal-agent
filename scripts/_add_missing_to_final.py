#!/usr/bin/env python3
"""Append ApprovedbyUrl orgs that are MISSING from FinalActivationSheet.

For each missing org (orgid not already in FinalActivationSheet col B), build the
cross-product of its login URLs (ApprovedbyUrl col C) x its T&C URLs (col D) and
append one row per pair to the bottom of FinalActivationSheet:

    B = orgid      C = login URL      D = T&C URL      (A and E.. left blank)

ApprovedbyUrl layout: row1 header; A=OrgID, B=Name, C=Login URL(s), D=Terms URL(s).
Multi-valued C/D cells are split on newline/comma.

Usage:
  python scripts/_add_missing_to_final.py --dry-run
  python scripts/_add_missing_to_final.py
"""
from __future__ import annotations

import re

import _bootstrap  # noqa: F401
import click

from agent.config import load_config
from agent.sheets_client import SheetsClient
from _run_dated_tab_portals import _resolve_tab_title
from run_portal_sheet import PORTAL_SHEET_ID

APPR_ORGID, APPR_LOGIN, APPR_TNC = 0, 2, 3   # ApprovedbyUrl cols A, C, D
FINAL_ORGID = 1                              # FinalActivationSheet col B


def _split(cell: str) -> list[str]:
    return [u.strip() for u in re.split(r"[\r\n,]+", str(cell or "")) if u.strip().lower().startswith("http")]


@click.command()
@click.option("--dry-run", is_flag=True, help="Report what would be appended; do NOT write.")
def main(dry_run: bool) -> None:
    config = load_config()
    sheets = SheetsClient(
        sheet_id=PORTAL_SHEET_ID, universities_tab="x", portals_tab="x",
        credentials_path=config.google_credentials_path, token_path=config.google_token_path,
    )
    appr_q = f"'{_resolve_tab_title(sheets, 'ApprovedbyUrl')}'"
    final_title = _resolve_tab_title(sheets, "FinalActivationSheet")
    final_q = f"'{final_title}'"

    final_all = sheets._get_values(final_q, "1:100000")
    final_orgs = {str(r[FINAL_ORGID]).strip() for r in final_all[2:]
                  if len(r) > FINAL_ORGID and str(r[FINAL_ORGID]).strip()}
    append_start = len(final_all) + 1  # first empty row at the bottom

    appr_rows = sheets._get_values(appr_q, "2:100000")
    new_rows: list[list[str]] = []
    seen: set[tuple[str, str, str]] = set()
    missing_orgs = skipped_no_login = 0
    for r in appr_rows:
        orgid = str(r[APPR_ORGID]).strip() if r else ""
        if not orgid or orgid in final_orgs:
            continue
        logins = _split(r[APPR_LOGIN] if len(r) > APPR_LOGIN else "")
        tncs = _split(r[APPR_TNC] if len(r) > APPR_TNC else "")
        if not logins:
            skipped_no_login += 1
            continue
        missing_orgs += 1
        for login in logins:                 # each login covers each tnc
            for tnc in (tncs or [""]):
                key = (orgid, login, tnc)
                if key in seen:
                    continue
                seen.add(key)
                new_rows.append(["", orgid, login, tnc])  # A blank, B/C/D filled

    click.echo("=" * 70)
    click.echo(f"  FinalActivationSheet existing orgids : {len(final_orgs)}")
    click.echo(f"  Missing orgs to append               : {missing_orgs}"
               + (f"  (skipped {skipped_no_login} w/o login URL)" if skipped_no_login else ""))
    click.echo(f"  Rows to append (login x tnc)         : {len(new_rows)}")
    click.echo(f"  Append range                         : B{append_start}:D{append_start + len(new_rows) - 1}")
    click.echo("=" * 70)
    for row in new_rows[:6]:
        click.echo(f"  {row[1]} | {row[2][:45]} | {row[3][:45]}")
    click.echo("  ...")

    if dry_run:
        click.echo("DRY RUN — no writes.")
        return
    if not new_rows:
        click.echo("Nothing to append.")
        return

    # write A..D block in one shot
    end_row = append_start + len(new_rows) - 1
    sheets._service.spreadsheets().values().update(
        spreadsheetId=PORTAL_SHEET_ID,
        range=f"{final_q}!A{append_start}:D{end_row}",
        valueInputOption="USER_ENTERED",
        body={"values": new_rows},
    ).execute()
    click.echo(f"Done. appended {len(new_rows)} rows ({missing_orgs} orgs) "
               f"to {final_title} rows {append_start}..{end_row}.")


if __name__ == "__main__":
    main()

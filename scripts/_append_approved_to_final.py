#!/usr/bin/env python3
"""Append ApprovedbyUrl (orgid x login x tnc) rows to FinalActivationSheet,
skipping any (orgid, login, tnc) triple that ALREADY exists there.

ApprovedbyUrl: row1 header ['OrgIDs','Org Name','Websites','Terms URLs','Notes']
  A=orgid, C=login URL(s), D=Terms URL(s)  (multi-valued; split on newline/comma,
  non-http lines like free-text notes are ignored).

For each org we cross-product its login URLs x its T&C URLs and append, in the
same format used before:  B=orgid, C=login, D=tnc  (A and E.. blank).

A candidate is a DUPLICATE (skipped) if FinalActivationSheet already has a row
with col B == orgid, col C == login, and the tnc URL present in any T&C column
(D..I) of that row.

Usage:
  python scripts/_append_approved_to_final.py --dry-run
  python scripts/_append_approved_to_final.py
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
F_ORGID, F_PORTAL = 1, 2                      # FinalActivationSheet cols B, C
F_TNC_COLS = range(3, 9)                       # FinalActivationSheet cols D..I


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
    append_start = len(final_all) + 1

    # Existing (orgid, portal, tnc) triples already in FinalActivationSheet.
    existing: set[tuple[str, str, str]] = set()
    for r in final_all[2:]:
        orgid = str(r[F_ORGID]).strip() if len(r) > F_ORGID else ""
        portal = str(r[F_PORTAL]).strip() if len(r) > F_PORTAL else ""
        if not orgid or not portal:
            continue
        for i in F_TNC_COLS:
            u = str(r[i]).strip() if len(r) > i else ""
            if u.lower().startswith("http"):
                existing.add((orgid, portal, u))

    appr_rows = sheets._get_values(appr_q, "2:100000")
    new_rows: list[list[str]] = []
    seen: set[tuple[str, str, str]] = set()
    orgs_seen = set()
    dup_skipped = 0
    blank_tnc_rows = 0
    no_login_orgs = 0
    for r in appr_rows:
        orgid = str(r[APPR_ORGID]).strip() if r else ""
        if not orgid:
            continue
        logins = _split(r[APPR_LOGIN] if len(r) > APPR_LOGIN else "")
        tncs = _split(r[APPR_TNC] if len(r) > APPR_TNC else "")
        if not logins:
            no_login_orgs += 1
            continue
        orgs_seen.add(orgid)
        for login in logins:
            for tnc in (tncs or [""]):
                triple = (orgid, login, tnc)
                if tnc and triple in existing:   # already in Final -> skip
                    dup_skipped += 1
                    continue
                if triple in seen:               # dup within ApprovedbyUrl
                    continue
                seen.add(triple)
                if not tnc:
                    blank_tnc_rows += 1
                new_rows.append(["", orgid, login, tnc])

    click.echo("=" * 70)
    click.echo(f"  ApprovedbyUrl orgs (with login)      : {len(orgs_seen)}"
               + (f"  (skipped {no_login_orgs} w/o login URL)" if no_login_orgs else ""))
    click.echo(f"  Existing triples in Final            : {len(existing)}")
    click.echo(f"  Candidates already in Final (skipped): {dup_skipped}")
    click.echo(f"  NEW rows to append                   : {len(new_rows)}"
               + (f"  (of which {blank_tnc_rows} have blank T&C)" if blank_tnc_rows else ""))
    click.echo(f"  Append range                         : A{append_start}:D{append_start + len(new_rows) - 1}")
    click.echo("=" * 70)
    for row in new_rows[:8]:
        click.echo(f"  {row[1]} | {row[2][:42]} | {row[3][:42]}")
    click.echo("  ...")

    if dry_run:
        click.echo("DRY RUN — no writes.")
        return
    if not new_rows:
        click.echo("Nothing to append.")
        return

    end_row = append_start + len(new_rows) - 1
    sheets._service.spreadsheets().values().update(
        spreadsheetId=PORTAL_SHEET_ID,
        range=f"{final_q}!A{append_start}:D{end_row}",
        valueInputOption="USER_ENTERED",
        body={"values": new_rows},
    ).execute()
    click.echo(f"Done. appended {len(new_rows)} rows ({len(orgs_seen)} orgs scanned) "
               f"to {final_title} rows {append_start}..{end_row}.")


if __name__ == "__main__":
    main()

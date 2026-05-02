"""Wipe the Portals tab + per-OrgID state so the next batch run starts fresh.

Sheet side:
  * Delete every data row from the Portals tab (rows 2..N).
  * Re-write the header row to the canonical 7-column schema in
    `SheetsClient.PORTALS_COLUMNS`.

state.db side:
  * Delete every row from `orgid_status` (so `is_done` is false everywhere).
  * Delete every row from `stage_results` (so cached Stage A/B/C output
    doesn't shadow the new Stage C/D logic).
  * Delete every row from `tc_analyzer_cache` (so the new keyword pass /
    URL fallback runs against fresh fetches).

Schema is preserved — only rows are deleted. Prints before/after counts so
you see exactly what was wiped.
"""
from __future__ import annotations

import sqlite3

import _bootstrap  # noqa: F401
import click

from agent.config import load_config
from agent.sheets_client import SheetsClient


@click.command()
def main() -> None:
    config = load_config()
    _bootstrap.setup_logging(config.log_level)

    sheets = SheetsClient.from_config(config)

    # --- Sheet: count, clear, rewrite header --------------------------------
    portal_rows = sheets.read_portals()
    sheet_data_rows_before = len(portal_rows)
    click.echo(f"Portals tab: {sheet_data_rows_before} data row(s) before clean")

    # Clear a wide range so legacy columns (H..Q from the old 17-col schema)
    # don't survive the trim — values().clear() is idempotent and reflows
    # nothing, but we have to span enough columns to wipe all legacy data.
    wipe_end_col = _col_letter(max(26, len(SheetsClient.PORTALS_COLUMNS)))
    sheets._execute_with_retry(  # noqa: SLF001 — no public method covers values().clear()
        lambda: sheets._service.spreadsheets().values().clear(
            spreadsheetId=sheets.sheet_id,
            range=f"{sheets.portals_tab}!A2:{wipe_end_col}100000",
            body={},
        ).execute(),
        label=f"clear A2:{wipe_end_col}100000",
    )

    # Replace the entire header row: clear it first (so legacy column names
    # in H..Q from the old schema are removed) and then write the new one.
    sheets._execute_with_retry(  # noqa: SLF001
        lambda: sheets._service.spreadsheets().values().clear(
            spreadsheetId=sheets.sheet_id,
            range=f"{sheets.portals_tab}!A1:{wipe_end_col}1",
            body={},
        ).execute(),
        label=f"clear A1:{wipe_end_col}1",
    )
    end_col = _col_letter(len(SheetsClient.PORTALS_COLUMNS))
    sheets._execute_with_retry(  # noqa: SLF001
        lambda: sheets._service.spreadsheets().values().update(
            spreadsheetId=sheets.sheet_id,
            range=f"{sheets.portals_tab}!A1:{end_col}1",
            valueInputOption="USER_ENTERED",
            body={"values": [list(SheetsClient.PORTALS_COLUMNS)]},
        ).execute(),
        label="rewrite header",
    )
    click.echo(
        f"Portals tab: header set to "
        f"{list(SheetsClient.PORTALS_COLUMNS)} ({len(SheetsClient.PORTALS_COLUMNS)} cols)"
    )

    portal_rows_after = sheets.read_portals()
    click.echo(f"Portals tab: {len(portal_rows_after)} data row(s) after clean")

    # --- state.db: wipe per-OrgID rows --------------------------------------
    db_path = config.state_db_path
    click.echo(f"\nstate.db: {db_path}")
    counts_before = _count_rows(db_path)
    for table, count in counts_before.items():
        click.echo(f"  {table}: {count} row(s) before clean")

    _delete_all_rows(db_path)

    counts_after = _count_rows(db_path)
    for table, count in counts_after.items():
        click.echo(f"  {table}: {count} row(s) after clean")

    click.echo("\n✅ Sheet cleaned. Ready for fresh batch run.")


# ------------------------------------------------------------------ helpers

# Tables we know about. We try each; missing tables are silently skipped so
# this script keeps working if the schema migrates.
_TABLES_TO_WIPE: tuple[str, ...] = (
    "orgid_status",
    "stage_results",
    "tc_analyzer_cache",
)


def _count_rows(db_path) -> dict[str, int]:
    out: dict[str, int] = {}
    conn = sqlite3.connect(db_path)
    try:
        for table in _TABLES_TO_WIPE:
            try:
                row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
                out[table] = int(row[0])
            except sqlite3.OperationalError:
                # Table doesn't exist — note it so the user sees what was skipped.
                out[table] = -1
    finally:
        conn.close()
    return out


def _delete_all_rows(db_path) -> None:
    conn = sqlite3.connect(db_path)
    try:
        for table in _TABLES_TO_WIPE:
            try:
                conn.execute(f"DELETE FROM {table}")
            except sqlite3.OperationalError:
                pass
        conn.commit()
    finally:
        conn.close()


def _col_letter(n: int) -> str:
    """1 → A, 26 → Z, 27 → AA. Matches the helper in sheets_client."""
    if n < 1:
        raise ValueError(f"column number must be >= 1, got {n}")
    letters = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


if __name__ == "__main__":
    main()

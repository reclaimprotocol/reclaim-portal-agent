"""Manual purge — wipe an OrgID from the Portals tab AND state.db.

Stage D's happy path now *merges* rather than replacing, so old rows stick
around across runs. When you genuinely want to start fresh for an OrgID
(e.g. you've deleted it from SheerID's sheet, or your testing got into a
weird state), use this.

Examples:
    python scripts/purge_orgid.py --orgid 664140
    python scripts/purge_orgid.py --orgid 664140 --yes  # no confirmation prompt
"""
from __future__ import annotations

import _bootstrap  # noqa: F401
import click

from agent.config import load_config
from agent.sheets_client import SheetsClient
from agent.state import StateStore


@click.command()
@click.option("--orgid", required=True, help="The OrgID to purge")
@click.option(
    "--yes", "skip_confirm", is_flag=True,
    help="Skip the y/N confirmation prompt",
)
def main(orgid: str, skip_confirm: bool) -> None:
    config = load_config()
    _bootstrap.setup_logging(config.log_level)
    target = str(orgid).strip()

    if not skip_confirm:
        click.confirm(
            f"Delete all Portals-tab rows for OrgID {target} and wipe its "
            f"state.db entries? This cannot be undone.",
            abort=True,
        )

    sheets = SheetsClient.from_config(config)
    rows_deleted = sheets.delete_portals_for_orgid(target)
    click.echo(f"Portals tab: deleted {rows_deleted} row(s)")

    with StateStore(config.state_db_path) as state:
        results_deleted, status_deleted = state.purge_orgid(target)
    click.echo(
        f"state.db: deleted {status_deleted} orgid_status row, "
        f"{results_deleted} stage_results row(s)"
    )


if __name__ == "__main__":
    main()

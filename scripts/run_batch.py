"""Process a batch of OrgIDs from the sheet.

Examples:
    python scripts/run_batch.py --limit 20
    python scripts/run_batch.py --all
"""
from __future__ import annotations

import _bootstrap  # noqa: F401
import click

from agent.config import load_config
from agent.orchestrator import Orchestrator
from agent.sheets_client import SheetsClient
from agent.state import StateStore


@click.command()
@click.option("--limit", type=int, default=20, show_default=True, help="Max OrgIDs to process")
@click.option("--all", "all_rows", is_flag=True, help="Ignore --limit; process every un-done row")
@click.option(
    "--force", is_flag=True,
    help="Re-process even OrgIDs that state.db marks as complete",
)
def main(limit: int, all_rows: bool, force: bool) -> None:
    config = load_config()
    _bootstrap.setup_logging(config.log_level)

    sheets = SheetsClient.from_config(config)
    rows = sheets.read_universities()

    effective_limit = None if all_rows else limit
    with StateStore(config.state_db_path) as state:
        orch = Orchestrator(config, state)
        stats = orch.process(rows, limit=effective_limit, force=force)
        click.echo(f"result: {stats.as_dict()}")


if __name__ == "__main__":
    main()

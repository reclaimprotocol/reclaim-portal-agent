"""Daemon-style runner.

Processes every remaining (un-done) OrgID until the sheet is exhausted.
Rate-limit pauses are already handled inside the orchestrator, so this
script simply keeps pulling until `done + failed + stubbed == remaining`.

Example:
    python scripts/run_background.py
    nohup python scripts/run_background.py > run.log 2>&1 &
"""
from __future__ import annotations

import time

import _bootstrap  # noqa: F401
import click

from agent.config import load_config
from agent.orchestrator import Orchestrator
from agent.sheets_client import SheetsClient
from agent.state import StateStore


@click.command()
@click.option(
    "--idle-sleep",
    type=int,
    default=30,
    show_default=True,
    help="Seconds to wait between passes when nothing new was processed",
)
@click.option(
    "--force", is_flag=True,
    help="Re-process even OrgIDs that state.db marks as complete",
)
def main(idle_sleep: int, force: bool) -> None:
    config = load_config()
    _bootstrap.setup_logging(config.log_level)

    sheets = SheetsClient.from_config(config)

    with StateStore(config.state_db_path) as state:
        orch = Orchestrator(config, state)
        while True:
            rows = sheets.read_universities()
            stats = orch.process(rows, force=force)
            click.echo(f"pass: {stats.as_dict()}")
            if stats.done == 0 and stats.failed == 0 and stats.stubbed == 0:
                click.echo("Nothing left to process. Exiting.")
                break
            time.sleep(idle_sleep)


if __name__ == "__main__":
    main()

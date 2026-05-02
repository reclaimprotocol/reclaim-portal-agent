"""Process a single OrgID end-to-end.

Example:
    python scripts/run_single.py --orgid 5819165
    python scripts/run_single.py --orgid 664140 --force --debug --debug-discovery
"""
from __future__ import annotations

import logging

import _bootstrap  # noqa: F401  -- must come before agent.* imports
import click

from agent.config import load_config
from agent.orchestrator import Orchestrator
from agent.sheets_client import SheetsClient
from agent.state import StateStore


@click.command()
@click.option("--orgid", required=True, help="The OrgID to process")
@click.option(
    "--force", is_flag=True,
    help="Re-process even if state.db marks this OrgID as complete",
)
@click.option(
    "--debug", is_flag=True,
    help="DEBUG-level logging across the agent (tc-finder probes, "
         "tc-analyzer keyword matches, etc.)",
)
@click.option(
    "--debug-discovery", is_flag=True,
    help="Verbose Stage A discovery: every search/probe candidate, "
         "every per-candidate validation decision, every Playwright trigger",
)
def main(orgid: str, force: bool, debug: bool, debug_discovery: bool) -> None:
    config = load_config()
    if debug or debug_discovery:
        _bootstrap.setup_logging("DEBUG")
        logging.getLogger("googleapiclient").setLevel(logging.WARNING)
        logging.getLogger("urllib3").setLevel(logging.WARNING)
        if debug_discovery:
            # Discovery already logs at INFO for most candidate decisions.
            # DEBUG turns on per-probe-attempt records.
            logging.getLogger("agent.stages.discovery").setLevel(logging.DEBUG)
            logging.getLogger("agent.stages.discovery_rules").setLevel(logging.DEBUG)
    else:
        _bootstrap.setup_logging(config.log_level)

    target_orgid = str(orgid).strip()

    sheets = SheetsClient.from_config(config)
    rows = sheets.read_universities()
    target = [r for r in rows if SheetsClient.extract_orgid(r) == target_orgid]

    if not target:
        sample = [SheetsClient.extract_orgid(r) for r in rows[:5]]
        click.echo(
            f"OrgID {target_orgid} not found. "
            f"Total Universities rows: {len(rows)}. "
            f"Sample of available OrgIDs: {sample}",
            err=True,
        )
        raise click.ClickException(f"OrgID {target_orgid} not found in sheet")

    with StateStore(config.state_db_path) as state:
        orch = Orchestrator(config, state)
        stats = orch.process(target, force=force)
        click.echo(f"result: {stats.as_dict()}")


if __name__ == "__main__":
    main()

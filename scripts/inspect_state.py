"""Inspect the SQLite state — summarise what's done, pending, failed.

Example:
    python scripts/inspect_state.py
    python scripts/inspect_state.py --status failed
"""
from __future__ import annotations

from collections import Counter

import _bootstrap  # noqa: F401
import click

from agent.config import load_config
from agent.state import StateStore


@click.command()
@click.option("--status", type=str, default=None, help="Filter to this status only")
def main(status: str | None) -> None:
    config = load_config()
    with StateStore(config.state_db_path) as state:
        rows = state.list_by_status(status) if status else state.all_statuses()
        by_status = Counter(r["status"] for r in rows)
        click.echo(f"total: {len(rows)}  {dict(by_status)}")
        click.echo("-" * 72)
        for r in rows:
            err = f"  err={r['last_error']}" if r["last_error"] else ""
            click.echo(f"{r['orgid']:<12} stage={r['stage']:<14} status={r['status']:<12}{err}")


if __name__ == "__main__":
    main()

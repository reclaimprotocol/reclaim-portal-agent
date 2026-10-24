"""Run GenieV3 against a Google Sheet and write results to its Portals tab.

The source tab may be a normal research sheet with ``Org name`` and
``Domain`` columns.  ``SheetsClient`` normalises those fields and generates a
stable ID if the sheet has no Org ID column.  Each source organisation gets
one upserted output row in the separate Portals worksheet.

Examples:
    python scripts/run_v3_sheet.py --limit 3
    python scripts/run_v3_sheet.py --all --concurrency 2
"""
from __future__ import annotations

import asyncio
import logging
import time

import _bootstrap  # noqa: F401
import click

from agent.config import load_config
from agent.lookup import find_portals
from agent.sheets_client import SheetsClient

logger = logging.getLogger("genie.v3_sheet")


def _unique_urls(portals: list[dict], key: str) -> list[str]:
    seen: set[str] = set()
    values: list[str] = []
    for portal in portals:
        value = str(portal.get(key) or "").strip()
        if value and value not in seen:
            seen.add(value)
            values.append(value)
    return values


def _write_output(sheets: SheetsClient, org_id: str, output: dict) -> None:
    """Serialize one Sheets upsert outside the asyncio event loop."""
    existing = sheets.read_portals_by_orgid(org_id)
    values = [output.get(col, "") for col in SheetsClient.PORTALS_COLUMNS]
    if existing:
        sheets.update_portal_rows([(existing[0][0], values)])
    else:
        sheets.append_portal_rows([output])


async def _process_one(
    row: dict,
    sheets: SheetsClient,
    *,
    portals_only: bool,
    fast: bool,
    row_timeout_seconds: int,
    write_lock: asyncio.Lock,
) -> str:
    org_id = SheetsClient.extract_orgid(row)
    name = str(row.get("SheerID University Name") or "").strip()
    domain = str(row.get("SheerID Website Domain") or "").strip()
    country = str(row.get("Country") or "").strip()
    if not org_id or not name or not domain:
        return f"skipped {org_id or '<no id>'}: missing Org name or Domain"

    try:
        result = await asyncio.wait_for(
            find_portals(
                domain, name=name, country=country,
                portals_only=(portals_only or fast), fast_mode=fast,
            ),
            timeout=row_timeout_seconds if fast else None,
        )
    except TimeoutError:
        result = {
            "domain": domain, "name": name, "portals": [],
            "error": f"fast-pass deadline exceeded ({row_timeout_seconds}s)",
        }
    portals = result.get("portals") or []
    portal_urls = _unique_urls(portals, "url")
    tnc_urls = _unique_urls(portals, "tnc_url")
    error = str(result.get("error") or "").strip()
    if error:
        status = f"V3: {error}"
    elif portal_urls:
        status = f"V3: found {len(portal_urls)} verified student login portal(s)"
    else:
        status = "V3: no verified student login portals found"

    output = {
        "OrgID": org_id,
        "University Name": result.get("name") or name,
        "Portal URLs": "\n".join(portal_urls),
        "T&C URLs": "\n".join(tnc_urls),
        # GenieV3 associates legal pages with portals, but does not run the
        # legacy content-verdict classifier.  Leave that legacy-only value blank.
        "Overall T&C Verdict": "",
        "GenieV3 status": status,
    }
    # Google API calls are synchronous. Running them directly here freezes all
    # active discoveries; use one background writer lane instead.
    async with write_lock:
        await asyncio.to_thread(_write_output, sheets, org_id, output)
    return f"{org_id}: {status}"


@click.command()
@click.option("--limit", type=int, default=20, show_default=True,
              help="Maximum organisations to process.")
@click.option("--all", "all_rows", is_flag=True,
              help="Process every source row instead of using --limit.")
@click.option("--concurrency", type=int, default=None,
              help="Lookups in flight (default: 3 in --fast mode, otherwise 2).")
@click.option("--portals-only", is_flag=True,
              help="Skip legal-page discovery and write only portal URLs.")
@click.option("--fast", is_flag=True,
              help="Bounded first pass; defers legal discovery and deep retries.")
@click.option("--row-timeout-seconds", type=int, default=120, show_default=True,
              help="Hard per-row discovery deadline in --fast mode.")
def main(limit: int, all_rows: bool, concurrency: int | None,
         portals_only: bool, fast: bool, row_timeout_seconds: int) -> None:
    """Discover portals with GenieV3 and upsert them to the output tab."""
    config = load_config()
    _bootstrap.setup_logging(config.log_level)
    sheets = SheetsClient.from_config(config)
    sheets.ensure_portals_header()
    rows = sheets.read_universities()
    if not all_rows:
        rows = rows[:limit]
    if not rows:
        raise click.ClickException("No source rows found")

    async def run() -> list[str]:
        effective_concurrency = concurrency or (3 if fast else 2)
        semaphore = asyncio.Semaphore(max(1, effective_concurrency))
        write_lock = asyncio.Lock()

        async def guarded(row: dict) -> str:
            async with semaphore:
                try:
                    return await _process_one(
                        row, sheets, portals_only=portals_only, fast=fast,
                        row_timeout_seconds=max(1, row_timeout_seconds),
                        write_lock=write_lock,
                    )
                except Exception as exc:  # noqa: BLE001
                    org_id = SheetsClient.extract_orgid(row) or "<no id>"
                    logger.exception("GenieV3 failed for %s", org_id)
                    return f"{org_id}: V3 error: {type(exc).__name__}: {exc}"

        return await asyncio.gather(*(guarded(row) for row in rows))

    started = time.monotonic()
    outcomes = asyncio.run(run())
    for outcome in outcomes:
        click.echo(outcome)
    click.echo(f"GenieV3 complete: {len(outcomes)} organisation(s) in {time.monotonic() - started:.1f}s")


if __name__ == "__main__":
    main()

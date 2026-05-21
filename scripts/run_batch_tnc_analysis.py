"""Phased pipeline: Stage C.2 (T&C analysis / verdict) for a contiguous
range of Google Sheet rows that ALREADY have T&C URLs populated. Does
NOT re-run discovery or T&C URL finding — use
`scripts/run_batch_discovery.py` first to populate Portal URLs +
T&C URLs.

--start and --end refer to ACTUAL Google Sheet row numbers as visible
in the sheet UI (row 1 = header, row 2 = first data row). They are
NOT array indices and NOT "first N universities". `sheets_client.py`
does not expose row numbers directly — `read_universities()` returns
data rows in sheet order, so `rows[i]` is sheet row `i + 2` (row 1 is
the header).

Examples:
    python scripts/run_batch_tnc_analysis.py --start 2 --end 21
    python scripts/run_batch_tnc_analysis.py --start 45 --end 64
    python scripts/run_batch_tnc_analysis.py --start 2 --end 21 --force
    python scripts/run_batch_tnc_analysis.py --start 2 --end 21 --blank-only
"""
from __future__ import annotations

import logging
import time
from typing import Any

import _bootstrap  # noqa: F401  -- must come before agent.* imports
import click
from anthropic import RateLimitError
from googleapiclient.errors import HttpError

from agent.config import load_config
from agent.sheets_client import SheetsClient
from agent.stages import tc_analyzer
from agent.state import StateStore

logger = logging.getLogger(__name__)


@click.command()
@click.option(
    "--start", type=int, required=True,
    help="First sheet row number to process (row 2 = first data row)",
)
@click.option(
    "--end", type=int, required=True,
    help="Last sheet row number to process (inclusive)",
)
@click.option(
    "--force", is_flag=True,
    help="Re-run analysis even if Overall T&C Verdict is filled. Also "
         "bypasses the per-URL analyzer cache so updated scoring logic "
         "takes effect.",
)
@click.option(
    "--blank-only", "blank_only", is_flag=True,
    help="Only process rows whose Overall T&C Verdict cell is blank "
         "(skip rows that already have a verdict). Independent of "
         "--force.",
)
def main(start: int, end: int, force: bool, blank_only: bool) -> None:
    if start > end:
        raise click.ClickException(
            f"--start ({start}) must be <= --end ({end})"
        )
    if start < 2:
        raise click.ClickException(
            "Row 2 is the first data row (row 1 is the header)"
        )

    config = load_config()
    _bootstrap.setup_logging(config.log_level)

    sheets = SheetsClient.from_config(config)
    sheets.ensure_portals_header()

    all_rows = sheets.read_universities()
    # `read_universities()` returns data rows in sheet order. Row 1 is the
    # header, so `all_rows[i]` corresponds to sheet row `i + 2`.
    in_range: list[tuple[int, dict[str, Any]]] = [
        (i + 2, r) for i, r in enumerate(all_rows)
        if start <= (i + 2) <= end
    ]

    click.echo(
        f"Processing sheet rows {start}–{end} ({len(in_range)} rows)"
    )
    if not in_range:
        click.echo("No rows in range. Done.")
        return

    stats = {
        "analyzed": 0,
        "skipped": 0,
        "failed": 0,
        "no_orgid": 0,
        "no_tc_url": 0,
    }

    with StateStore(config.state_db_path) as state:
        for sheet_row, row in in_range:
            orgid = SheetsClient.extract_orgid(row)
            if not orgid:
                stats["no_orgid"] += 1
                click.echo(
                    f"[sheet_row={sheet_row}] no OrgID — skipping",
                    err=True,
                )
                continue

            existing_sheet_rows = sheets.read_portals_by_orgid(orgid)
            if not existing_sheet_rows:
                stats["no_tc_url"] += 1
                click.echo(
                    f"[sheet_row={sheet_row}] [{orgid}] no Portals row "
                    f"yet — run run_batch_discovery.py first; skipping"
                )
                continue

            portal_row_num, portal_row = existing_sheet_rows[0]
            tc_urls = _parse_tc_urls(portal_row.get("T&C URLs", ""))
            existing_verdict = str(
                portal_row.get("Overall T&C Verdict", "")
            ).strip()
            uni_name = (
                str(row.get("SheerID University Name", "")).strip()
                or str(portal_row.get("University Name", "")).strip()
            )

            # Skip rules — literal reading of the spec:
            #   (a) no T&C URL AND not --force                  → skip
            #   (b) verdict filled AND not --force AND not --blank-only
            #                                                    → skip
            if not tc_urls and not force:
                stats["no_tc_url"] += 1
                click.echo(
                    f"[sheet_row={sheet_row}] [{orgid}] no T&C URL in "
                    f"sheet — skipping (pass --force to retry)"
                )
                continue
            if existing_verdict and not force and not blank_only:
                stats["skipped"] += 1
                click.echo(
                    f"[sheet_row={sheet_row}] [{orgid}] verdict already "
                    f"filled ({existing_verdict!r}) — skipping"
                )
                continue

            click.echo(
                f"[sheet_row={sheet_row}] [{orgid}] {uni_name} → "
                f"analyzing {len(tc_urls)} T&C URL(s)"
            )

            try:
                overall = _analyze_one(
                    sheet_row=sheet_row,
                    orgid=orgid,
                    tc_urls=tc_urls,
                    state=state,
                    config=config,
                    force=force,
                )
            except RateLimitError:
                _rate_limit_sleep(60, "Claude API")
                stats["failed"] += 1
                continue
            except HttpError as err:
                if getattr(err, "resp", None) is not None and err.resp.status == 429:
                    _rate_limit_sleep(60, "Google Sheets API")
                else:
                    logger.exception(
                        "[sheet_row=%d] [%s] HttpError",
                        sheet_row, orgid,
                    )
                stats["failed"] += 1
                continue
            except Exception:
                logger.exception(
                    "[sheet_row=%d] [%s] analyzer raised",
                    sheet_row, orgid,
                )
                stats["failed"] += 1
                continue

            new_row = {
                "OrgID": orgid,
                "University Name": uni_name,
                "Portal URLs": str(portal_row.get("Portal URLs", "")),
                "T&C URLs": "\n".join(tc_urls),
                "Overall T&C Verdict": overall,
            }
            values = [
                new_row.get(col, "") for col in SheetsClient.PORTALS_COLUMNS
            ]
            sheets.update_portal_rows([(portal_row_num, values)])
            stats["analyzed"] += 1
            click.echo(
                f"[sheet_row={sheet_row}] [{orgid}] {uni_name} → "
                f"verdict={overall!r}"
            )

    click.echo(
        f"\nSummary: total rows in range={len(in_range)}, "
        f"analyzed={stats['analyzed']}, skipped={stats['skipped']}, "
        f"failed={stats['failed']}, no_orgid={stats['no_orgid']}, "
        f"no_tc_url={stats['no_tc_url']}"
    )


def _rate_limit_sleep(seconds: float, reason: str) -> None:
    logger.warning("Rate-limit pause (%s): sleeping %.0fs", reason, seconds)
    time.sleep(seconds)


def _parse_tc_urls(cell: Any) -> list[str]:
    """Split the multi-line `T&C URLs` cell into a clean URL list."""
    return [
        line.strip()
        for line in str(cell or "").split("\n")
        if line.strip()
    ]


def _analyze_one(
    *,
    sheet_row: int,
    orgid: str,
    tc_urls: list[str],
    state: StateStore,
    config: Any,
    force: bool,
) -> str:
    """Run `tc_analyzer.analyze_tc_url` over each T&C URL and aggregate.

    Mirrors `run_tnc_only.py`'s short-circuit branch: each URL is
    fetched (cache-hit when possible), keyword-scored, and folded into
    `aggregate_verdicts`. An empty `tc_urls` list (only reachable when
    `--force` bypassed the no-T&C-URL skip) collapses to
    "Yes (No T&C Found)" — the same default the full pipeline uses.
    """
    if not tc_urls:
        return tc_analyzer.aggregate_verdicts([])

    verdicts: list[str] = []
    for tc_url in tc_urls:
        result = tc_analyzer.analyze_tc_url(
            tc_url=tc_url,
            state=state,
            user_agent=config.user_agent,
            http_timeout=config.http_timeout_seconds,
            orgid=orgid,
            force_refresh=force,
        )
        verdict = str(result.get("verdict") or "Yes (No T&C Found)")
        verdicts.append(verdict)
        logger.info(
            "[sheet_row=%d] [%s] %s → %s",
            sheet_row, orgid, tc_url, verdict,
        )
    return tc_analyzer.aggregate_verdicts(verdicts)


if __name__ == "__main__":
    main()

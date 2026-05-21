"""Phased pipeline: Stage A (discovery) + Stage C.1 (T&C URL finding) for
a contiguous range of Google Sheet rows. Does NOT run Stage C.2 (T&C
analysis / verdict). Use this to bulk-populate Portal URLs + T&C URLs;
then run `scripts/run_batch_tnc_analysis.py` to fill in verdicts.

--start and --end refer to ACTUAL Google Sheet row numbers as visible
in the sheet UI (row 1 = header, row 2 = first data row). They are
NOT array indices and NOT "first N universities". `sheets_client.py`
does not expose row numbers directly — `read_universities()` returns
data rows in sheet order, so `rows[i]` is sheet row `i + 2` (row 1 is
the header).

Examples:
    python scripts/run_batch_discovery.py --start 2 --end 21
    python scripts/run_batch_discovery.py --start 45 --end 64
    python scripts/run_batch_discovery.py --start 2 --end 21 --force
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
from agent.pipeline import PipelineContext
from agent.sheets_client import SheetsClient
from agent.stages import discovery, tc_finder
from agent.stages.js_renderer import JSRenderer
from agent.stages.sheet_writer import _portal_sort_key
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
    help="Re-run discovery + T&C find even if the Portal URLs cell is "
         "already populated. Also bypasses the state.db cache for "
         "discovery / tc_finder so re-run uses fresh logic.",
)
def main(start: int, end: int, force: bool) -> None:
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

    stats = {"done": 0, "skipped": 0, "failed": 0, "no_orgid": 0}

    js_renderer: JSRenderer | None = None
    if config.enable_js_rendering:
        js_renderer = JSRenderer(
            timeout_seconds=config.js_rendering_timeout_seconds,
            user_agent=config.user_agent,
        )

    try:
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
                has_portals = bool(
                    existing_sheet_rows
                    and str(existing_sheet_rows[0][1].get("Portal URLs", "")).strip()
                )
                if has_portals and not force:
                    stats["skipped"] += 1
                    click.echo(
                        f"[sheet_row={sheet_row}] [{orgid}] already has "
                        f"Portal URLs — skipping (pass --force to re-run)"
                    )
                    continue

                uni_name = str(row.get("SheerID University Name", "")).strip()
                click.echo(
                    f"[sheet_row={sheet_row}] [{orgid}] {uni_name} → running"
                )

                try:
                    n_portals, n_tc, tc_first = _run_one(
                        sheet_row=sheet_row,
                        orgid=orgid,
                        row=row,
                        sheets=sheets,
                        state=state,
                        js_renderer=js_renderer,
                        config=config,
                        force=force,
                        existing_sheet_rows=existing_sheet_rows,
                    )
                    stats["done"] += 1
                    click.echo(
                        f"[sheet_row={sheet_row}] [{orgid}] {uni_name} → "
                        f"portals={n_portals} tc_urls={n_tc} "
                        f"tc_url={tc_first or '(none)'}"
                    )
                except RateLimitError:
                    _rate_limit_sleep(60, "Claude API")
                    stats["failed"] += 1
                except HttpError as err:
                    if getattr(err, "resp", None) is not None and err.resp.status == 429:
                        _rate_limit_sleep(60, "Google Sheets API")
                    else:
                        logger.exception(
                            "[sheet_row=%d] [%s] HttpError",
                            sheet_row, orgid,
                        )
                    stats["failed"] += 1
                except Exception:
                    logger.exception(
                        "[sheet_row=%d] [%s] pipeline raised",
                        sheet_row, orgid,
                    )
                    stats["failed"] += 1
    finally:
        if js_renderer is not None:
            js_renderer.close()

    click.echo(
        f"\nSummary: total rows in range={len(in_range)}, "
        f"done={stats['done']}, skipped={stats['skipped']}, "
        f"failed={stats['failed']}, no_orgid={stats['no_orgid']}"
    )


def _rate_limit_sleep(seconds: float, reason: str) -> None:
    logger.warning("Rate-limit pause (%s): sleeping %.0fs", reason, seconds)
    time.sleep(seconds)


def _run_one(
    *,
    sheet_row: int,
    orgid: str,
    row: dict[str, Any],
    sheets: SheetsClient,
    state: StateStore,
    js_renderer: JSRenderer | None,
    config: Any,
    force: bool,
    existing_sheet_rows: list[tuple[int, dict[str, Any]]],
) -> tuple[int, int, str]:
    """Discovery + tc_finder for one OrgID; write the sheet row (no verdict).

    Returns `(n_portal_urls, n_unique_tc_urls, first_tc_url_or_empty)`.
    Preserves any existing `Overall T&C Verdict` cell — verdict updates
    are owned by `run_batch_tnc_analysis.py`. Rows whose discovery
    yields zero portals are recorded in state.db (`failed_discovery`)
    and skipped — no sheet write happens so an existing row is not
    blanked.
    """
    ctx = PipelineContext(
        orgid=orgid, row=row,
        deps={
            "state": state,
            "js_renderer": js_renderer,
            "user_agent": config.user_agent,
            "http_timeout": config.http_timeout_seconds,
        },
    )

    # ---- Stage A: discovery -----------------------------------------
    discovery_result: dict[str, Any] | None = None
    budget_tripped = False
    if not force:
        cached = state.get_result(orgid, "discovery")
        if isinstance(cached, dict):
            discovery_result = cached
            logger.info("[%s] stage=discovery cached; skipping", orgid)
    if discovery_result is None:
        state.mark_stage(orgid, "discovery", "in_progress")
        discovery_result = discovery.run(ctx)
        # Mirror pipeline.run_pipeline's budget-tripped handling: do not
        # cache a partial/empty discovery result that came back from a
        # wall-clock-budget abort (next run should retry from scratch).
        budget_tripped = bool(discovery_result.get("completed_with_timeout"))
        if not budget_tripped:
            state.save_result(orgid, "discovery", discovery_result)
    ctx.results["discovery"] = discovery_result

    portals = discovery_result.get("portals") or []
    if not portals:
        reason = discovery_result.get("reason") or "no portals found"
        if budget_tripped:
            # Non-terminal: leave the OrgID retryable on the next run.
            state.mark_stage(
                orgid, "discovery", "budget_tripped",
                error="discovery wall-clock budget exceeded",
            )
        else:
            state.mark_final(
                orgid, status="failed_discovery", stage="discovery",
                error=str(reason),
            )
        click.echo(
            f"[sheet_row={sheet_row}] [{orgid}] discovery found 0 portals "
            f"({reason}); leaving sheet row untouched",
            err=True,
        )
        return 0, 0, ""

    # ---- Stage C.1: tc_finder ---------------------------------------
    tc_finder_result: dict[str, Any] | None = None
    if not force:
        cached = state.get_result(orgid, "tc_finder")
        if isinstance(cached, dict):
            tc_finder_result = cached
            logger.info("[%s] stage=tc_finder cached; skipping", orgid)
    if tc_finder_result is None:
        state.mark_stage(orgid, "tc_finder", "in_progress")
        tc_finder_result = tc_finder.run(ctx)
        state.save_result(orgid, "tc_finder", tc_finder_result)
    ctx.results["tc_finder"] = tc_finder_result

    # ---- Compose + write the Portals-tab row ------------------------
    sorted_portals = sorted(
        (p for p in portals if (p.get("url") or "").strip()),
        key=_portal_sort_key,
    )
    portal_urls: list[str] = [p["url"] for p in sorted_portals]

    findings = tc_finder_result.get("tc_findings") or []
    tc_urls_seen: set[str] = set()
    tc_urls_ordered: list[str] = []
    for f in findings:
        tc_url = str(f.get("tc_url") or "").strip()
        if not tc_url:
            continue
        key = tc_url.lower().rstrip("/")
        if key not in tc_urls_seen:
            tc_urls_seen.add(key)
            tc_urls_ordered.append(tc_url)

    university_name = (
        str(discovery_result.get("university_name") or "").strip()
        or str(row.get("SheerID University Name", "")).strip()
    )

    # Preserve any existing verdict — this script doesn't run the
    # analyzer, so it has no business overwriting a previously
    # computed verdict.
    preserved_verdict = ""
    if existing_sheet_rows:
        preserved_verdict = str(
            existing_sheet_rows[0][1].get("Overall T&C Verdict", "")
        ).strip()

    new_row = {
        "OrgID": orgid,
        "University Name": university_name,
        "Portal URLs": "\n".join(portal_urls),
        "T&C URLs": "\n".join(tc_urls_ordered),
        "Overall T&C Verdict": preserved_verdict,
    }
    values = [new_row.get(col, "") for col in SheetsClient.PORTALS_COLUMNS]

    if existing_sheet_rows:
        row_num, _ = existing_sheet_rows[0]
        sheets.update_portal_rows([(row_num, values)])
    else:
        sheets.append_portal_rows([new_row])

    # mark_stage (not mark_final) — verdict is still pending, so the
    # OrgID is not yet "done" from the full-pipeline perspective.
    state.mark_stage(orgid, "tc_finder", "done")

    return (
        len(portal_urls),
        len(tc_urls_ordered),
        tc_urls_ordered[0] if tc_urls_ordered else "",
    )


if __name__ == "__main__":
    main()

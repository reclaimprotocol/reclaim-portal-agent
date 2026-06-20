#!/usr/bin/env python3
"""T&C analysis for the PIVOTED "17June-Final" tab.

This tab is organised by T&C, not by university. The layout is:

    A: TnC Category                              (vendor/family label, e.g. "Samarth")
    B: ReclaimProtocol Terms of Use URL          <- INPUT (one or more URLs, \\n-separated)
    C: Login Page Url covered under TnC          (login portals governed by this T&C)
    D: SheerID OrgIDs                            (orgids covered by this T&C)
    E: Reclaim Protocol Terms of use AI-Review   <- OUTPUT (verdict)
    F: AI-Review Reason                          <- OUTPUT (reasoning)
    G: SheerID terms of use review
    H: Notes

A row that carries a column-B URL DEFINES a T&C group: we analyze that
URL set (in-depth via the Claude legal pass — run with TC_ANALYZER_MODE=hybrid
TC_FORCE_CLAUDE=1) and write the resulting verdict/reason to E/F. The rows
*below* it that have NO column-B URL (only their own C login page + D orgids)
are the additional universities **covered under that same T&C** — they inherit
the group's verdict/reason into E/F too. A fully-empty row resets the group so
verdicts never bleed across the gap between blocks.

Columns are located by header name (the user re-orders/renames columns — col C
was just renamed). Idempotent: rows that already have a column-E verdict are
left untouched unless --force.

Reuses run_portal_sheet's hardwired PORTAL_SHEET_ID + _write_cell, so it can
only ever write to the office consolidation sheet.

Usage:
    python scripts/_run_17june_final_tnc.py --dry-run
    TC_ANALYZER_MODE=hybrid TC_FORCE_CLAUDE=1 TC_CLAUDE_MAX_CALLS=300 \\
        python scripts/_run_17june_final_tnc.py
    python scripts/_run_17june_final_tnc.py --start 2 --end 15 --force
"""
from __future__ import annotations

import logging

import _bootstrap  # noqa: F401  -- must come before agent.* imports
import click

from agent.config import load_config
from agent.sheets_client import SheetsClient, _col_letter
from agent.stages import tc_analyzer
from agent.stages.js_renderer import JSRenderer
from agent.state import StateStore
from _run_dated_tab_portals import _resolve_tab_title
from _run_dated_tab_tnc import (
    REASON_HEADER,
    TC_URL_HEADER,
    UNREADABLE_REASON,
    VERDICT_HEADER,
    _find_col,
    _parse_tc_urls,
    _reason_for,
)
from run_portal_sheet import PORTAL_SHEET_ID, _write_cell

logger = logging.getLogger("run_17june_final_tnc")

DEFAULT_TAB = "17June-Final"


def _analyze_group(tc_urls, *, state, config, js_renderer, orgid, force):
    """Analyze a T&C group's URL set and return (overall_verdict, reason)."""
    pairs: list[tuple[str, str]] = []
    results: list[dict] = []
    for tc_url in tc_urls:
        try:
            result = tc_analyzer.analyze_tc_url(
                tc_url=tc_url,
                state=state,
                user_agent=config.user_agent,
                http_timeout=config.http_timeout_seconds,
                orgid=orgid,
                mode=config.tc_analyzer_mode,
                force_refresh=force,
                js_renderer=js_renderer,
            )
        except Exception:
            logger.exception("analyzer raised for %s; treating as no-verdict", tc_url)
            continue
        results.append(result)
        v = str(result.get("verdict") or "Yes (No T&C Found)")
        pairs.append((tc_url, v))
        logger.info("    %s → %s", tc_url, v)

    overall = tc_analyzer.aggregate_verdicts_by_url(pairs)
    # Row HAS T&C URL(s) but every one was unreadable — not a confident "Yes".
    if overall == "Yes (No T&C Found)" and tc_urls:
        overall = "Maybe"
    reason = _reason_for(overall, results)
    return overall, reason


@click.command()
@click.option("--tab", "tab_arg", default=DEFAULT_TAB, show_default=True, help="Tab to process.")
@click.option("--start", type=int, default=None, help="First data row (1-based, incl. header offset — row 2 is first data row).")
@click.option("--end", type=int, default=None, help="Last data row (inclusive).")
@click.option("--force", is_flag=True, help="Re-analyse rows that already have a column-E verdict (also bypasses the per-URL analyzer cache for T&C rows).")
@click.option("--dry-run", is_flag=True, help="Analyse and print, but do NOT write.")
def main(tab_arg: str, start: int | None, end: int | None, force: bool, dry_run: bool) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = load_config()

    sheets = SheetsClient(
        sheet_id=PORTAL_SHEET_ID,
        universities_tab="x", portals_tab="x",  # unused; we read/write cells directly
        credentials_path=config.google_credentials_path,
        token_path=config.google_token_path,
    )
    title = _resolve_tab_title(sheets, tab_arg)
    qtab = f"'{title}'"

    header = (sheets._get_values(qtab, "1:1") or [[]])[0]
    data_rows = sheets._get_values(qtab, "2:100000")

    tc_url_idx = _find_col(header, TC_URL_HEADER, 1)        # B
    verdict_idx = _find_col(header, VERDICT_HEADER, 4)      # E
    reason_idx = _find_col(header, REASON_HEADER, 5)        # F
    verdict_letter = _col_letter(verdict_idx + 1)
    reason_letter = _col_letter(reason_idx + 1)
    pad_to = max(tc_url_idx, verdict_idx, reason_idx, 3) + 1  # incl. C(2) & D(3)

    from agent.config import TC_FORCE_CLAUDE  # report the active depth setting

    click.echo("=" * 70)
    click.echo(f"  Spreadsheet : office consolidation ({PORTAL_SHEET_ID})")
    click.echo(f"  Tab         : {title!r}")
    click.echo(f"  In  col     : {header[tc_url_idx]!r} ({_col_letter(tc_url_idx+1)})")
    click.echo(f"  Out cols    : verdict={header[verdict_idx]!r} ({verdict_letter})  "
               f"reason={header[reason_idx]!r} ({reason_letter})")
    click.echo(f"  Data rows   : {len(data_rows)}")
    click.echo(f"  Analyzer    : mode={config.tc_analyzer_mode}  force_claude={TC_FORCE_CLAUDE}")
    click.echo(f"  Mode        : {'DRY RUN (no writes)' if dry_run else f'WRITE {verdict_letter}+{reason_letter} in-place'} force={force}")
    click.echo("=" * 70)

    js_renderer: JSRenderer | None = None
    if config.enable_js_rendering:
        js_renderer = JSRenderer(
            timeout_seconds=config.js_rendering_timeout_seconds,
            user_agent=config.user_agent,
        )

    groups = covered = skipped = blank = 0
    verdict_counts: dict[str, int] = {}
    # The verdict/reason of the T&C group currently in effect (inherited by
    # blank-B continuation rows below it). Reset to None on a fully-empty row.
    cur_verdict: str | None = None
    cur_reason: str = ""

    try:
      with StateStore(config.state_db_path) as state:
        for ridx, raw in enumerate(data_rows):
            sheet_row = ridx + 2
            if start is not None and sheet_row < start:
                continue
            if end is not None and sheet_row > end:
                break

            padded = list(raw) + [""] * (pad_to - len(raw))
            cat = str(padded[0]).strip()
            tc_cell = str(padded[tc_url_idx]).strip()
            login_cell = str(padded[2]).strip()
            orgid_cell = str(padded[3]).strip()
            existing = str(padded[verdict_idx]).strip()

            # Fully-empty separator row → end of the current group.
            if not (cat or tc_cell or login_cell or orgid_cell):
                cur_verdict = None
                cur_reason = ""
                blank += 1
                continue

            tc_urls = _parse_tc_urls(tc_cell)

            if tc_urls:
                # This row DEFINES a T&C group. Analyze it (unless already
                # done and not --force) and make it the verdict in effect.
                label = cat or login_cell or f"row {sheet_row}"
                if existing and not force:
                    cur_verdict, cur_reason = existing, str(padded[reason_idx]).strip()
                    skipped += 1
                    logger.info("[row %d] %s — group already %r, reusing for coverage", sheet_row, label, existing)
                    continue
                orgid = (orgid_cell.split(",")[0].strip() or f"row:{sheet_row}")
                logger.info("[row %d] T&C group %r — analysing %d URL(s)", sheet_row, label, len(tc_urls))
                cur_verdict, cur_reason = _analyze_group(
                    tc_urls, state=state, config=config, js_renderer=js_renderer,
                    orgid=orgid, force=force,
                )
                groups += 1
                verdict_counts[cur_verdict] = verdict_counts.get(cur_verdict, 0) + 1
                logger.info("[row %d] %s → %s", sheet_row, label, cur_verdict)
            else:
                # No column-B URL: a university COVERED under the T&C above.
                if cur_verdict is None:
                    logger.warning("[row %d] no T&C URL and no group in effect — leaving blank", sheet_row)
                    continue
                if existing and not force:
                    skipped += 1
                    continue
                covered += 1
                logger.info("[row %d] covered by group → %s", sheet_row, cur_verdict)

            if cur_verdict is None:
                continue
            if dry_run:
                click.echo(f"  DRY [{sheet_row}] {cur_verdict!r}  reason={cur_reason[:60]!r}")
            else:
                _write_cell(sheets, qtab, verdict_letter, sheet_row, cur_verdict)
                _write_cell(sheets, qtab, reason_letter, sheet_row, cur_reason)
    finally:
        if js_renderer is not None:
            js_renderer.close()

    click.echo("-" * 70)
    click.echo(f"Done. groups={groups} covered_rows={covered} skipped={skipped} empty_rows={blank}")
    if verdict_counts:
        click.echo("Group verdicts: " + ", ".join(f"{k}={v}" for k, v in sorted(verdict_counts.items())))


if __name__ == "__main__":
    main()

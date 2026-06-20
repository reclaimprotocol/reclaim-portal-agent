#!/usr/bin/env python3
"""Run Stage C.2 (T&C analysis) for the *dated* office tabs (e.g. "15June")
and write the verdict into column F ("Reclaim Protocol Terms of use AI-Review").

These dated tabs carry T&C URLs already (column E), so this script does NOT
discover anything — it only fetches + scores the URL(s) already present:

    A: SheerID OrgID
    B: SheerID University Name
    C: SheerID Website Domain
    D: Reclaim Protocol Login Page Url
    E: ReclaimProtocol Terms of Use URL          <- INPUT (one or more URLs, \\n-separated)
    F: Reclaim Protocol Terms of use AI-Review    <- OUTPUT (we write the verdict here)
    G: SheerID terms of use review
    H: Notes

For each row it splits column E into URLs, runs the EXACT same trained
`tc_analyzer.analyze_tc_url` per URL (cached by normalised URL in state.db,
auto-learns vendors), aggregates the per-URL verdicts via
`tc_analyzer.aggregate_verdicts`, and writes the single overall verdict into
column F. Idempotent: rows that already have a column-F value are left
untouched unless --force.

Reuses run_portal_sheet.py's hardwired PORTAL_SHEET_ID and _write_cell, so it
can only ever write to the office consolidation sheet.

Usage:
    python scripts/_run_dated_tab_tnc.py --tab 15June --dry-run
    python scripts/_run_dated_tab_tnc.py --tab 15June
    python scripts/_run_dated_tab_tnc.py --tab 15June --start 2 --end 40
    python scripts/_run_dated_tab_tnc.py --tab 15June --force
"""
from __future__ import annotations

import logging
import re

import _bootstrap  # noqa: F401  -- must come before agent.* imports
import click

from agent.config import load_config
from agent.sheets_client import SheetsClient, _col_letter
from agent.stages import tc_analyzer
from agent.stages.js_renderer import JSRenderer
from agent.state import StateStore
from _run_dated_tab_portals import _resolve_tab_title
from run_portal_sheet import PORTAL_SHEET_ID, _write_cell

logger = logging.getLogger("run_dated_tab_tnc")

# Default 0-based column indices for the dated office tabs. The actual columns
# are resolved by header name at runtime (the user re-orders columns), with
# these as fallbacks.
TC_URL_COL = 4     # E  ("ReclaimProtocol Terms of Use URL")  -- input
VERDICT_COL = 5    # F  ("Reclaim Protocol Terms of use AI-Review")  -- output

# Exact header titles used to locate columns regardless of position.
TC_URL_HEADER = "ReclaimProtocol Terms of Use URL"
VERDICT_HEADER = "Reclaim Protocol Terms of use AI-Review"
REASON_HEADER = "AI-Review Reason"

# When a row has T&C URL(s) but every one is unreadable, the verdict is
# relabeled "Maybe"; this is the reason written alongside it.
UNREADABLE_REASON = (
    "T&C URL present but unreadable (connection timeout / empty body / "
    "scanned-image PDF / HTTP 403) — manual review needed"
)


def _parse_tc_urls(cell: str) -> list[str]:
    """Split the multi-line column-E cell into a clean URL list."""
    return [line.strip() for line in str(cell or "").split("\n") if line.strip()]


def _find_col(header: list, title: str, fallback: int | None = None) -> int | None:
    """Index of the column whose header exactly equals `title` (case/space
    insensitive), else `fallback`."""
    for i, h in enumerate(header):
        if str(h).strip().lower() == title.strip().lower():
            return i
    return fallback


def _reason_for(overall: str, results: list[dict]) -> str:
    """The AI-Review reason to record for a No/Maybe verdict: the reasoning of
    the per-URL analysis that drove the overall verdict; for a relabeled
    unreadable Maybe (every URL came back 'Yes (No T&C Found)') use the
    unreadable message. Empty string for Yes verdicts."""
    if overall not in ("No", "Maybe"):
        return ""
    driver = next((r for r in results if str(r.get("verdict")) == overall), None)
    if driver is not None:
        return str(driver.get("reasoning") or "")
    if overall == "Maybe":
        return UNREADABLE_REASON
    return ""


@click.command()
@click.option("--tab", "tab_arg", required=True, help='Dated tab, e.g. "15June".')
@click.option("--start", type=int, default=None, help="First data row (1-based, excl. header).")
@click.option("--end", type=int, default=None, help="Last data row (inclusive).")
@click.option("--force", is_flag=True, help="Re-analyse rows that already have a column-F verdict (also bypasses the per-URL analyzer cache).")
@click.option("--only-verdict", "only_verdict", default=None, help="Re-analyse ONLY rows whose current column-F equals this (e.g. 'Maybe'). Implies force (re-fetch + bypass cache) for the matched rows.")
@click.option("--orgid", "orgids", multiple=True, help="Only process these OrgID(s) from column A. Comma-separated and/or repeatable. Implies force for the matched rows.")
@click.option("--dry-run", is_flag=True, help="Analyse and print, but do NOT write.")
def main(tab_arg: str, start: int | None, end: int | None, force: bool, only_verdict: str | None, orgids: tuple[str, ...], dry_run: bool) -> None:
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
    qtab = f"'{title}'"  # quoted for A1 ranges (title may contain a space)

    header = (sheets._get_values(qtab, "1:1") or [[]])[0]
    data_rows = sheets._get_values(qtab, "2:100000")

    # Resolve columns by header name (the user re-orders columns).
    tc_url_idx = _find_col(header, TC_URL_HEADER, TC_URL_COL)
    verdict_idx = _find_col(header, VERDICT_HEADER, VERDICT_COL)
    verdict_letter = _col_letter(verdict_idx + 1)
    # The AI-Review Reason column: locate it, or create it at the end.
    reason_idx = _find_col(header, REASON_HEADER)
    if reason_idx is None:
        reason_idx = len(header)
        logger.info("no %r column — creating it at %s1", REASON_HEADER, _col_letter(reason_idx + 1))
        if not dry_run:
            _write_cell(sheets, qtab, _col_letter(reason_idx + 1), 1, REASON_HEADER)
        header.append(REASON_HEADER)
    reason_letter = _col_letter(reason_idx + 1)
    pad_to = max(tc_url_idx, verdict_idx, reason_idx) + 1

    # Accept comma-separated and/or repeated --orgid values.
    orgid_filter = {
        tok.strip()
        for entry in orgids
        for tok in re.split(r"[,\s]+", str(entry))
        if tok.strip()
    }

    click.echo("=" * 70)
    click.echo(f"  Spreadsheet : office consolidation ({PORTAL_SHEET_ID})")
    click.echo(f"  Tab         : {title!r}")
    click.echo(f"  Columns     : in={header[tc_url_idx]!r} ({_col_letter(tc_url_idx+1)})  "
               f"verdict={header[verdict_idx]!r} ({verdict_letter})  "
               f"reason={REASON_HEADER!r} ({reason_letter})")
    click.echo(f"  Data rows   : {len(data_rows)}")
    click.echo(f"  Analyzer    : mode={config.tc_analyzer_mode}")
    click.echo(f"  Mode        : {'DRY RUN (no writes)' if dry_run else f'WRITE {verdict_letter}+{reason_letter} in-place'} force={force}")
    click.echo("=" * 70)

    # JS-render fallback for SPA terms pages whose static HTML is empty —
    # without it those rows score a false "Yes (No T&C Found)".
    js_renderer: JSRenderer | None = None
    if config.enable_js_rendering:
        js_renderer = JSRenderer(
            timeout_seconds=config.js_rendering_timeout_seconds,
            user_agent=config.user_agent,
        )

    processed = skipped = no_url = 0
    verdict_counts: dict[str, int] = {}
    try:
      with StateStore(config.state_db_path) as state:
        for ridx, raw in enumerate(data_rows):
            sheet_row = ridx + 2          # 1-based incl. header
            data_row_no = ridx + 1        # what --start/--end mean
            if start is not None and data_row_no < start:
                continue
            if end is not None and data_row_no > end:
                break

            padded = list(raw) + [""] * (pad_to - len(raw))
            orgid_cell = str(padded[0]).strip()
            name = str(padded[1]).strip()
            tc_cell = str(padded[tc_url_idx]).strip()
            existing = str(padded[verdict_idx]).strip()

            if orgid_filter and orgid_cell not in orgid_filter:
                continue

            tc_urls = _parse_tc_urls(tc_cell)
            if not tc_urls:
                no_url += 1
                logger.info("[row %d] %s — no T&C URL in column E; leaving blank", data_row_no, name)
                continue
            if only_verdict is not None:
                # Targeted re-analysis: process only rows whose current
                # column-F matches, and always re-fetch (bypass cache).
                if existing != only_verdict:
                    skipped += 1
                    continue
            elif existing and not force and not orgid_filter:
                skipped += 1
                logger.info("[row %d] %s — column F already %r, skipping", data_row_no, name, existing)
                continue

            # A targeted --orgid run always re-analyzes the matched rows.
            refresh = force or (only_verdict is not None) or bool(orgid_filter)
            orgid = orgid_cell or f"row:{sheet_row}"
            verdicts: list[str] = []
            results: list[dict] = []
            pairs: list[tuple[str, str]] = []
            for tc_url in tc_urls:
                try:
                    result = tc_analyzer.analyze_tc_url(
                        tc_url=tc_url,
                        state=state,
                        user_agent=config.user_agent,
                        http_timeout=config.http_timeout_seconds,
                        orgid=orgid,
                        mode=config.tc_analyzer_mode,
                        force_refresh=refresh,
                        js_renderer=js_renderer,
                    )
                except Exception:
                    logger.exception("[row %d] %s — analyzer raised for %s; treating as no-verdict", data_row_no, name, tc_url)
                    continue
                results.append(result)
                v = str(result.get("verdict") or "Yes (No T&C Found)")
                verdicts.append(v)
                pairs.append((tc_url, v))
                logger.info("[row %d] %s | %s → %s", data_row_no, name, tc_url, v)

            # URL-aware aggregation: a binding Terms-of-Use page outweighs
            # permissive privacy/disclaimer pages (15June finder audit).
            overall = tc_analyzer.aggregate_verdicts_by_url(pairs)
            # The row HAS T&C URL(s) but every one came back unreadable
            # (HTTP error / empty body even after JS render). That is NOT a
            # confident permissive "Yes" — flag it "Maybe" so it gets a human
            # look instead of silently reading as allowed.
            if overall == "Yes (No T&C Found)" and tc_urls:
                overall = "Maybe"
            # Reason recorded only for No/Maybe (blank on Yes so it never lingers).
            reason = _reason_for(overall, results)
            processed += 1
            verdict_counts[overall] = verdict_counts.get(overall, 0) + 1
            logger.info("[row %d] %s → OVERALL %s (from %d URL(s))", data_row_no, name, overall, len(verdicts))

            if dry_run:
                click.echo(f"  DRY [{data_row_no}] {name}: {overall!r}  reason={reason[:60]!r}")
            else:
                _write_cell(sheets, qtab, verdict_letter, sheet_row, overall)
                # Keep the reason column in sync with the verdict on every
                # processed row (write for No/Maybe, clear otherwise).
                _write_cell(sheets, qtab, reason_letter, sheet_row, reason)
    finally:
        if js_renderer is not None:
            js_renderer.close()

    click.echo("-" * 70)
    click.echo(f"Done. processed={processed} skipped={skipped} no_url={no_url}")
    if verdict_counts:
        click.echo("Verdict breakdown: " + ", ".join(f"{k}={v}" for k, v in sorted(verdict_counts.items())))


if __name__ == "__main__":
    main()

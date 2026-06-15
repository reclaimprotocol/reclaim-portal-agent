#!/usr/bin/env python3
"""Discover-mode T&C for the dated office tabs (e.g. 16June): for each row,
run the NEW parallel finder (`tc_finder.find_all_tc_urls`) to gather every
T&C-type page across the university's own domains AND the login-portal host,
analyze each, aggregate with the terms-page-weighted rule, and write:

    E  ReclaimProtocol Terms of Use URL      <- discovered URL(s), newline-joined
    F  Reclaim Protocol Terms of use AI-Review <- aggregated verdict
    G  AI-Review Reason                        <- reason for No/Maybe (created if absent)

Unlike `_run_dated_tab_tnc.py` (which analyzes the MANUAL col-E URLs), this
populates col E from discovery. Idempotent: rows with a col-F verdict are
skipped unless --force. Columns are located by header name.

Usage:
    python scripts/_run_dated_tab_tnc_discover.py --tab 16June --start 1 --end 20 --dry-run
    TC_ANALYZER_MODE=hybrid TC_CLAUDE_MAX_CALLS=300 python scripts/_run_dated_tab_tnc_discover.py --tab 16June --start 1 --end 20
"""
from __future__ import annotations

import logging
import re
import time

import _bootstrap  # noqa: F401
import click

from agent.config import load_config
from agent.sheets_client import SheetsClient, _col_letter
from agent.stages import tc_analyzer, tc_finder
from agent.stages.js_renderer import JSRenderer
from agent.state import StateStore
from _run_dated_tab_portals import _resolve_tab_title
from _run_dated_tab_tnc import (
    REASON_HEADER, TC_URL_HEADER, VERDICT_HEADER, _find_col, _reason_for,
)
from run_portal_sheet import PORTAL_SHEET_ID, _write_cell

logger = logging.getLogger("run_dated_tab_tnc_discover")

NAME_COL = 1   # B
DOMAIN_COL = 2  # C
PORTAL_COL = 3  # D
PER_ROW_FINDER_BUDGET = 70  # seconds for find_all_tc_urls per university


@click.command()
@click.option("--tab", "tab_arg", required=True, help='Dated tab, e.g. "16June".')
@click.option("--start", type=int, default=None, help="First data row (1-based, excl. header).")
@click.option("--end", type=int, default=None, help="Last data row (inclusive).")
@click.option("--force", is_flag=True, help="Re-discover rows that already have a verdict.")
@click.option("--dry-run", is_flag=True, help="Discover + analyze + print, but do NOT write.")
def main(tab_arg, start, end, force, dry_run):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = load_config()
    sheets = SheetsClient(
        sheet_id=PORTAL_SHEET_ID, universities_tab="x", portals_tab="x",
        credentials_path=config.google_credentials_path, token_path=config.google_token_path,
    )
    title = _resolve_tab_title(sheets, tab_arg)
    qtab = f"'{title}'"
    header = (sheets._get_values(qtab, "1:1") or [[]])[0]
    data_rows = sheets._get_values(qtab, "2:100000")

    tc_url_idx = _find_col(header, TC_URL_HEADER, 4)
    verdict_idx = _find_col(header, VERDICT_HEADER, 5)
    reason_idx = _find_col(header, REASON_HEADER)
    if reason_idx is None:
        reason_idx = len(header)
        if not dry_run:
            _write_cell(sheets, qtab, _col_letter(reason_idx + 1), 1, REASON_HEADER)
        header.append(REASON_HEADER)
    tc_letter, verdict_letter, reason_letter = (
        _col_letter(tc_url_idx + 1), _col_letter(verdict_idx + 1), _col_letter(reason_idx + 1),
    )
    pad_to = max(PORTAL_COL, tc_url_idx, verdict_idx, reason_idx) + 1

    click.echo("=" * 70)
    click.echo(f"  Tab         : {title!r}  ({len(data_rows)} data rows)")
    click.echo(f"  Write cols  : E={tc_letter}(urls) verdict={verdict_letter} reason={reason_letter}")
    click.echo(f"  Analyzer    : mode={config.tc_analyzer_mode}")
    click.echo(f"  Mode        : {'DRY RUN' if dry_run else 'WRITE in-place'} force={force}")
    click.echo("=" * 70)

    js_renderer = None
    if config.enable_js_rendering:
        js_renderer = JSRenderer(timeout_seconds=config.js_rendering_timeout_seconds, user_agent=config.user_agent)

    processed = skipped = found = empty = 0
    counts: dict[str, int] = {}
    try:
        with StateStore(config.state_db_path) as state:
            for ridx, raw in enumerate(data_rows):
                sheet_row = ridx + 2
                data_row_no = ridx + 1
                if start is not None and data_row_no < start:
                    continue
                if end is not None and data_row_no > end:
                    break
                p = list(raw) + [""] * pad_to
                name = str(p[NAME_COL]).strip()
                if not name:
                    continue
                if str(p[verdict_idx]).strip() and not force:
                    skipped += 1
                    logger.info("[row %d] %s — verdict already filled; skipping", data_row_no, name)
                    continue

                domains = [d.strip() for d in re.split(r"[,\s]+", str(p[DOMAIN_COL])) if d.strip()]
                portal_cell = str(p[PORTAL_COL]).strip()
                portals = []
                if portal_cell.startswith("http"):
                    portals = [{"url": u.strip()} for u in portal_cell.splitlines() if u.strip().startswith("http")]
                orgid = str(p[0]).strip() or f"row:{sheet_row}"

                budget = tc_finder._TCBudget(deadline_at=time.monotonic() + PER_ROW_FINDER_BUDGET)
                logger.info("[row %d] %s — discovering T&C (domains=%s portals=%d)", data_row_no, name, domains, len(portals))
                try:
                    # js_renderer=None for the finder: its candidate validation
                    # runs in worker threads where Playwright's sync API is
                    # thread-bound and only spews greenlet errors. SPA terms
                    # still get rendered at the (sequential) analyze step below.
                    discovered = tc_finder.find_all_tc_urls(
                        portals=portals, uni_domains=domains, js_renderer=None,
                        user_agent=config.user_agent, http_timeout=config.http_timeout_seconds,
                        orgid=orgid, university_name=name, budget=budget,
                    )
                except Exception:
                    logger.exception("[row %d] %s — finder raised; skipping", data_row_no, name)
                    continue

                urls = [d["tc_url"] for d in discovered]
                pairs = []
                for u in urls:
                    try:
                        res = tc_analyzer.analyze_tc_url(
                            tc_url=u, state=state, user_agent=config.user_agent,
                            http_timeout=config.http_timeout_seconds, orgid=orgid,
                            mode=config.tc_analyzer_mode, js_renderer=js_renderer,
                        )
                    except Exception:
                        logger.exception("[row %d] analyze raised for %s", data_row_no, u)
                        continue
                    pairs.append((u, str(res.get("verdict") or "Yes (No T&C Found)")))

                if pairs:
                    overall = tc_analyzer.aggregate_verdicts_by_url(pairs)
                else:
                    overall = "Yes (No T&C Found)"
                # Reason needs the per-URL analysis dicts; rebuild from cache.
                results = []
                for u, _v in pairs:
                    cached = state.get_tc_cache(tc_analyzer.normalize_tc_url(u))
                    if cached:
                        results.append(cached)
                reason = _reason_for(overall, results)

                processed += 1
                found += 1 if urls else 0
                empty += 0 if urls else 1
                counts[overall] = counts.get(overall, 0) + 1
                logger.info("[row %d] %s → %s (%d url(s): %s)", data_row_no, name, overall, len(urls), urls[:2])

                if dry_run:
                    click.echo(f"  DRY [{data_row_no}] {name}: {overall} | {len(urls)} url(s) | reason={reason[:50]!r}")
                else:
                    if urls:
                        _write_cell(sheets, qtab, tc_letter, sheet_row, "\n".join(urls))
                    _write_cell(sheets, qtab, verdict_letter, sheet_row, overall)
                    _write_cell(sheets, qtab, reason_letter, sheet_row, reason)
    finally:
        if js_renderer is not None:
            js_renderer.close()

    click.echo("-" * 70)
    click.echo(f"Done. processed={processed} found_urls={found} no_url={empty} skipped={skipped}")
    if counts:
        click.echo("Verdicts: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))


if __name__ == "__main__":
    main()

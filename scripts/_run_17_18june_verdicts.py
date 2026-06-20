#!/usr/bin/env python3
"""FAST verdict pass for the "17&18June" tab.

The level columns (C-H) are ALREADY populated with the T&C URL for each row
(one row per T&C). This does NOT discover anything — it just reads the T&C URL
already present on each row, runs the trained analyzer, and writes:

    I  Reclaim Protocol Terms of use AI-Review   <- verdict (Yes/No/Maybe)
    J  AI Review                                  <- reasoning

Per row it takes the first http(s) URL found across columns C..H (the winning
level the user recorded). Analysis runs concurrently (--workers), de-dupes
shared URLs in-memory (all the Samarth rows analyze once), and JS-renders SPA
T&C pages via a per-thread renderer. Writes I+J for every processed row in one
batched update. Idempotent: rows with a col-I verdict are skipped unless --force.

Usage:
    python scripts/_run_17_18june_verdicts.py --dry-run
    TC_ANALYZER_MODE=hybrid TC_FORCE_CLAUDE=1 python scripts/_run_17_18june_verdicts.py --workers 8
"""
from __future__ import annotations

import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import _bootstrap  # noqa: F401
import click

from agent.config import load_config
from agent.sheets_client import SheetsClient, _col_letter
from agent.stages import tc_analyzer
from agent.stages.js_renderer import JSRenderer
from _run_dated_tab_portals import _resolve_tab_title
from _run_dated_tab_tnc import _reason_for
from run_portal_sheet import PORTAL_SHEET_ID

logger = logging.getLogger("run_17_18june_verdicts")

DEFAULT_TAB = "17&18June"
HEADER_ROW = 2
# Columns that may hold the T&C URL (C..H = the level columns), scanned in order.
TCURL_HEADERS = (
    "Exact URL (1)", "Parent URL (2-4)", "Parent domain (5-6)",
    "Linked Parent University Home page (8)", "Vendor Home page (7)",
    "Unlinked Parent University Home page (8)",
)
ORGID_HEADER = "Org id"
VERDICT_HEADER = "Reclaim Protocol Terms of use AI-Review"
REASON_HEADER = "AI Review"


def _find_col(header, title, fallback=None):
    for i, h in enumerate(header):
        if str(h).strip().lower() == title.strip().lower():
            return i
    return fallback


@click.command()
@click.option("--tab", "tab_arg", default=DEFAULT_TAB, show_default=True)
@click.option("--start", type=int, default=None, help="First sheet row (1-based).")
@click.option("--end", type=int, default=None, help="Last sheet row (inclusive).")
@click.option("--orgid", "orgids", multiple=True, help="Only these OrgID(s).")
@click.option("--force", is_flag=True, help="Re-analyse rows that already have a col-I verdict.")
@click.option("--only-verdict", "only_verdict", default=None, help="Re-analyse ONLY rows whose current col-I equals this (e.g. 'Maybe'). Implies force.")
@click.option("--http-timeout", type=int, default=None, help="Override per-request timeout (seconds) — bump for slow hosts.")
@click.option("--js-timeout", type=int, default=None, help="Override JS-render timeout (seconds).")
@click.option("--workers", type=int, default=8, show_default=True)
@click.option("--dry-run", is_flag=True, help="Analyse + print; do NOT write.")
def main(tab_arg, start, end, orgids, force, only_verdict, http_timeout, js_timeout, workers, dry_run):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = load_config()
    from agent.config import TC_FORCE_CLAUDE
    http_to = http_timeout or config.http_timeout_seconds
    js_to = js_timeout or config.js_rendering_timeout_seconds

    sheets = SheetsClient(
        sheet_id=PORTAL_SHEET_ID, universities_tab="x", portals_tab="x",
        credentials_path=config.google_credentials_path, token_path=config.google_token_path,
    )
    title = _resolve_tab_title(sheets, tab_arg)
    qtab = f"'{title}'"
    header = (sheets._get_values(qtab, f"{HEADER_ROW}:{HEADER_ROW}") or [[]])[0]
    orgid_idx = _find_col(header, ORGID_HEADER, 0)
    tcurl_idxs = [_find_col(header, h) for h in TCURL_HEADERS]
    tcurl_idxs = [i for i in tcurl_idxs if i is not None]
    verdict_idx = _find_col(header, VERDICT_HEADER)
    reason_idx = _find_col(header, REASON_HEADER)
    if verdict_idx is None or reason_idx is None or not tcurl_idxs:
        raise SystemExit(f"could not locate columns; header={header}")

    orgid_filter = {tok.strip() for e in orgids for tok in re.split(r"[,\s]+", str(e)) if tok.strip()}
    width = max(orgid_idx, *tcurl_idxs, verdict_idx, reason_idx) + 1

    click.echo("=" * 70)
    click.echo(f"  Tab        : {title!r}")
    click.echo(f"  T&C cols   : {[_col_letter(i+1) for i in tcurl_idxs]}  -> verdict {_col_letter(verdict_idx+1)} / reason {_col_letter(reason_idx+1)}")
    click.echo(f"  Analyzer   : mode={config.tc_analyzer_mode} force_claude={TC_FORCE_CLAUDE} workers={workers}")
    click.echo(f"  Timeouts   : http={http_to}s js_render={js_to}s")
    click.echo(f"  Mode       : {'DRY RUN' if dry_run else 'WRITE I+J'} force={force} only_verdict={only_verdict!r}")
    click.echo("=" * 70)

    data_first = HEADER_ROW + 1
    rows = sheets._get_values(qtab, f"{data_first}:100000")
    jobs: list[tuple[int, str, str]] = []  # (sheet_row, orgid, tc_url)
    for ridx, raw in enumerate(rows):
        sheet_row = data_first + ridx
        if start is not None and sheet_row < start:
            continue
        if end is not None and sheet_row > end:
            continue
        p = list(raw) + [""] * width
        orgid = str(p[orgid_idx]).strip()
        if orgid_filter and orgid not in orgid_filter:
            continue
        existing = str(p[verdict_idx]).strip()
        if only_verdict is not None:
            if existing != only_verdict:
                continue
        elif existing and not force:
            continue
        tc_url = next((str(p[i]).strip() for i in tcurl_idxs if str(p[i]).strip().startswith("http")), "")
        if not tc_url:
            continue
        jobs.append((sheet_row, orgid or f"row:{sheet_row}", tc_url))

    click.echo(f"  Rows to analyse: {len(jobs)}")

    tls = threading.local()
    all_renderers: list[JSRenderer] = []
    rlock = threading.Lock()
    acache: dict[str, tuple[str, str]] = {}
    clock = threading.Lock()

    def _renderer():
        if not config.enable_js_rendering:
            return None
        r = getattr(tls, "r", None)
        if r is None:
            r = JSRenderer(timeout_seconds=js_to, user_agent=config.user_agent)
            tls.r = r
            with rlock:
                all_renderers.append(r)
        return r

    def _analyze(job):
        sheet_row, orgid, tc_url = job
        key = tc_analyzer.normalize_tc_url(tc_url)
        with clock:
            cached = acache.get(key)
        if cached is not None:
            return sheet_row, tc_url, cached[0], cached[1]
        try:
            result = tc_analyzer.analyze_tc_url(
                tc_url=tc_url, state=None, user_agent=config.user_agent,
                http_timeout=http_to, orgid=orgid,
                mode=config.tc_analyzer_mode, force_refresh=True, js_renderer=_renderer(),
            )
        except Exception:
            logger.exception("analyze raised for %s", tc_url)
            result = {"verdict": "Maybe", "reasoning": "analyzer error"}
        verdict = str(result.get("verdict") or "Maybe")
        # A T&C URL that came back unreadable is not a confident permissive Yes.
        if verdict == "Yes (No T&C Found)":
            verdict = "Maybe"
        reason = _reason_for(verdict, [result])
        with clock:
            acache[key] = (verdict, reason)
        return sheet_row, tc_url, verdict, reason

    out: list[tuple[int, str, str]] = []  # (sheet_row, verdict, reason)
    counts: dict[str, int] = {}
    try:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
            futs = {ex.submit(_analyze, j): j for j in jobs}
            for n, fut in enumerate(as_completed(futs), 1):
                try:
                    sheet_row, tc_url, verdict, reason = fut.result()
                except Exception:
                    logger.exception("job failed %s", futs[fut])
                    continue
                counts[verdict] = counts.get(verdict, 0) + 1
                out.append((sheet_row, verdict, reason))
                logger.info("[%d/%d] row %d %s -> %s", n, len(jobs), sheet_row, tc_url[:60], verdict)
    finally:
        for r in all_renderers:
            try:
                r.close()
            except Exception:
                pass

    if dry_run:
        for sheet_row, verdict, reason in sorted(out):
            click.echo(f"  DRY row {sheet_row}: {verdict}  {reason[:70]!r}")
    else:
        # One batched values update for all I and J cells.
        data = []
        vL, rL = _col_letter(verdict_idx + 1), _col_letter(reason_idx + 1)
        for sheet_row, verdict, reason in out:
            data.append({"range": f"{qtab}!{vL}{sheet_row}", "values": [[verdict]]})
            data.append({"range": f"{qtab}!{rL}{sheet_row}", "values": [[reason]]})
        for i in range(0, len(data), 500):  # chunk to keep payloads modest
            sheets._service.spreadsheets().values().batchUpdate(
                spreadsheetId=PORTAL_SHEET_ID,
                body={"valueInputOption": "USER_ENTERED", "data": data[i:i + 500]},
            ).execute()

    click.echo("-" * 70)
    click.echo(f"Done. analysed={len(out)} unique_urls={len(acache)}")
    if counts:
        click.echo("Verdicts: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))


if __name__ == "__main__":
    main()

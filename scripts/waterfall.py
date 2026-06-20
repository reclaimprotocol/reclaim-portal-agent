#!/usr/bin/env python3
"""Waterfall T&C search + verdict for the pivoted level-cascade tabs
("17&18June", "Disqualified-newFormat", "19June", ...).

THE WATERFALL APPROACH: for each (Org id, Portal) seed, search for the governing
T&C at a priority ladder of locations and STOP at the first level that yields a
T&C — C exact -> D parent-URL path -> E parent-domain (subdomain climb) ->
F linked-university (backlink-gated) -> G vendor (relevance-gated) ->
H unlinked-university fallback. It "waterfalls" down the levels. See
agent.stages.tc_levels.find_tc_levels for the engine.

Workflow: you paste (Org id -> col A, Portal -> col B) seed rows into the tab,
then run this. For each seed row it walks the waterfall and fills:

    A  Org id                              (seed, untouched)
    B  Portal                              (seed, untouched)
    C  Exact URL (1)                       \\
    D  Parent URL (2-4)                     |  T&C URL at the WINNING level;
    E  Parent domain (5-6)                  >  levels searched-but-empty = "n/a";
    F  Linked Parent University Home (8)    |  levels after the win = blank.
    G  Vendor Home page (7)                /
    H  Unlinked Parent University Home (8)  university homepage (provenance)
    I  Reclaim Protocol Terms of use AI-Review   verdict (Yes/No/Maybe)
    J  AI Review                                 reasoning
    K  Confidence                                (skipped for now)

ONE ROW PER T&C: if the winning level yields several T&C pages (Terms + Privacy
+ ...), the seed row is reused for the first and extra rows are INSERTED right
below it (copying A+B). Idempotent: a seed row that already has a col-I verdict
is skipped unless --force.

Columns are located by header name (header row = row 2; the row-1 banner is
"TNCs"). Reuses run_portal_sheet's hardwired PORTAL_SHEET_ID, so it can only
write to the office consolidation sheet.

Verdict depth follows the env, same as the 17June-Final run:
    TC_ANALYZER_MODE=hybrid TC_FORCE_CLAUDE=1   -> full Claude legal on every T&C

Usage:
    python scripts/waterfall.py --tab 19June --start 110 --end 174 --no-analysis
    TC_ANALYZER_MODE=hybrid TC_FORCE_CLAUDE=1 python scripts/waterfall.py --tab 17&18June
    python scripts/waterfall.py --tab Disqualified-newFormat --orgid 3826815 --dry-run
"""
from __future__ import annotations

import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import _bootstrap  # noqa: F401
import click

from agent.config import load_config
from agent.sheets_client import SheetsClient, _col_letter
from agent.stages import tc_analyzer, tc_levels
from agent.stages.js_renderer import JSRenderer
from _run_dated_tab_portals import _resolve_tab_title
from _run_dated_tab_tnc import _reason_for
from run_portal_sheet import PORTAL_SHEET_ID

logger = logging.getLogger("waterfall")

DEFAULT_TAB = "17&18June"
HEADER_ROW = 2  # row 1 is the merged "TNCs" banner

ORGID_HEADER = "Org id"
PORTAL_HEADER = "Portal"
# Level column -> exact header text in the tab. Order = cascade order.
# `unlinked_uni` (column H) is the last-resort fallback: the parent university's
# T&C, recorded only when C-G found nothing.
LEVEL_HEADERS: dict[str, str] = {
    "exact": "Exact URL (1)",
    "parent_url": "Parent URL (2-4)",
    "parent_domain": "Parent domain (5-6)",
    "linked_uni": "Linked Parent University Home page (8)",
    "vendor": "Vendor Home page (7)",
    "unlinked_uni": "Unlinked Parent University Home page (8)",
}
VERDICT_HEADER = "Reclaim Protocol Terms of use AI-Review"
REASON_HEADER = "AI Review"

NA = "n/a"


def _find_col(header: list, title: str, fallback: int | None = None) -> int | None:
    for i, h in enumerate(header):
        if str(h).strip().lower() == title.strip().lower():
            return i
    return fallback


def _tab_gid(sheets: SheetsClient, title: str) -> int:
    meta = sheets._service.spreadsheets().get(spreadsheetId=PORTAL_SHEET_ID).execute()
    for sh in meta["sheets"]:
        if sh["properties"]["title"] == title:
            return int(sh["properties"]["sheetId"])
    raise SystemExit(f"tab {title!r} not found")


def _insert_blank_rows(sheets: SheetsClient, gid: int, after_row_1based: int, count: int) -> None:
    """Insert `count` blank rows directly after `after_row_1based`."""
    sheets._service.spreadsheets().batchUpdate(
        spreadsheetId=PORTAL_SHEET_ID,
        body={"requests": [{
            "insertDimension": {
                "range": {"sheetId": gid, "dimension": "ROWS",
                          "startIndex": after_row_1based, "endIndex": after_row_1based + count},
                "inheritFromBefore": True,
            }
        }]},
    ).execute()


def _write_row_block(sheets: SheetsClient, qtab: str, row_1based: int, values: list[str]) -> None:
    """Write a contiguous A..N block for one row."""
    end = _col_letter(len(values))
    sheets._service.spreadsheets().values().update(
        spreadsheetId=PORTAL_SHEET_ID,
        range=f"{qtab}!A{row_1based}:{end}{row_1based}",
        valueInputOption="USER_ENTERED",
        body={"values": [values]},
    ).execute()


def _build_row_values(
    orgid: str, portal: str, level_idx_map: dict[str, int],
    verdict_idx: int, reason_idx: int, width: int,
    *, winning_level: str | None, tc_url: str, verdict: str, reason: str,
) -> list[str]:
    """One output row: levels before the win = n/a, the winning column = tc_url,
    levels after the win = '' (not searched). I = verdict, J = reason. Column H
    (unlinked_uni) is just the last level in the ladder."""
    row = [""] * width
    row[0] = orgid
    row[1] = portal
    order = list(LEVEL_HEADERS.keys())
    win_pos = order.index(winning_level) if winning_level in order else len(order)
    for pos, level in enumerate(order):
        idx = level_idx_map[level]
        if pos < win_pos:
            row[idx] = NA          # searched, nothing found
        elif pos == win_pos and winning_level is not None:
            row[idx] = tc_url      # the hit
        else:
            row[idx] = "" if winning_level is not None else NA  # after win / no win at all
    row[verdict_idx] = verdict
    row[reason_idx] = reason
    return row


@click.command()
@click.option("--tab", "tab_arg", default=DEFAULT_TAB, show_default=True)
@click.option("--start", type=int, default=None, help="First sheet row to process (1-based).")
@click.option("--end", type=int, default=None, help="Last sheet row (inclusive).")
@click.option("--orgid", "orgids", multiple=True, help="Only these OrgID(s). Comma-sep/repeatable.")
@click.option("--force", is_flag=True, help="Re-process rows that already have a col-I verdict.")
@click.option("--workers", type=int, default=6, show_default=True, help="Concurrent portals (each gets its own JS renderer).")
@click.option("--no-analysis", "no_analysis", is_flag=True, help="DISCOVERY ONLY: find + write the T&C URL(s) to the level columns; leave verdict/reason (I/J) blank for a later pass.")
@click.option("--row-budget", type=int, default=90, show_default=True, help="Per-row wall-clock cap (seconds); a slow host gives up and the remaining levels are skipped.")
@click.option("--dry-run", is_flag=True, help="Compute + print proposed rows; do NOT write.")
def main(tab_arg, start, end, orgids, force, workers, no_analysis, row_budget, dry_run):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = load_config()
    from agent.config import TC_FORCE_CLAUDE

    sheets = SheetsClient(
        sheet_id=PORTAL_SHEET_ID, universities_tab="x", portals_tab="x",
        credentials_path=config.google_credentials_path, token_path=config.google_token_path,
    )
    title = _resolve_tab_title(sheets, tab_arg)
    qtab = f"'{title}'"
    gid = _tab_gid(sheets, title)

    header = (sheets._get_values(qtab, f"{HEADER_ROW}:{HEADER_ROW}") or [[]])[0]
    orgid_idx = _find_col(header, ORGID_HEADER, 0)
    portal_idx = _find_col(header, PORTAL_HEADER, 1)
    level_idx_map = {lvl: _find_col(header, h) for lvl, h in LEVEL_HEADERS.items()}
    verdict_idx = _find_col(header, VERDICT_HEADER)
    reason_idx = _find_col(header, REASON_HEADER)
    missing = [name for name, idx in {
        **{f"level:{k}": v for k, v in level_idx_map.items()},
        "verdict": verdict_idx, "reason": reason_idx,
    }.items() if idx is None]
    if missing:
        raise SystemExit(f"could not locate columns by header: {missing}\nheader={header}")
    width = max(orgid_idx, portal_idx, *level_idx_map.values(), verdict_idx, reason_idx) + 1

    orgid_filter = {tok.strip() for e in orgids for tok in re.split(r"[,\s]+", str(e)) if tok.strip()}

    click.echo("=" * 70)
    click.echo(f"  Tab        : {title!r} (gid={gid})")
    click.echo(f"  Analyzer   : mode={config.tc_analyzer_mode} force_claude={TC_FORCE_CLAUDE}")
    click.echo(f"  Mode       : {'DRY RUN' if dry_run else 'WRITE in-place + insert rows'} force={force}")
    click.echo("=" * 70)

    data_first = HEADER_ROW + 1
    rows = sheets._get_values(qtab, f"{data_first}:100000")
    seeds: list[tuple[int, str, str]] = []
    for ridx, raw in enumerate(rows):
        sheet_row = data_first + ridx
        if start is not None and sheet_row < start:
            continue
        if end is not None and sheet_row > end:
            continue
        p = list(raw) + [""] * width
        orgid = str(p[orgid_idx]).strip()
        portal = str(p[portal_idx]).strip()
        existing_verdict = str(p[verdict_idx]).strip()
        # A row is "already done" if it has a verdict (analysis mode) OR any
        # level column C-H already filled (discovery mode leaves verdict blank).
        already = bool(existing_verdict) or any(str(p[i]).strip() for i in level_idx_map.values())
        if not orgid or not portal.startswith("http"):
            continue
        if orgid_filter and orgid not in orgid_filter:
            continue
        if already and not force:
            continue
        seeds.append((sheet_row, orgid, portal))

    click.echo(f"  Seeds to process: {len(seeds)}  (workers={workers})")

    # Each worker thread gets its OWN JS renderer (Playwright sync is thread-
    # bound; a shared renderer breaks, a per-thread one is fine). Analysis
    # results are de-duped in-memory across the run (shared vendor terms) so we
    # don't hit SQLite from threads.
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
            r = JSRenderer(timeout_seconds=config.js_rendering_timeout_seconds, user_agent=config.user_agent)
            tls.r = r
            with rlock:
                all_renderers.append(r)
        return r

    def _analyze(tc_url: str, orgid: str, jr) -> tuple[str, str]:
        key = tc_analyzer.normalize_tc_url(tc_url)
        with clock:
            if key in acache:
                return acache[key]
        try:
            result = tc_analyzer.analyze_tc_url(
                tc_url=tc_url, state=None, user_agent=config.user_agent,
                http_timeout=config.http_timeout_seconds, orgid=orgid,
                mode=config.tc_analyzer_mode, force_refresh=True, js_renderer=jr,
            )
        except Exception:
            logger.exception("analyze raised for %s", tc_url)
            result = {"verdict": "Maybe", "reasoning": "analyzer error"}
        verdict = str(result.get("verdict") or "Maybe")
        pair = (verdict, _reason_for(verdict, [result]))
        with clock:
            acache[key] = pair
        return pair

    def _compute(seed):
        sheet_row, orgid, portal = seed
        jr = _renderer()
        res = tc_levels.find_tc_levels(
            portal, orgid=orgid, university_name="", domains=[],
            js_renderer=jr, user_agent=config.user_agent,
            http_timeout=config.http_timeout_seconds,
            deadline=time.monotonic() + row_budget,
        )
        out_rows: list[list[str]] = []
        if not res.tc_urls:
            # Discovery-only: leave a no-T&C row blank (no verdict to add later).
            if no_analysis:
                reason = ""
            elif res.blocked:
                reason = ("Portal/site blocked (HTTP 403 / Cloudflare bot challenge) — "
                          "could not read the portal or its pages; manual review needed")
            elif res.spa_tc_hint:
                reason = ("T&C present only as a JS-rendered SPA modal (Privacy Policy / "
                          "Legal links are non-navigable '#' anchors) — no stable URL to "
                          "capture; open the portal and read it manually")
            else:
                reason = "No T&C found at any level (exact/parent/domain/vendor/university)"
            out_rows.append(_build_row_values(
                orgid, portal, level_idx_map, verdict_idx, reason_idx, width,
                winning_level=None, tc_url="", verdict=("" if no_analysis else "Maybe"), reason=reason))
        else:
            for tc_url in res.tc_urls:
                # DISCOVERY ONLY: write the T&C URL to its level column, leave
                # verdict/reason blank for a later analysis pass.
                verdict, reason = ("", "") if no_analysis else _analyze(tc_url, orgid, jr)
                out_rows.append(_build_row_values(
                    orgid, portal, level_idx_map, verdict_idx, reason_idx, width,
                    winning_level=res.winning_level, tc_url=tc_url, verdict=verdict, reason=reason))
        return {"row": sheet_row, "orgid": orgid, "portal": portal,
                "winning": res.winning_level, "uni": res.uni_homepage, "out_rows": out_rows}

    # Write the FIRST out-row of each result IN-PLACE to its seed row as soon
    # as it completes (safe: in-place cell updates don't shift rows, so this is
    # fine even in completion order). Multi-T&C extras need row INSERTION, which
    # shifts rows — those are deferred to a single bottom-to-top pass at the end.
    results: list[dict] = []
    counts: dict[str, int] = {}
    wlock = threading.Lock()

    def _tally(rv):
        v = rv[verdict_idx]
        counts[v] = counts.get(v, 0) + 1

    try:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
            futs = {ex.submit(_compute, s): s for s in seeds}
            for n, fut in enumerate(as_completed(futs), 1):
                seed = futs[fut]
                try:
                    r = fut.result()
                except Exception:
                    logger.exception("compute failed for seed %s", seed)
                    continue
                results.append(r)
                logger.info("[%d/%d] row %d %s -> win=%s rows=%d",
                            n, len(seeds), r["row"], r["orgid"], r["winning"], len(r["out_rows"]))
                if dry_run:
                    for i, rv in enumerate(r["out_rows"]):
                        tag = f"row {r['row']}" if i == 0 else f"+insert {r['row']}.{i}"
                        win_cell = rv[level_idx_map[r["winning"]]] if r["winning"] else "-"
                        click.echo(f"  DRY [{tag}] win={r['winning']} tc={win_cell!r} uni={r['uni'] or '-'!r}")
                    continue
                # incremental in-place write of the first row
                with wlock:
                    _write_row_block(sheets, qtab, r["row"], r["out_rows"][0])
                    _tally(r["out_rows"][0])
    finally:
        for rend in all_renderers:
            try:
                rend.close()
            except Exception:
                pass

    # End phase — insert + write the multi-T&C extra rows, bottom-to-top so
    # earlier (higher-row) insertions don't shift rows we haven't done yet.
    inserted = 0
    if not dry_run:
        for r in sorted((x for x in results if len(x["out_rows"]) > 1), key=lambda x: -x["row"]):
            extras = r["out_rows"][1:]
            _insert_blank_rows(sheets, gid, r["row"], len(extras))
            for j, rv in enumerate(extras, start=1):
                _write_row_block(sheets, qtab, r["row"] + j, rv)
                _tally(rv)
                inserted += 1
    else:
        for r in results:
            for rv in r["out_rows"][1:]:
                _tally(rv)

    click.echo("-" * 70)
    click.echo(f"Done. seeds_processed={len(results)} rows_inserted={inserted}")
    if counts:
        click.echo("Verdicts: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))


if __name__ == "__main__":
    main()

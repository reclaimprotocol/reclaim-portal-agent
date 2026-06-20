#!/usr/bin/env python3
"""Find student-login portals for the *dated* office tabs (e.g. "15June",
" 16June") and write them into column D ("Reclaim Protocol Login Page Url").

These dated tabs have a DIFFERENT layout from the per-state tabs that
run_portal_sheet.py targets:

    A: SheerID OrgID
    B: SheerID University Name      <- discovery name seed
    C: SheerID Website Domain       <- discovery domain seed
    D: Reclaim Protocol Login Page Url   <- OUTPUT (we write here)
    E: ReclaimProtocol Terms of Use URL
    F: Reclaim Protocol Terms of use AI-Review
    G: Notes

It runs the EXACT same trained discovery pipeline as everywhere else and is
idempotent: any row that already has something in column D is left untouched
(never deleted/overwritten) unless --force. State/city are parsed from a
trailing "(City, State)" in the university name when present, so affiliation
probes can still fire even though dated tabs carry no state column.

Reuses run_portal_sheet.py's hardwired PORTAL_SHEET_ID and helpers, so it can
only ever write to the office consolidation sheet.

Usage:
    python scripts/_run_dated_tab_portals.py --tab 16June --dry-run
    python scripts/_run_dated_tab_portals.py --tab 16June
    python scripts/_run_dated_tab_portals.py --tab 16June --start 4 --end 20
"""
from __future__ import annotations

import logging
import re
import sys
import time

import _bootstrap  # noqa: F401  -- must come before agent.* imports
import click

from agent.config import assert_openrouter_model_live, load_config
from agent.pipeline import PipelineContext
from agent.sheets_client import SheetsClient
from agent.stages import discovery
from agent.stages.js_renderer import JSRenderer
from agent.stages.sheet_writer import _portal_sort_key
from agent.state import StateStore
from run_portal_sheet import (  # reuse hardwired sheet + battle-tested helpers
    BAD_SEED_MARKER,
    DISCOVERY_DOMAIN_KEY,
    DISCOVERY_NAME_KEY,
    NO_PORTAL_MARKER,
    PORTAL_SHEET_ID,
    _extract_domain,
    _resolve_domain_from_name,
    _write_cell,
)

logger = logging.getLogger("run_dated_tab")

# Fixed layout for the dated office tabs (0-based column indices).
NAME_COL = 1      # B
SITE_COL = 2      # C
OUTPUT_COL = 3    # D  ("Reclaim Protocol Login Page Url")
OUTPUT_LETTER = "D"

# Trailing "(City, State)" or "(State)" in the SheerID name, e.g.
# "Vijaya College (Bengaluru Urban, Karnataka)".
_PAREN_RE = re.compile(r"\(([^()]+)\)\s*$")


def _parse_state_city(name: str) -> tuple[str, str]:
    """Pull (state, city) out of a trailing parenthetical in the name.
    Returns ('', '') when there's no parenthetical to parse."""
    m = _PAREN_RE.search(name or "")
    if not m:
        return "", ""
    parts = [p.strip() for p in m.group(1).split(",") if p.strip()]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""          # "(Karnataka)" -> state only
    return parts[-1], parts[0]        # "(City, State)" -> state, city


def _resolve_tab_title(sheets: SheetsClient, wanted: str) -> str:
    """Resolve a (possibly space-padded) tab title from a substring match,
    so the caller can pass --tab 16June even though the real title is
    ' 16June'. Returns the exact title or exits if not uniquely found."""
    meta = sheets._service.spreadsheets().get(spreadsheetId=PORTAL_SHEET_ID).execute()
    titles = [sh["properties"]["title"] for sh in meta["sheets"]]
    exact = [t for t in titles if t == wanted]
    if exact:
        return exact[0]
    matches = [t for t in titles if wanted.strip().lower() in t.strip().lower()]
    if len(matches) == 1:
        return matches[0]
    click.echo(f"ERROR: --tab {wanted!r} matched {matches or titles}", err=True)
    sys.exit(1)


@click.command()
@click.option("--tab", "tab_arg", required=True, help='Dated tab, e.g. "16June".')
@click.option("--start", type=int, default=None, help="First data row (1-based, excl. header).")
@click.option("--end", type=int, default=None, help="Last data row (inclusive).")
@click.option("--orgid", "orgids", multiple=True, help="Only process these OrgID(s) from column A. Comma-separated and/or repeatable, e.g. --orgid 664320,10256103.")
@click.option("--force", is_flag=True, help="Re-run rows that already have a REAL column-D portal (markers like '(no portal found)' are always retried).")
@click.option("--dry-run", is_flag=True, help="Discover and print, but do NOT write.")
def main(tab_arg: str, start: int | None, end: int | None, orgids: tuple[str, ...], force: bool, dry_run: bool) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = load_config()
    assert_openrouter_model_live()

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

    click.echo("=" * 70)
    click.echo(f"  Spreadsheet : office consolidation ({PORTAL_SHEET_ID})")
    click.echo(f"  Tab         : {title!r}")
    click.echo(f"  Columns     : name={header[NAME_COL]!r} website={header[SITE_COL]!r} "
               f"out={header[OUTPUT_COL]!r} (col {OUTPUT_LETTER})")
    click.echo(f"  Data rows   : {len(data_rows)}")
    if orgids:
        click.echo(f"  OrgID filter: {', '.join(orgids)}")
    click.echo(f"  Mode        : {'DRY RUN (no writes)' if dry_run else 'WRITE col D in-place'} force={force}")
    click.echo("=" * 70)

    # Accept comma-separated and/or repeated --orgid values.
    orgid_filter = {
        tok.strip()
        for entry in orgids
        for tok in re.split(r"[,\s]+", str(entry))
        if tok.strip()
    }

    js_renderer: JSRenderer | None = None
    if config.enable_js_rendering:
        js_renderer = JSRenderer(
            timeout_seconds=config.js_rendering_timeout_seconds,
            user_agent=config.user_agent,
        )

    processed = skipped = found = empty = 0
    try:
        with StateStore(config.state_db_path) as state:
            for ridx, raw in enumerate(data_rows):
                sheet_row = ridx + 2          # 1-based incl. header
                data_row_no = ridx + 1        # what --start/--end mean
                if start is not None and data_row_no < start:
                    continue
                if end is not None and data_row_no > end:
                    break

                padded = list(raw) + [""] * (OUTPUT_COL + 1 - len(raw))
                orgid_cell = str(padded[0]).strip()
                name = str(padded[NAME_COL]).strip()
                website = str(padded[SITE_COL]).strip()
                existing = str(padded[OUTPUT_COL]).strip()

                if not name:
                    continue
                if orgid_filter and orgid_cell not in orgid_filter:
                    continue
                # A REAL portal (an http URL) is never overwritten without
                # --force. The "(no portal found)"/"(invalid website)" markers
                # are treated as empty so a targeted re-run retries them.
                existing_is_real = bool(existing) and existing not in (NO_PORTAL_MARKER, BAD_SEED_MARKER)
                if existing_is_real and not force:
                    skipped += 1
                    logger.info("[row %d] %s — column D already has a portal, skipping", data_row_no, name)
                    continue

                domain = _extract_domain(website)
                if not domain:
                    state_name, city = _parse_state_city(name)
                    domain = _resolve_domain_from_name(name, city)
                    if not domain:
                        logger.warning("[row %d] %s — bad website %r and name→domain "
                                       "recovery failed; marking", data_row_no, name, website)
                        if not dry_run:
                            _write_cell(sheets, qtab, OUTPUT_LETTER, sheet_row, BAD_SEED_MARKER)
                        empty += 1
                        continue

                state_name, city = _parse_state_city(name)
                row = {
                    DISCOVERY_NAME_KEY: name,
                    DISCOVERY_DOMAIN_KEY: domain,
                    discovery.ORG_STATE_COL: state_name,
                    discovery.ORG_CITY_COL: city,
                }
                ctx = PipelineContext(
                    orgid=f"new:{domain}",
                    row=row,
                    deps={
                        "state": state,
                        "js_renderer": js_renderer,
                        "user_agent": config.user_agent,
                        "http_timeout": config.http_timeout_seconds,
                    },
                )

                logger.info("[row %d] %s (%s) → running discovery", data_row_no, name, domain)
                try:
                    result = discovery.run(ctx)
                except OSError as err:  # socket exhaustion mid-fan-out
                    logger.warning("[row %d] %s — OSError (%s); draining 15s and skipping",
                                   data_row_no, name, err)
                    time.sleep(15)
                    continue
                except Exception:
                    logger.exception("[row %d] %s — discovery raised; leaving cell untouched",
                                     data_row_no, name)
                    continue

                portals_sorted = sorted(result.get("portals", []) or [], key=_portal_sort_key)
                urls: list[str] = []
                for p in portals_sorted:
                    u = (p.get("url") or "").strip()
                    if u and u not in urls:
                        urls.append(u)

                processed += 1
                if urls:
                    found += 1
                    cell_value = "\n".join(urls)
                    logger.info("[row %d] %s → %d portal(s): %s", data_row_no, name, len(urls), urls[0])
                else:
                    empty += 1
                    cell_value = NO_PORTAL_MARKER
                    logger.info("[row %d] %s → no portal found", data_row_no, name)

                if dry_run:
                    click.echo(f"  DRY [{data_row_no}] {name}: {cell_value!r}")
                else:
                    _write_cell(sheets, qtab, OUTPUT_LETTER, sheet_row, cell_value)
    finally:
        if js_renderer is not None:
            js_renderer.close()

    click.echo("-" * 70)
    click.echo(f"Done. processed={processed} found={found} empty={empty} skipped={skipped}")


if __name__ == "__main__":
    main()

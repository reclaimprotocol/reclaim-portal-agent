#!/usr/bin/env python3
"""Find student-login portals for the consolidated *new-universities* Google
Sheet and write them back into each state tab IN-PLACE.

This is a SEPARATE entrypoint from run_single / run_batch / run_finetune_eval.
Those operate on SheerID's "Universities" sheet (GOOGLE_SHEET_ID from .env).
This script is HARDWIRED to the office consolidation sheet and will never
touch the SheerID sheet — so there is no way to confuse the two.

Layout it expects (one tab per state, owned by rohit@reclaimprotocol.org):

    | University Name | Website | ... | Portals URL |

It reads each row, seeds discovery from the Website column, runs the EXACT
same trained discovery pipeline we use everywhere else (rule-based +
Gemini-search + JS-render validation + known-platform short-circuits), then
writes the discovered student-login portal URL(s) into that row's
"Portals URL" cell. Idempotent: rows that already have a Portals URL are
skipped unless --force.

Auth reuses the existing personal OAuth token.json (the personal account has
been shared on the office sheet with read+write).

Usage:
    python scripts/run_portal_sheet.py --tab "Manipur"
    python scripts/run_portal_sheet.py --tab "Goa" --start 2 --end 20
    python scripts/run_portal_sheet.py --tab "Bihar" --force
    python scripts/run_portal_sheet.py --tab "Kerala" --dry-run
"""
from __future__ import annotations

import logging
import re
import sys
import time
from pathlib import Path
from typing import Any

import _bootstrap  # noqa: F401  -- must come before agent.* imports
import click
import requests
from google.auth.exceptions import TransportError as GoogleTransportError
from httplib2 import HttpLib2Error  # socket-exhaustion DNS failures land here

# All three surface from the SAME root cause — transient socket / ephemeral-
# port / DNS exhaustion during the burst of probe connections — but arrive as
# different exception classes depending on which network call tripped first:
#   OSError              — raw socket [Errno 49] can't-assign-address
#   HttpLib2Error        — httplib2 "Unable to find the server at sheets.…"
#   GoogleTransportError — google-auth token refresh "Unable to find … oauth2.…"
_TRANSIENT_WRITE_ERRORS = (OSError, HttpLib2Error, GoogleTransportError)

from agent.config import (
    OPENROUTER_API_KEY,
    OPENROUTER_MODEL,
    assert_openrouter_model_live,
    load_config,
)
from agent.pipeline import PipelineContext
from agent.sheets_client import SheetsClient, _col_letter
from agent.stages import discovery
from agent.stages.js_renderer import JSRenderer
from agent.stages.sheet_writer import _portal_sort_key
from agent.state import StateStore

logger = logging.getLogger("run_portal_sheet")

# Hardwired office consolidation sheet (one tab per state). NOT the SheerID
# Universities sheet. Do not parameterise this — the safety of this script is
# that it can only ever write to this spreadsheet.
PORTAL_SHEET_ID = "153Eg9KheygBagf_6-IG4CbiadvzBZ6r04QQhGVBWoSQ"

# Discovery reads these exact keys off the row dict (see discovery.run). We map
# the new sheet's human columns onto them so the trained pipeline runs
# unchanged.
DISCOVERY_NAME_KEY = "SheerID University Name"
DISCOVERY_DOMAIN_KEY = "SheerID Website Domain"

NO_PORTAL_MARKER = "(no portal found)"
BAD_SEED_MARKER = "(invalid website in sheet)"

# A seed that is a single bare label (no dot) — e.g. the placeholder text
# "Website" a few rows carry instead of a real URL — must NOT be fed to
# discovery: ".website" is a real TLD, so "website" would fabricate hosts
# like `exams.website` and match a stranger's login form. Require a dotted
# domain whose final label looks like a TLD.
_DOMAIN_RE = re.compile(r"^(?=.{4,253}$)([a-z0-9-]+\.)+[a-z]{2,}$")

# When a row has no OrgID (these don't), discovery's per-OrgID domain_overrides
# simply won't match — that's fine, the general discovery intelligence still
# applies. We key the synthetic orgid off the domain so logs are readable.


def _pick_col(header: list[str], exact: str, pattern: str, *, exclude: str = "") -> int | None:
    """Find a column index by exact header match, else regex, honouring an
    exclusion pattern so e.g. 'Website' doesn't grab 'Portals URL'."""
    for i, h in enumerate(header):
        if h.strip().lower() == exact.lower():
            return i
    rx = re.compile(pattern, re.I)
    exrx = re.compile(exclude, re.I) if exclude else None
    for i, h in enumerate(header):
        if exrx and exrx.search(h):
            continue
        if rx.search(h):
            return i
    return None


def _extract_domain(website: str) -> str:
    """Reduce a Website cell to a bare host discovery can parse. Discovery's
    parse_domains is liberal, but we strip scheme/path/www for a clean seed."""
    w = (website or "").strip()
    if not w:
        return ""
    w = re.sub(r"^[a-zA-Z]+://", "", w)
    w = w.split("/")[0].split("?")[0].strip().lower()
    if w.startswith("www."):
        w = w[4:]
    # Reject placeholders / non-domains ("Website", "N/A", "-", a bare
    # label). Only a real dotted domain is a safe discovery seed.
    if not _DOMAIN_RE.match(w):
        return ""
    return w


def _resolve_domain_from_name(name: str, city: str = "") -> str:
    """When the Website cell is junk/missing, recover the institution's
    official domain from its NAME via Gemini, so discovery can still run
    (name + acronym → Samarth/platform probes + name-based search). Returns
    "" on any failure or an implausible answer."""
    if not (OPENROUTER_API_KEY and name):
        return ""
    where = f", {city}," if city else ""
    prompt = (
        f"What is the official website domain of the university/college "
        f'"{name}"{where} in India? Return ONLY the bare hostname '
        f"(e.g. example.ac.in) — no scheme, no path, no explanation."
    )
    try:
        resp = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}"},
            json={"model": OPENROUTER_MODEL, "messages": [{"role": "user", "content": prompt}]},
            timeout=40,
        )
        text = resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as err:
        logger.warning("name→domain lookup failed for %r: %s", name, err)
        return ""
    # Pull the first domain-looking token out of whatever Gemini returns.
    for tok in re.split(r"[\s,]+", text):
        cand = _extract_domain(tok)
        if cand:
            return cand
    return ""


@click.command()
@click.option("--tab", "tab", required=True, help="State tab name, e.g. \"Manipur\".")
@click.option("--start", type=int, default=None, help="First data row (1-based, row 1 = first row under header). Default: all.")
@click.option("--end", type=int, default=None, help="Last data row (inclusive). Default: all.")
@click.option("--force", is_flag=True, help="Re-run rows that already have a Portals URL.")
@click.option("--dry-run", is_flag=True, help="Discover and print, but do NOT write to the sheet.")
def main(tab: str, start: int | None, end: int | None, force: bool, dry_run: bool) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config = load_config()
    assert_openrouter_model_live()

    # Build a SheetsClient pinned to the OFFICE sheet (not config.google_sheet_id).
    sheets = SheetsClient(
        sheet_id=PORTAL_SHEET_ID,
        universities_tab=tab,
        portals_tab=tab,  # unused here; we write cells directly
        credentials_path=config.google_credentials_path,
        token_path=config.google_token_path,
    )

    # --- read header + rows, locate columns -------------------------------
    header_rows = sheets._get_values(tab, "1:1")
    if not header_rows:
        click.echo(f"ERROR: tab {tab!r} has no header row (is the tab name exact?).", err=True)
        sys.exit(1)
    header = [str(h) for h in header_rows[0]]

    name_i = _pick_col(header, "University Name", r"name|college|institution|universit", exclude=r"portal|website|url|domain")
    site_i = _pick_col(header, "Website", r"website|domain", exclude=r"portal")
    city_i = _pick_col(header, "City", r"city|district|location|town", exclude=r"portal")
    portal_i = _pick_col(header, "Portals URL", r"portal")

    if name_i is None or site_i is None:
        click.echo(
            "ERROR: could not locate required input columns.\n"
            f"  header      = {header}\n"
            f"  name col    = {header[name_i] if name_i is not None else 'NOT FOUND'}\n"
            f"  website col = {header[site_i] if site_i is not None else 'NOT FOUND'}",
            err=True,
        )
        sys.exit(1)

    # The output column may not exist yet on every tab (the user added it to
    # some). Create it at the end of the header so every tab can be processed.
    if portal_i is None:
        portal_i = len(header)
        new_header_cell = _col_letter(portal_i + 1)
        logger.info("tab %r has no 'Portals URL' column — creating it at %s1", tab, new_header_cell)
        if not dry_run:
            _write_cell(sheets, tab, new_header_cell, 1, "Portals URL")
        header.append("Portals URL")

    data_rows = sheets._get_values(tab, "2:100000")
    portal_col_letter = _col_letter(portal_i + 1)

    # Sheet identity banner so the user always knows exactly what is being hit.
    meta = sheets._service.spreadsheets().get(spreadsheetId=PORTAL_SHEET_ID).execute()
    sheet_title = meta.get("properties", {}).get("title", "?")
    click.echo("=" * 70)
    click.echo(f"  Spreadsheet : {sheet_title}")
    click.echo(f"  Sheet ID    : {PORTAL_SHEET_ID}")
    click.echo(f"  Tab         : {tab}")
    click.echo(f"  Columns     : name={header[name_i]!r}  website={header[site_i]!r}  out={header[portal_i]!r} (col {portal_col_letter})")
    click.echo(f"  Data rows   : {len(data_rows)}")
    click.echo(f"  Mode        : {'DRY RUN (no writes)' if dry_run else 'WRITE in-place'}  force={force}")
    click.echo("=" * 70)

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
                data_row_no = ridx + 1        # 1-based excl. header (what --start/--end mean)
                if start is not None and data_row_no < start:
                    continue
                if end is not None and data_row_no > end:
                    break

                padded = list(raw) + [""] * (len(header) - len(raw))
                name = str(padded[name_i]).strip()
                website = str(padded[site_i]).strip()
                existing = str(padded[portal_i]).strip()

                if not name:
                    continue
                if existing and not force:
                    skipped += 1
                    logger.info("[row %d] %s — already has Portals URL, skipping", data_row_no, name)
                    continue

                domain = _extract_domain(website)
                if not domain:
                    # Website cell is junk/missing — recover the domain from
                    # the NAME via Gemini so we can still discover (name +
                    # acronym → Samarth/platform probes + name-based search).
                    city = str(padded[city_i]).strip() if city_i is not None else ""
                    domain = _resolve_domain_from_name(name, city)
                    if domain:
                        logger.info(
                            "[row %d] %s — Website cell %r invalid; recovered "
                            "domain %r from name", data_row_no, name, website, domain,
                        )
                    else:
                        logger.warning(
                            "[row %d] %s — Website cell %r invalid and name→domain "
                            "recovery failed; marking", data_row_no, name, website,
                        )
                        if not dry_run:
                            _write_cell(sheets, tab, portal_col_letter, sheet_row, BAD_SEED_MARKER)
                        empty += 1
                        continue

                orgid = f"new:{domain}"
                # The tab name IS the state — inject it so discovery's
                # affiliation probe / fallback / always-attach can fire
                # (office-sheet rows have no per-OrgID state override).
                row_city = str(padded[city_i]).strip() if city_i is not None else ""
                row = {
                    DISCOVERY_NAME_KEY: name,
                    DISCOVERY_DOMAIN_KEY: domain,
                    discovery.ORG_STATE_COL: tab,
                    discovery.ORG_CITY_COL: row_city,
                }
                ctx = PipelineContext(
                    orgid=orgid,
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
                    logger.warning(
                        "[row %d] %s — discovery hit OSError (%s); draining 15s "
                        "and skipping row", data_row_no, name, err,
                    )
                    time.sleep(15)
                    continue
                except Exception:  # one bad row must not kill the run
                    logger.exception("[row %d] %s — discovery raised; leaving cell untouched", data_row_no, name)
                    continue

                portals = result.get("portals", []) or []
                portals_sorted = sorted(portals, key=_portal_sort_key)
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
                    _write_cell(sheets, tab, portal_col_letter, sheet_row, cell_value)
    finally:
        if js_renderer is not None:
            js_renderer.close()

    click.echo("-" * 70)
    click.echo(f"Done. processed={processed} found={found} empty={empty} skipped={skipped}")


def _write_cell(sheets: SheetsClient, tab: str, col_letter: str, row: int, value: str) -> bool:
    """Write one cell, retrying through transient socket exhaustion.

    The per-row discovery fan-out opens many short-lived sockets; under a
    burst the ephemeral port range is momentarily exhausted and the very
    next outbound connection (this Sheets write) fails with OSError
    [Errno 49] "Can't assign requested address". That is transient — the
    TIME_WAIT sockets drain within seconds — so we back off and retry
    rather than let it abort the whole batch. Returns True on success.
    """
    a1 = f"{tab}!{col_letter}{row}"
    last_err: Exception | None = None
    for attempt in range(5):
        try:
            sheets._execute_with_retry(
                lambda: sheets._service.spreadsheets().values().update(
                    spreadsheetId=sheets.sheet_id,
                    range=a1,
                    valueInputOption="RAW",
                    body={"values": [[value]]},
                ).execute(),
                label=f"write {a1}",
            )
            return True
        except _TRANSIENT_WRITE_ERRORS as err:
            last_err = err
            wait = 8 * (attempt + 1)  # 8,16,24,32s — let TIME_WAIT drain
            logger.warning(
                "write %s failed (%s); sockets/DNS likely exhausted — "
                "draining %ds then retry (%d/5)", a1, err, wait, attempt + 1,
            )
            time.sleep(wait)
        except Exception as err:  # never let an unexpected write error kill the batch
            logger.error("write %s failed with unexpected error: %s — row left blank", a1, err)
            return False
    logger.error("write %s permanently failed after retries: %s", a1, last_err)
    return False


if __name__ == "__main__":
    main()

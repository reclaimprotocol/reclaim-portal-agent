#!/usr/bin/env python3
"""For the 'Tnc retrieval' tab: fetch the content of each T&C URL (col B) and
write the extracted text into col C; if it can't be fetched, write the error
description into col D.

Reuses tc_analyzer's HTML/PDF extractors and the shared HTTP session, plus the
JS-render fallback for SPA pages. Cells are capped to fit Google's 50k limit.

Usage:
  python scripts/_fetch_tnc_content.py --end 11           # first 10 data rows
  python scripts/_fetch_tnc_content.py --start 2 --end 11 --dry-run
"""
from __future__ import annotations

import http.client
import time

import _bootstrap  # noqa: F401
import click
import requests

from agent.config import load_config
from agent.sheets_client import SheetsClient
from agent.stages import discovery_rules
from agent.stages import tc_analyzer
from agent.stages.tc_analyzer import _extract_html_text, _extract_pdf_text, _MIN_TEXT_LEN
from agent.stages.js_renderer import JSRenderer
from _run_dated_tab_portals import _resolve_tab_title
from run_portal_sheet import PORTAL_SHEET_ID

TAB = "Tnc retrieval"
URL_COL = 1       # B
CONTENT_COL = "C"
ERROR_COL = "D"
CELL_CAP = 49000  # Google Sheets per-cell hard limit is 50000 chars
LIGHT_RED = {"red": 0.96, "green": 0.80, "blue": 0.80}
COLOR_END_COL = 6  # color A..F


def _execute_retry(request, *, tries: int = 6):
    """Retry a Sheets API call on transient 429/5xx and transport errors
    (read timeout / connection reset) — a long run must not die on one flaky
    write."""
    from googleapiclient.errors import HttpError
    delay = 2.0
    for attempt in range(1, tries + 1):
        try:
            return request.execute()
        except HttpError as e:
            if int(getattr(e.resp, "status", 0) or 0) in (429, 500, 502, 503, 504) and attempt < tries:
                time.sleep(delay); delay = min(delay * 2, 30); continue
            raise
        except (OSError, http.client.HTTPException, TimeoutError):
            if attempt < tries:
                time.sleep(delay); delay = min(delay * 2, 30); continue
            raise

# SUBSTANTIVE legal-prose markers — phrases that appear in the *body* of a real
# Terms/Privacy/Disclaimer document, NOT in site chrome. Deliberately EXCLUDES
# footer boilerplate like "copyright" / "all rights reserved" / bare "cookies",
# which appear site-wide and falsely inflated a nav/footer stub (e.g.
# mgmuhs.com/copyrights) into looking like a legal page.
_STRONG_MARKERS = (
    "terms and conditions", "terms of use", "terms of service", "terms & conditions",
    "privacy policy", "privacy statement", "privacy notice", "acceptable use",
    "intellectual property", "personal information", "personal data", "we collect",
    "we may collect", "information we collect", "you agree", "your consent",
    "you consent", "by accessing", "by using this", "limitation of liability",
    "shall not be liable", "no liability", "no responsibility", "accepts no responsibility",
    "no warranties", "no warranty", "warranties of any kind", "no representations",
    "at your own risk", "in no event", "we make no", "governing law", "jurisdiction",
    "data protection", "user agreement", "conditions of use", "cookie policy",
    "your rights", "data retention", "unauthorized", "prohibited", "indemnif",
    "hyperlinking policy", "we endeavour", "reliance you place",
)
# URL-path tokens that signal the page IS meant to be a T&C/legal page.
_TC_URL_TOKENS = (
    "term", "privacy", "disclaimer", "condition", "policy", "policies",
    "copyright", "legal", "tnc", "t-and-c", "/tos", "dataprivacy", "refund",
    "acceptable-use", "website-polic",
)


def _tc_relevance(text: str, url: str) -> tuple[bool, int]:
    """Keep only pages with substantive legal prose. URL pointing at a legal
    page needs >=1 substantive marker; a page reached at a non-obvious URL needs
    >=3 (so a real T&C body still counts but homepage/nav/footer stubs don't)."""
    low = text.lower()
    strong = sum(1 for m in _STRONG_MARKERS if m in low)
    url_is_tc = any(tok in url.lower() for tok in _TC_URL_TOKENS)
    if url_is_tc and strong >= 1:
        return True, strong
    if strong >= 3:
        return True, strong
    return False, strong


def fetch(url: str, ua: str, timeout: int, jr) -> tuple[str, str]:
    """Return (content_text, error_description). One of them is empty."""
    resp = None
    try:
        resp = discovery_rules.HTTP_SESSION.get(
            url, headers={"User-Agent": ua}, timeout=timeout, allow_redirects=True)
    except requests.RequestException as err:
        resp = None
        http_err = f"{type(err).__name__}: {str(err)[:200]}"
    else:
        http_err = ""

    static_text, is_pdf, status = "", False, None
    if resp is not None:
        status = resp.status_code
        if 200 <= status < 400:
            ctype = (resp.headers.get("content-type") or "").lower()
            is_pdf = "pdf" in ctype or url.lower().split("?")[0].endswith(".pdf")
            try:
                static_text = _extract_pdf_text(resp.content) if is_pdf else _extract_html_text(resp.text or "")
            except Exception as err:
                http_err = f"extract failed: {type(err).__name__}: {str(err)[:150]}"
        else:
            http_err = f"HTTP {status}"

    # JS-render fallback for short/empty non-PDF bodies. Use render_poll (early-
    # capture polling) so SPAs that render-then-redirect (auth guards) or render
    # late are captured at their richest DOM, not a single mistimed snapshot.
    js_err = ""
    if not is_pdf and jr is not None and len(static_text.strip()) < _MIN_TEXT_LEN:
        try:
            rendered = jr.render_poll(url) if hasattr(jr, "render_poll") else jr.render(url)
            if rendered is not None and rendered.ok and rendered.html:
                rtext = _extract_html_text(rendered.html)
                if len(rtext.strip()) > len(static_text.strip()):
                    static_text = rtext
            elif rendered is not None and not rendered.ok:
                js_err = f"JS render failed: {rendered.error[:150]}"
        except Exception as err:
            js_err = f"JS render raised: {type(err).__name__}: {str(err)[:120]}"

    text = static_text.strip()
    if len(text) >= _MIN_TEXT_LEN or (text and is_pdf):
        # Relevance gate: must read like an actual T&C/privacy/legal doc, not a
        # homepage / nav / directory index.
        relevant, hits = _tc_relevance(text, url)
        if relevant:
            return text[:CELL_CAP], ""
        return "", (f"fetched {len(text)} chars but content is NOT T&C-related "
                    f"(only {hits} legal markers) — looks like a homepage/index/landing page, "
                    f"not the actual terms/privacy text")
    # nothing usable -> build an error description
    if http_err:
        return "", http_err
    if status is not None and 200 <= status < 400:
        base = f"HTTP {status} but body empty/too short ({len(text)} chars after extraction)"
        return "", (base + (" | " + js_err if js_err else " — likely JS-only/blocked content"))
    return "", (js_err or "unknown fetch failure")


@click.command()
@click.option("--start", type=int, default=2, show_default=True)
@click.option("--end", type=int, default=11, show_default=True, help="Last data row inclusive (default 11 = first 10).")
@click.option("--skip-filled", is_flag=True, help="Skip rows that already have content (C) or an error (D) — safe resume, never clobbers a good result.")
@click.option("--dry-run", is_flag=True)
def main(start, end, skip_filled, dry_run):
    config = load_config()
    sheets = SheetsClient(sheet_id=PORTAL_SHEET_ID, universities_tab="x", portals_tab="x",
                          credentials_path=config.google_credentials_path, token_path=config.google_token_path)
    title = _resolve_tab_title(sheets, TAB)
    q = f"'{title}'"
    rows = sheets._get_values(q, f"{start}:{end}")

    jr = None
    if config.enable_js_rendering:
        jr = JSRenderer(timeout_seconds=config.js_rendering_timeout_seconds, user_agent=config.user_agent)

    ok = errs = 0
    error_rows: list[int] = []
    try:
        for i, r in enumerate(rows):
            row = start + i
            url = str(r[URL_COL]).strip() if len(r) > URL_COL else ""
            if not url:
                continue
            if skip_filled and ((len(r) > 2 and str(r[2]).strip()) or (len(r) > 3 and str(r[3]).strip())):
                continue
            content, error = fetch(url, config.user_agent, config.http_timeout_seconds, jr)
            status = f"OK {len(content)} chars" if content else f"ERR {error[:70]}"
            click.echo(f"  row {row}: {url[:55]:55} -> {status}")
            if content:
                ok += 1
            else:
                errs += 1
                error_rows.append(row)
            if not dry_run:
                _execute_retry(sheets._service.spreadsheets().values().update(
                    spreadsheetId=PORTAL_SHEET_ID, range=f"{q}!{CONTENT_COL}{row}:{ERROR_COL}{row}",
                    valueInputOption="RAW", body={"values": [[content, error]]}))
                time.sleep(0.2)
    finally:
        if jr is not None:
            jr.close()

    # Color ALL error rows in the processed range light red (A..F). Re-read the
    # range so we catch error rows from earlier (e.g. crashed) runs too, not
    # just the ones touched this run.
    colored = 0
    if not dry_run:
        meta = sheets._service.spreadsheets().get(spreadsheetId=PORTAL_SHEET_ID).execute()
        gid = next(sh["properties"]["sheetId"] for sh in meta["sheets"]
                   if sh["properties"]["title"] == title)
        cur = sheets._get_values(q, f"{start}:{end}")
        err_now = [start + i for i, r in enumerate(cur)
                   if (len(r) > 3 and str(r[3]).strip()) and not (len(r) > 2 and str(r[2]).strip())]
        ranges = []
        for rw in sorted(err_now):
            if ranges and rw == ranges[-1][1] + 1:
                ranges[-1] = (ranges[-1][0], rw)
            else:
                ranges.append((rw, rw))
        if ranges:
            reqs = [{"repeatCell": {
                "range": {"sheetId": gid, "startRowIndex": a - 1, "endRowIndex": b,
                          "startColumnIndex": 0, "endColumnIndex": COLOR_END_COL},
                "cell": {"userEnteredFormat": {"backgroundColor": LIGHT_RED}},
                "fields": "userEnteredFormat.backgroundColor"}} for a, b in ranges]
            _execute_retry(sheets._service.spreadsheets().batchUpdate(
                spreadsheetId=PORTAL_SHEET_ID, body={"requests": reqs}))
        colored = len(err_now)

    click.echo("-" * 60)
    click.echo(f"Done. content_fetched={ok} errors={errs} error_rows_colored={colored}")


if __name__ == "__main__":
    main()

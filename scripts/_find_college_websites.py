#!/usr/bin/env python3
"""Find official website URLs for the colleges in the NU_Bangladesh tab and
write them into column E.

Tab NU_Bangladesh: A=College Code, B=College Name, C=District, D=Type, E=Website.
Data starts row 2. For each college we ask Gemini (via OpenRouter) for the
official website, constrained to BANGLADESH, reject any Indian URL, and
(unless --no-verify) confirm the URL is live before writing.

Idempotent: a row with non-empty column E is skipped unless --force.

Usage:
  python scripts/_find_college_websites.py --limit 8 --dry-run   # smoke test
  python scripts/_find_college_websites.py --workers 4
"""
from __future__ import annotations

import http.client
import re
import threading
import time
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed

import _bootstrap  # noqa: F401
import click
import requests

from agent.config import load_config, OPENROUTER_API_KEY, OPENROUTER_MODEL
from agent.sheets_client import SheetsClient
from _discover_bangladesh import _is_foreign  # Bangladesh geo filter (rejects .in / Indian vendors)

SHEET_ID = "1_V2-9XVua309pXmVUTpw-k5uPDT1bU2cTlRGk5uWzSs"
TAB = "NU_Bangladesh"
CODE_COL, NAME_COL, DIST_COL, OUT_COL = 0, 1, 2, 4  # A, B, C, E
OUT_LETTER = "E"
ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"

_URL_RE = re.compile(r"https?://[^\s'\"<>)]+", re.I)


def _ask_gemini(name: str, district: str, api_key: str, model: str, timeout: float) -> str:
    prompt = (
        f"What is the official website homepage URL of the college named "
        f"\"{name}\" located in {district} district, BANGLADESH (a college "
        f"affiliated with National University, Bangladesh)?\n"
        f"Rules:\n"
        f"- It MUST be a Bangladeshi institution's own website (typically a .edu.bd, "
        f".ac.bd, .com or .org domain). NEVER return an Indian (.in) website or any "
        f"site for a similarly-named Indian/other-country college.\n"
        f"- Return ONLY the bare homepage URL (https://...). No text.\n"
        f"- If the college has no official website, or you are not confident, "
        f"return exactly: NONE"
    )
    r = requests.post(
        ENDPOINT,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
                 "HTTP-Referer": "https://github.com/reclaimprotocol"},
        json={"model": model, "messages": [{"role": "user", "content": prompt}]},
        timeout=timeout,
    )
    data = r.json()
    if "error" in data:
        raise RuntimeError(str(data["error"])[:120])
    return str(data["choices"][0]["message"]["content"] or "").strip()


def _extract_url(text: str) -> str:
    if not text or text.strip().upper().startswith("NONE"):
        return ""
    m = _URL_RE.search(text)
    return m.group(0).rstrip(".,);") if m else ""


def _is_live(url: str, ua: str, timeout: float) -> bool:
    try:
        resp = requests.get(url, headers={"User-Agent": ua}, timeout=timeout,
                            allow_redirects=True, stream=True)
        resp.close()
        return resp.status_code < 400
    except Exception:
        return False


def _execute_retry(request, *, tries: int = 6):
    from googleapiclient.errors import HttpError
    delay = 2.0
    for attempt in range(1, tries + 1):
        try:
            return request.execute()
        except HttpError as e:
            if int(getattr(e.resp, "status", 0) or 0) in (429, 500, 502, 503, 504) and attempt < tries:
                time.sleep(delay); delay = min(delay * 2, 30); continue
            raise
        except (OSError, http.client.HTTPException):
            if attempt < tries:
                time.sleep(delay); delay = min(delay * 2, 30); continue
            raise


@click.command()
@click.option("--start", type=int, default=2, show_default=True)
@click.option("--end", type=int, default=None)
@click.option("--limit", type=int, default=None, help="Process at most N rows (smoke test).")
@click.option("--workers", type=int, default=4, show_default=True)
@click.option("--no-verify", is_flag=True, help="Skip the HTTP liveness check (faster, more hallucinations).")
@click.option("--force", is_flag=True, help="Re-lookup rows that already have column E.")
@click.option("--dry-run", is_flag=True, help="Look up + print; do NOT write.")
def main(start, end, limit, workers, no_verify, force, dry_run):
    config = load_config()
    api_key = OPENROUTER_API_KEY
    model = OPENROUTER_MODEL
    if not api_key:
        raise SystemExit("OPENROUTER_API_KEY missing")
    sheets = SheetsClient(sheet_id=SHEET_ID, universities_tab="x", portals_tab="x",
                          credentials_path=config.google_credentials_path, token_path=config.google_token_path)
    q = f"'{TAB}'"
    # ensure header E1
    if not dry_run:
        _execute_retry(sheets._service.spreadsheets().values().update(
            spreadsheetId=SHEET_ID, range=f"{q}!E1", valueInputOption="USER_ENTERED",
            body={"values": [["Website"]]}))

    rows = sheets._get_values(q, "2:100000")
    seeds = []
    for ridx, r in enumerate(rows):
        sheet_row = ridx + 2
        if sheet_row < start:
            continue
        if end is not None and sheet_row > end:
            break
        name = str(r[NAME_COL]).strip() if len(r) > NAME_COL else ""
        dist = str(r[DIST_COL]).strip() if len(r) > DIST_COL else ""
        existing = str(r[OUT_COL]).strip() if len(r) > OUT_COL else ""
        if not name:
            continue
        if existing and not force:
            continue
        seeds.append((sheet_row, name, dist))
    if limit is not None:
        seeds = seeds[:limit]

    click.echo("=" * 70)
    click.echo(f"  Tab {TAB}: seeds={len(seeds)} workers={workers} verify={not no_verify} "
               f"{'DRY' if dry_run else 'WRITE E'} model={model}")
    click.echo("=" * 70)

    wlock = threading.Lock()

    def _one(seed):
        sheet_row, name, dist = seed
        try:
            ans = _ask_gemini(name, dist, api_key, model, config.http_timeout_seconds * 3 or 30)
        except Exception as e:
            return {"row": sheet_row, "name": name, "url": "", "note": f"llm-err:{e}"}
        url = _extract_url(ans)
        note = ""
        if url and _is_foreign(url):
            note = f"rejected-foreign:{url}"; url = ""
        if url and not no_verify and not _is_live(url, config.user_agent, config.http_timeout_seconds):
            note = f"dead:{url}"; url = ""
        return {"row": sheet_row, "name": name, "url": url, "note": note}

    found = done = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futs = {ex.submit(_one, s): s for s in seeds}
        for fut in as_completed(futs):
            r = fut.result(); done += 1
            if r["url"]:
                found += 1
            tag = f"[{done}/{len(seeds)}] row {r['row']} {r['name'][:34]:34}"
            click.echo(f"  {tag} -> {r['url'] or '(none)'}{'  '+r['note'] if r['note'] else ''}")
            if not dry_run and r["url"]:
                with wlock:
                    _execute_retry(sheets._service.spreadsheets().values().update(
                        spreadsheetId=SHEET_ID, range=f"{q}!{OUT_LETTER}{r['row']}",
                        valueInputOption="USER_ENTERED", body={"values": [[r["url"]]]}))
    click.echo("-" * 70)
    click.echo(f"Done. processed={done} websites_found={found}")


if __name__ == "__main__":
    main()

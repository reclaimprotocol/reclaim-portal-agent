#!/usr/bin/env python3
"""Discover student login portals for the universities in the standalone
'Bangladesh' spreadsheet and write them into column C ("Login portals").

Sheet (id below): A=University Name, B=Website, C=Login portals (output).
Data starts row 2. For each row we run the trained discovery pipeline
(agent.stages.discovery.run) on (name, domain-from-website) and write ALL
discovered portal URLs, newline-separated, into column C.

Idempotent: a row with a non-empty column C is skipped unless --force.

Usage:
  python scripts/_discover_bangladesh.py --limit 2          # smoke test
  python scripts/_discover_bangladesh.py --start 2 --end 50
  python scripts/_discover_bangladesh.py --workers 4
"""
from __future__ import annotations

import http.client
import threading
import time
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed

import _bootstrap  # noqa: F401
import click

from agent.config import load_config
from agent.pipeline import PipelineContext
from agent.sheets_client import SheetsClient
from agent.stages import discovery
from agent.stages.js_renderer import JSRenderer
from agent.state import StateStore

SHEET_ID = "1_V2-9XVua309pXmVUTpw-k5uPDT1bU2cTlRGk5uWzSs"
TAB = "Bangladesh"
NAME_COL, SITE_COL, OUT_COL = 0, 1, 2  # A, B, C
OUT_LETTER = "C"


# These colleges are all Bangladesh institutions. The discovery pipeline was
# trained on Indian universities, so it can surface Indian portals/vendors
# (e.g. India's JNU at jnu.samarth.edu.in matching Bangladesh's "Jagannath
# University"). Reject any URL on an Indian TLD or a known Indian SaaS vendor.
_INDIAN_VENDOR_SUBSTR = (
    "samarth.edu.in", "ucanapply", "digitaluniversity.ac", "iitms",
    "mastersofterp", "icloudems", "knimbus", "vidyamantra", "ivyeduerp",
    "nopaperforms", "eduserveonline", "academia.edu.in",
    # Indian SaaS vendors on .com/.org that .in-TLD check misses (seen in logs)
    "sumsraj", "digiicampus", "core-campus", "mponline", "samvidha",
    "peoplesoft", "vidyamitra", "ekalsoft",
)


def _is_foreign(url: str) -> bool:
    """True if the URL looks Indian (so it must NOT be added for a BD college)."""
    low = url.lower()
    host = urlparse(low if "://" in low else "http://" + low).netloc
    if host.endswith(".in"):
        return True
    return any(v in low for v in _INDIAN_VENDOR_SUBSTR)


def _domain(website: str) -> str:
    """Bare host (strip scheme, path, leading www.) from a website URL."""
    w = str(website or "").strip()
    if not w:
        return ""
    if "://" not in w:
        w = "http://" + w
    host = urlparse(w).netloc.strip().lower()
    return host[4:] if host.startswith("www.") else host


def _execute_retry(request, *, what: str, tries: int = 6):
    from googleapiclient.errors import HttpError
    delay = 2.0
    for attempt in range(1, tries + 1):
        try:
            return request.execute()
        except HttpError as e:
            status = int(getattr(e.resp, "status", 0) or 0)
            if status in (429, 500, 502, 503, 504) and attempt < tries:
                time.sleep(delay); delay = min(delay * 2, 30); continue
            raise
        except (OSError, http.client.HTTPException):
            if attempt < tries:
                time.sleep(delay); delay = min(delay * 2, 30); continue
            raise


@click.command()
@click.option("--start", type=int, default=2, show_default=True, help="First sheet row (1-based).")
@click.option("--end", type=int, default=None, help="Last sheet row (inclusive).")
@click.option("--limit", type=int, default=None, help="Process at most N rows (smoke test).")
@click.option("--workers", type=int, default=3, show_default=True, help="Concurrent universities.")
@click.option("--force", is_flag=True, help="Re-discover rows that already have column C filled.")
@click.option("--dry-run", is_flag=True, help="Discover + print; do NOT write.")
def main(start, end, limit, workers, force, dry_run):
    config = load_config()
    sheets = SheetsClient(
        sheet_id=SHEET_ID, universities_tab="x", portals_tab="x",
        credentials_path=config.google_credentials_path, token_path=config.google_token_path,
    )
    q = f"'{TAB}'"
    rows = sheets._get_values(q, "2:100000")

    seeds = []
    for ridx, r in enumerate(rows):
        sheet_row = ridx + 2
        if sheet_row < start:
            continue
        if end is not None and sheet_row > end:
            break
        name = str(r[NAME_COL]).strip() if len(r) > NAME_COL else ""
        site = str(r[SITE_COL]).strip() if len(r) > SITE_COL else ""
        existing = str(r[OUT_COL]).strip() if len(r) > OUT_COL else ""
        if not name:
            continue
        if existing and not force:
            continue
        seeds.append((sheet_row, name, _domain(site) or site))
    if limit is not None:
        seeds = seeds[:limit]

    click.echo("=" * 70)
    click.echo(f"  Sheet : {TAB} ({SHEET_ID})")
    click.echo(f"  Seeds : {len(seeds)}  workers={workers}  "
               f"{'DRY RUN' if dry_run else 'WRITE col C'} force={force}")
    click.echo("=" * 70)

    tls = threading.local()
    all_renderers, all_states = [], []
    rlock = threading.Lock()
    wlock = threading.Lock()

    def _deps():
        jr = getattr(tls, "jr", None)
        if jr is None and config.enable_js_rendering:
            jr = JSRenderer(timeout_seconds=config.js_rendering_timeout_seconds, user_agent=config.user_agent)
            tls.jr = jr
            with rlock:
                all_renderers.append(jr)
        st = getattr(tls, "st", None)
        if st is None:
            st = StateStore(":memory:")
            tls.st = st
            with rlock:
                all_states.append(st)
        return {"state": st, "js_renderer": getattr(tls, "jr", None),
                "user_agent": config.user_agent, "http_timeout": config.http_timeout_seconds}

    def _one(seed):
        sheet_row, name, domain = seed
        ctx = PipelineContext(orgid=f"bd:{domain or sheet_row}",
                              row={"SheerID University Name": name, "SheerID Website Domain": domain},
                              deps=_deps())
        try:
            res = discovery.run(ctx)
        except Exception as e:
            return {"row": sheet_row, "name": name, "urls": [], "err": str(e)}
        urls, seen = [], set()
        for p in res.get("portals", []):
            u = str(p.get("url") or "").strip()
            if u and u not in seen and not _is_foreign(u):  # drop Indian portals
                seen.add(u); urls.append(u)
        return {"row": sheet_row, "name": name, "urls": urls,
                "timeout": res.get("completed_with_timeout", False), "err": None}

    done = 0
    try:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
            futs = {ex.submit(_one, s): s for s in seeds}
            for fut in as_completed(futs):
                r = fut.result()
                done += 1
                tag = f"[{done}/{len(seeds)}] row {r['row']} {r['name'][:38]}"
                if r["err"]:
                    click.echo(f"  {tag}: ERROR {r['err'][:60]}")
                    continue
                cell = "\n".join(r["urls"])
                click.echo(f"  {tag}: {len(r['urls'])} portal(s)"
                           + ("  [timeout]" if r.get("timeout") else ""))
                for u in r["urls"]:
                    click.echo(f"        {u}")
                if not dry_run and r["urls"]:
                    with wlock:
                        _execute_retry(
                            sheets._service.spreadsheets().values().update(
                                spreadsheetId=SHEET_ID, range=f"{q}!{OUT_LETTER}{r['row']}",
                                valueInputOption="USER_ENTERED", body={"values": [[cell]]}),
                            what=f"write C{r['row']}")
    finally:
        for jr in all_renderers:
            try: jr.close()
            except Exception: pass
        for st in all_states:
            try: st.close()
            except Exception: pass

    click.echo("-" * 70)
    click.echo(f"Done. processed={done}")


if __name__ == "__main__":
    main()

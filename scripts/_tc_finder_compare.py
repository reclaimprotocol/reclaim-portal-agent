#!/usr/bin/env python3
"""Audit harness: run the Stage C.1 T&C *finder* against every 15June row and
compare what it discovers to the human-entered column-E URL(s) and verdict.

READ-ONLY on the sheet. Emits a JSON report (/tmp/tc_finder_compare.json) we
mine for analyzer/finder improvements:
  * rows where the finder discovered a readable T&C the human URL missed,
  * rows where finder and human disagree on the verdict,
  * rows where the finder found nothing but a manual URL exists,
  * which finder source/host won, to spot coverage gaps.

Fast config: rows run in a thread pool, finder uses static fetch only
(js_renderer=None), tight per-row budget, no SQLite cache (force_refresh).
"""
from __future__ import annotations

import json
import logging
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import _bootstrap  # noqa: F401
from agent.config import load_config
from agent.sheets_client import SheetsClient
from agent.stages import tc_analyzer, tc_finder
from run_portal_sheet import PORTAL_SHEET_ID

logging.basicConfig(level=logging.ERROR, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("tc_finder_compare")

PER_ROW_BUDGET = 18  # seconds for the finder per university
WORKERS = 8          # rows processed concurrently (network-bound, no JS render)


def norm(u: str) -> str:
    try:
        return tc_analyzer.normalize_tc_url(u)
    except Exception:
        return (u or "").strip().lower()


def main() -> None:
    c = load_config()
    sheets = SheetsClient(
        sheet_id=PORTAL_SHEET_ID, universities_tab="x", portals_tab="x",
        credentials_path=c.google_credentials_path, token_path=c.google_token_path,
    )
    rows = sheets._get_values("'15June'", "2:100000")

    def work(i: int, r: list):
        p = list(r) + [""] * 8
        sheet_row = i + 2
        orgid = str(p[0]).strip() or f"row:{sheet_row}"
        name = str(p[1]).strip()
        domain = str(p[2]).strip()
        portal = str(p[3]).strip()
        existing = [u.strip() for u in str(p[4]).split("\n") if u.strip()]
        verdict_now = str(p[5]).strip()
        if not name:
            return None

        domains = [d.strip() for d in re.split(r"[,\s]+", domain) if d.strip()]
        primary = domains[0] if domains else ""
        portal_url = portal.splitlines()[0].strip() if portal.startswith("http") else (
            f"https://{primary}" if primary else ""
        )

        discovered, source = None, None
        if portal_url or domains:
            budget = tc_finder._TCBudget(deadline_at=time.monotonic() + PER_ROW_BUDGET)
            try:
                discovered, source = tc_finder.find_university_tnc(
                    portals=[{"url": portal_url}] if portal_url else [],
                    domains=domains, extra_effective_domains=[],
                    university_domain=primary or None, js_renderer=None,
                    user_agent=c.user_agent, http_timeout=c.http_timeout_seconds,
                    orgid=orgid, budget=budget, university_name=name,
                )
            except Exception as e:
                log.warning("[%s] finder raised: %s", orgid, e)

        disc_verdict = None
        if discovered:
            try:
                res = tc_analyzer.analyze_tc_url(
                    tc_url=discovered, state=None, user_agent=c.user_agent,
                    http_timeout=c.http_timeout_seconds, orgid=orgid,
                    mode="keyword", js_renderer=None, force_refresh=True,
                )
                disc_verdict = res.get("verdict")
            except Exception as e:
                log.warning("[%s] analyze discovered raised: %s", orgid, e)

        existing_norm = {norm(u) for u in existing}
        disc_norm = norm(discovered) if discovered else None
        return {
            "row": sheet_row, "orgid": orgid, "name": name,
            "domain": domain, "portal": portal_url,
            "existing_urls": existing, "verdict_now": verdict_now,
            "discovered": discovered, "source": source, "disc_verdict": disc_verdict,
            "discovered_is_new": bool(disc_norm and disc_norm not in existing_norm),
            "finder_found_nothing": discovered is None,
        }

    out = []
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(work, i, r): i for i, r in enumerate(rows)}
        for n, fut in enumerate(as_completed(futs), 1):
            rec = fut.result()
            if rec is None:
                continue
            out.append(rec)
            print(f"[{n}/{len(rows)}] r{rec['row']:<4} {rec['name'][:26]:26} | now={rec['verdict_now']:6} "
                  f"disc={str(rec['disc_verdict']):6} src={str(rec['source'])[:11]:11} "
                  f"{'NEW' if rec['discovered_is_new'] else '   '} | {str(rec['discovered'])[:48]}",
                  flush=True)
    out.sort(key=lambda x: x["row"])
    json.dump(out, open("/tmp/tc_finder_compare.json", "w"), indent=0)
    print(f"\nWrote {len(out)} rows to /tmp/tc_finder_compare.json", file=sys.stderr)


if __name__ == "__main__":
    main()

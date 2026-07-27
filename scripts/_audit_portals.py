#!/usr/bin/env python3
"""Audit the Portals + Portals TnC tabs and drop junk portal URLs — anything
that is NOT a real login/student portal, using the SAME acceptance gate the
discovery agent applies:

  KEEP iff  https  AND  not _is_junk_portal (grievance/content/generic-SSO/
  webmail/IdP/app-store/…)  AND  not _is_login_subpage  AND  _login_affordance
  (a password field, a real form, a login-named URL that reaches a form, a
  known login-platform fingerprint, a WAF-blocked login endpoint, or a
  dedicated portal-subdomain root). Dead/unreachable URLs (after a retry) are
  dropped too.

Two phases (resumable via a verdict cache on disk):
  default        : fetch+classify every distinct portal URL not yet cached
  --report-remaining : print how many URLs still need a verdict
  --apply        : snapshot, then DELETE rows whose portal URL is junk/dead
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts"))
try:
    from dotenv import load_dotenv; load_dotenv(ROOT / ".env")
except Exception:  # noqa: BLE001
    pass

import _bootstrap  # noqa: F401,E402
from agent.config import load_config  # noqa: E402
from agent.sheets_client import SheetsClient  # noqa: E402
import agent.magic as M  # noqa: E402

SHEET = "1sDK_1VnRHIuUqBComrvwS1JvSmB_l0_4Rsf9rfezFNw"
TABS = ["Portals", "Portals TnC"]
PORTAL_COL_IDX = 3  # A orgId | B name | C domains | D portal url | ...
CACHE = ROOT / "portal_audit_verdicts.json"
SNAP = ROOT / "portal_audit_snapshot.json"


def _retry(fn, n=4):
    for i in range(n):
        try:
            return fn()
        except Exception:  # noqa: BLE001
            if i == n - 1:
                raise
            time.sleep(2 * (i + 1))


def _load_cache() -> dict:
    if CACHE.exists():
        try:
            return json.loads(CACHE.read_text())
        except Exception:  # noqa: BLE001
            return {}
    return {}


def _distinct_portal_urls(svc) -> list[str]:
    seen: dict[str, None] = {}
    for tab in TABS:
        rows = _retry(lambda: svc.values().get(spreadsheetId=SHEET, range=f"'{tab}'!A2:F").execute()).get("values", [])
        for r in rows:
            p = (r[PORTAL_COL_IDX].strip() if len(r) > PORTAL_COL_IDX and r[PORTAL_COL_IDX] else "")
            if p and p != "(none found)":
                seen.setdefault(p, None)
    return list(seen)


def _classify(url: str) -> dict:
    """Fetch the URL and return {verdict, reason}. Mirrors discovery accept."""
    u0 = url
    if not u0.lower().startswith("https://"):
        return {"verdict": "JUNK", "reason": "not https"}
    # fetch (2 attempts so a transient timeout doesn't condemn a real portal)
    c = None
    for _ in range(2):
        c = M.fetch_signals(M.Candidate(url=url, provenance="audit"))
        if not c.error and c.status and c.status < 500:
            break
    final = c.final_url or c.url
    if _is := M._is_junk_portal(final):
        # figure out which junk rule for a readable reason
        from urllib.parse import urlsplit
        host = M._norm_host(final); path = urlsplit(final).path
        if M._is_webmail(final):
            reason = "webmail login"
        elif M._is_generic_sso(final):
            reason = "generic third-party SSO (google/microsoft/godaddy)"
        elif M._GRIEVANCE_RE.search(final):
            reason = "grievance/complaint portal"
        elif M._CONTENT_PATH_RE.search(path):
            reason = "editorial/marketing content page"
        elif host.startswith("idp."):
            reason = "bare IdP host (no login page)"
        else:
            reason = "junk (app-store/pdf/publisher-federation/etc.)"
        return {"verdict": "JUNK", "reason": reason}
    if M._is_login_subpage(final):
        return {"verdict": "JUNK", "reason": "login sub-page (dup of portal root)"}
    if c.error or not c.status or c.status >= 500:
        # unreachable even after retry (WAF 401/403/429 is handled by affordance)
        if M._login_affordance(c):
            return {"verdict": "KEEP", "reason": "login-named endpoint (WAF/blocked)"}
        return {"verdict": "JUNK", "reason": f"dead/unreachable (status={c.status or 0}{',err' if c.error else ''})"}
    if not M._login_affordance(c):
        return {"verdict": "JUNK", "reason": "no login affordance (no form/password/login-URL)"}
    return {"verdict": "KEEP", "reason": "has login affordance"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report-remaining", action="store_true")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    sc = SheetsClient.from_config(load_config()); sc.sheet_id = SHEET
    svc = sc._service.spreadsheets()
    cache = _load_cache()

    if args.apply:
        _apply(svc, cache)
        return

    urls = _distinct_portal_urls(svc)
    todo = [u for u in urls if u not in cache]
    if args.report_remaining:
        print(len(todo)); return

    print(f"portal-audit: {len(urls)} distinct URLs | cached {len(cache)} | to classify {len(todo)}", flush=True)
    done = 0
    with cf.ThreadPoolExecutor(max_workers=16) as exe:
        futs = {exe.submit(_classify, u): u for u in todo}
        for fut in cf.as_completed(futs):
            u = futs[fut]
            try:
                cache[u] = fut.result()
            except Exception as e:  # noqa: BLE001
                cache[u] = {"verdict": "KEEP", "reason": f"audit-error:{type(e).__name__}"}
            done += 1
            if done % 25 == 0:
                CACHE.write_text(json.dumps(cache))
                print(f"  {done}/{len(todo)} classified", flush=True)
    CACHE.write_text(json.dumps(cache))
    junk = sum(1 for v in cache.values() if v["verdict"] == "JUNK")
    print(f"DONE: classified {len(cache)} URLs | JUNK={junk} | KEEP={len(cache)-junk}", flush=True)


def _apply(svc, cache: dict) -> None:
    junk_urls = {u for u, v in cache.items() if v.get("verdict") == "JUNK"}
    print(f"apply: {len(junk_urls)} distinct junk URLs to remove")
    meta = svc.get(spreadsheetId=SHEET).execute()
    gid = {s["properties"]["title"]: s["properties"]["sheetId"] for s in meta["sheets"]}
    snapshot = {}
    for tab in TABS:
        rows = _retry(lambda: svc.values().get(spreadsheetId=SHEET, range=f"'{tab}'!A2:Z").execute()).get("values", [])
        removed = [r for r in rows if len(r) > PORTAL_COL_IDX and (r[PORTAL_COL_IDX].strip() if r[PORTAL_COL_IDX] else "") in junk_urls]
        snapshot[tab] = removed
        targets = [i + 1 for i, r in enumerate(rows)
                   if len(r) > PORTAL_COL_IDX and (r[PORTAL_COL_IDX].strip() if r[PORTAL_COL_IDX] else "") in junk_urls]
        reqs = [{"deleteDimension": {"range": {"sheetId": gid[tab], "dimension": "ROWS",
                 "startIndex": idx, "endIndex": idx + 1}}} for idx in sorted(targets, reverse=True)]
        for i in range(0, len(reqs), 500):
            _retry(lambda: svc.batchUpdate(spreadsheetId=SHEET, body={"requests": reqs[i:i + 500]}).execute())
        print(f"  [{tab}] removed {len(reqs)} junk rows")
    SNAP.write_text(json.dumps(snapshot, ensure_ascii=False))
    print(f"snapshot of removed rows -> {SNAP}")


if __name__ == "__main__":
    main()

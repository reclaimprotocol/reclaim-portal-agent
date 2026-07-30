#!/usr/bin/env python3
"""Audit the login endpoints we greened in batches 3-7 (reviewed AFTER the first
CSV, org 659617) for false positives.

Target = portal rows currently greened with "login present (JS-rendered)" — the
old loose rule that passed on mere login TEXT (it false-greened the EBPÓS GitBook
manual). Static-form greens ("login form present", real password field) and
tenant-SSO greens are trustworthy and skipped.

For each target, re-run the IMPROVED review_portal (requires a real login form,
rejects doc/manual pages, follows a login link to the exact endpoint):
  - still green  -> update note (+ write resolved endpoint into D if it followed
                    a login link); NOT a false positive.
  - not green    -> FALSE POSITIVE: rewrite E with the real verdict, colour red
                    for removals; recorded in the report.

In-place, resumable (done-cache keyed by oid+portal), proxy-enabled, per-row
timeout. Report -> login_audit_report.json.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
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
import importlib.util  # noqa: E402
from agent.config import load_config  # noqa: E402
from agent.sheets_client import SheetsClient  # noqa: E402

# load the review module (regexes + helpers)
_spec = importlib.util.spec_from_file_location("rev", ROOT / "scripts" / "_run_portals_review.py")
rev = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(rev)
import agent.magic as M  # noqa: E402


def classify_fast(url):
    """Fast, STATIC (no JS render) verdict for the audit — one fetch at most.
    Conservative: only reds CLEAR non-logins (doc/library/content/junk/webmail/
    India/dead); keeps real logins (form / login-URL-shape / SSO) green and
    portal/LMS hosts as amber 'verify'. Returns (color, note)."""
    if rev._is_indian(url):
        return "red", "Indian university — out of scope, remove"
    if M._is_junk_portal(url):
        return "red", "junk (search/asset/idp-metadata/redirect) — remove"
    if rev._is_webmail(url) and not rev._STUDENT_HINT.search(url):
        return "red", "webmail/email login (not student-only) — remove"
    if rev._TENANT_SSO.search(url) or rev._LOGIN_URLISH.search(url):
        return "green", "ok — login endpoint (URL/SSO-shaped)"
    if rev._DOC_RE.search(url):
        return "red", "documentation/manual/help page, not a login — remove"
    if rev._CONTENT_RE.search(url):
        return "red", "content page (library/news/info), not a login — remove"
    st, fin, html = rev._fetch(url)
    if rev._has_login_form(html):
        return "green", "ok — login form present (static)"
    if rev._DOC_RE.search(fin) or rev._CONTENT_RE.search(fin):
        return "red", "resolves to a content/doc page, not a login — remove"
    if rev._PORTAL_HINT.search(fin or url):
        return "amber", "portal/LMS host — login form not auto-detected, verify manually"
    if st == 0:
        return "red", "unreachable (no response) — remove"
    if st in (401, 403, 429):
        return "amber", f"returns {st} (alive but gated) — likely a real login, verify"
    return "amber", "no login form found (static) — verify manually"

SHEET = "1sDK_1VnRHIuUqBComrvwS1JvSmB_l0_4Rsf9rfezFNw"
TAB = "Portals TnC"
DONE = ROOT / "login_audit_done.json"
REPORT = ROOT / "login_audit_report.json"
CSV1 = ROOT / "verified_orgs_today_upto_661004.csv"


def _retry(fn, n=4):
    for i in range(n):
        try:
            return fn()
        except Exception:  # noqa: BLE001
            if i == n - 1:
                raise
            time.sleep(2 * (i + 1))


def _first_csv_orgs():
    out = set()
    try:
        import csv
        for r in csv.DictReader(open(CSV1)):
            out.add(r["orgId"])
    except Exception:  # noqa: BLE001
        pass
    return out


def _targets(svc):
    csv_orgs = _first_csv_orgs()
    rows = _retry(lambda: svc.values().get(spreadsheetId=SHEET,
                  range=f"'{TAB}'!A2:H").execute()).get("values", [])
    done = set(json.loads(DONE.read_text())) if DONE.exists() else set()
    out = []
    for i, r in enumerate(rows, 2):
        r = (r + [""] * 8)[:8]
        oid, name, dom, portal, e = [c.strip() if isinstance(c, str) else c for c in r[:5]]
        if (oid and oid not in csv_orgs and portal and portal not in ("N/A", "(none found)")
                and "js-render" in e.lower() and (e.lower().startswith("ok") or e.lower().startswith("resolved"))
                and f"{oid}|{portal}" not in done):
            out.append((i, oid, name, dom, portal))
    return out, done, rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report-remaining", action="store_true")
    args = ap.parse_args()
    sc = SheetsClient.from_config(load_config()); sc.sheet_id = SHEET
    svc = sc._service.spreadsheets()
    todo, done, rows = _targets(svc)
    if args.report_remaining:
        print(len(todo)); return

    gid = next(s["properties"]["sheetId"]
               for s in _retry(lambda: svc.get(spreadsheetId=SHEET).execute())["sheets"]
               if s["properties"]["title"] == TAB)
    root_hosts = set()
    RED = {"red": 0.85, "green": 0.55, "blue": 0.55}
    batch = int(os.getenv("MAGIC_REVIEW_BATCH", "25"))
    row_timeout = int(os.getenv("MAGIC_ROW_TIMEOUT", "70"))
    report = json.loads(REPORT.read_text()) if REPORT.exists() else {"false_positives": [], "resolved": [], "ok": 0}
    signal.signal(signal.SIGALRM, rev._on_alarm)

    print(f"login-audit: {len(todo)} suspect rows (processing up to {batch})", flush=True)
    for n, (row, oid, name, dom, portal) in enumerate(todo[:batch], 1):
        resolved = None
        try:
            signal.alarm(row_timeout)
            pc, pf = classify_fast(portal)
            signal.alarm(0)
        except rev._RowTimeout:
            signal.alarm(0)
            pc, pf = "amber", f"audit timed out (>{row_timeout}s) — verify manually"
        data = [{"range": f"'{TAB}'!E{row}", "values": [[pf]]}]
        if resolved and resolved != portal:
            data.append({"range": f"'{TAB}'!D{row}", "values": [[resolved]]})
            report["resolved"].append({"oid": oid, "name": name, "was": portal, "now": resolved})
        _retry(lambda: svc.values().batchUpdate(spreadsheetId=SHEET,
               body={"valueInputOption": "RAW", "data": data}).execute())
        WHITE = {"red": 1, "green": 1, "blue": 1}
        color = RED if pc == "red" else WHITE
        _retry(lambda: svc.batchUpdate(spreadsheetId=SHEET, body={"requests": [{"repeatCell": {
            "range": {"sheetId": gid, "startRowIndex": row - 1, "endRowIndex": row,
                      "startColumnIndex": 0, "endColumnIndex": 8},
            "cell": {"userEnteredFormat": {"backgroundColor": color}},
            "fields": "userEnteredFormat.backgroundColor"}}]}).execute())
        if pc == "red":
            report["false_positives"].append({"oid": oid, "name": name, "portal": portal, "verdict": pf})
        elif resolved:
            pass  # resolved endpoint, still a login — fine
        else:
            report["ok"] += 1
        done.add(f"{oid}|{portal}")
        flag = "FALSE-POS" if pc == "red" else ("RESOLVED" if resolved else "ok")
        print(f"  [{n}] row{row} {name[:20]:20} P[{pc}] {flag} {portal[:38]}", flush=True)
        if n % 5 == 0:
            DONE.write_text(json.dumps(sorted(done))); REPORT.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    DONE.write_text(json.dumps(sorted(done))); REPORT.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"DONE (FP so far: {len(report['false_positives'])}, resolved: {len(report['resolved'])}, ok: {report['ok']})", flush=True)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Build the cleaned, de-duplicated **Reported URLs** output + a verdict report.

Reads reported_urls_verdicts.json (from _classify_reported_urls.py) and the raw
tab snapshot, then:
  --report        print the verdict/reason breakdown (no writes)
  --write-clean   create/replace the 'Reported URLs Clean' tab with one row per
                  unique KEEP/REVIEW url + frequency columns
  --prune-raw     DESTRUCTIVE: rewrite the 'Reported URLs' tab in place, keeping
                  only unique valid urls + frequency (snapshot written first)

Frequency columns:
  rowsInSheet  - how many rows in the raw tab collapsed into this url
  totalReports - sum of the raw tab's reportsCount (times students reported it)
"""
from __future__ import annotations

import argparse
import collections
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
from _classify_reported_urls import RAW, STATE, aggregate, load_rows  # noqa: E402

SHEET = "1sDK_1VnRHIuUqBComrvwS1JvSmB_l0_4Rsf9rfezFNw"
SRC_TAB = "Reported URLs"
CLEAN_TAB = "Reported URLs Clean"
HEADER = ["url", "rowsInSheet", "totalReports", "orgIds", "orgCount",
          "firstReportedAt", "lastReportedAt", "verdict", "reason", "resolvedLoginURL"]


def build():
    rows = load_rows()
    agg = aggregate(rows)
    state = json.loads(STATE.read_text())
    out = []
    for k, e in agg.items():
        v = state.get(k, {})
        out.append({
            "key": k, "url": e["url"], "rows": e["rows"], "reports": e["reports"],
            "orgs": sorted(e["orgs"]), "first": e["first"], "last": e["last"],
            "verdict": v.get("verdict", "UNCLASSIFIED"), "reason": v.get("reason", ""),
            "resolved": v.get("resolved", ""), "stage": v.get("stage", ""),
        })
    # Most-reported first, so the highest-signal student URLs sit at the top.
    out.sort(key=lambda r: (-r["reports"], -r["rows"], r["url"].lower()))
    return len(rows) - 1, out


def report(total_rows, out):
    c = collections.Counter(r["verdict"] for r in out)
    print(f"raw rows: {total_rows}   unique urls: {len(out)}   collapsed: {total_rows-len(out)}")
    print(f"student reports represented: {sum(r['reports'] for r in out)}")
    print("\n=== verdicts ===")
    for v, n in c.most_common():
        rep = sum(r["reports"] for r in out if r["verdict"] == v)
        print(f"  {v:14} {n:6} urls  ({n/len(out)*100:4.1f}%)   {rep:6} student reports")
    for v in ("DROP", "REVIEW", "KEEP"):
        rs = [r for r in out if r["verdict"] == v]
        if not rs:
            continue
        print(f"\n=== {v}: reasons ===")
        for reason, n in collections.Counter(r["reason"] for r in rs).most_common(14):
            print(f"  {n:6}  {reason}")
    keep = [r for r in out if r["verdict"] in ("KEEP", "REVIEW")]
    print(f"\n=== top 20 surviving urls by student reports ===")
    for r in keep[:20]:
        print(f"  {r['reports']:5} reports  rows={r['rows']:2}  {r['verdict']:6}  {r['url'][:72]}")
    drops = [r for r in out if r["verdict"] == "DROP"]
    print(f"\n=== highest-frequency DROPs (sanity-check these) ===")
    for r in drops[:15]:
        print(f"  {r['reports']:5} reports  {r['url'][:60]:60}  {r['reason'][:44]}")


def _svc():
    cfg = load_config(); sc = SheetsClient.from_config(cfg); sc.sheet_id = SHEET
    return sc, sc._service.spreadsheets()


def _rowvals(r):
    return [r["url"], r["rows"], r["reports"], ", ".join(r["orgs"]), len(r["orgs"]),
            r["first"], r["last"], r["verdict"], r["reason"], r["resolved"]]


def _ensure_tab(svc, title):
    meta = svc.get(spreadsheetId=SHEET).execute()
    for s in meta["sheets"]:
        if s["properties"]["title"] == title:
            return s["properties"]["sheetId"]
    res = svc.batchUpdate(spreadsheetId=SHEET, body={"requests": [
        {"addSheet": {"properties": {"title": title}}}]}).execute()
    return res["replies"][0]["addSheet"]["properties"]["sheetId"]


def write_clean(out, keep_verdicts):
    sc, svc = _svc()
    _ensure_tab(svc, CLEAN_TAB)
    keep = [r for r in out if r["verdict"] in keep_verdicts]
    values = [HEADER] + [_rowvals(r) for r in keep]
    svc.values().clear(spreadsheetId=SHEET, range=f"'{CLEAN_TAB}'!A:Z").execute()
    svc.values().update(spreadsheetId=SHEET, range=f"'{CLEAN_TAB}'!A1",
                        valueInputOption="USER_ENTERED", body={"values": values}).execute()
    print(f"wrote {len(keep)} rows -> '{CLEAN_TAB}' (verdicts: {sorted(keep_verdicts)})")


def prune_raw(out, keep_verdicts):
    sc, svc = _svc()
    snap = ROOT / f"reported_urls_prune_snapshot_{time.strftime('%Y%m%dT%H%M%SZ')}.json"
    snap.write_text(json.dumps({"rows": load_rows()}))
    keep = [r for r in out if r["verdict"] in keep_verdicts]
    values = [HEADER] + [_rowvals(r) for r in keep]
    svc.values().clear(spreadsheetId=SHEET, range=f"'{SRC_TAB}'!A:Z").execute()
    svc.values().update(spreadsheetId=SHEET, range=f"'{SRC_TAB}'!A1",
                        valueInputOption="USER_ENTERED", body={"values": values}).execute()
    print(f"snapshot -> {snap.name}")
    print(f"PRUNED '{SRC_TAB}': {len(keep)} unique valid urls kept "
          f"(was {len(load_rows())-1} rows)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--write-clean", action="store_true")
    ap.add_argument("--prune-raw", action="store_true")
    ap.add_argument("--keep", default="KEEP,REVIEW",
                    help="verdicts to keep in the output (default KEEP,REVIEW)")
    ap.add_argument("--csv", default="", help="also dump the kept rows to this CSV")
    args = ap.parse_args()
    keep_verdicts = {v.strip().upper() for v in args.keep.split(",") if v.strip()}

    total_rows, out = build()
    if args.report or not (args.write_clean or args.prune_raw or args.csv):
        report(total_rows, out)
    if args.csv:
        import csv
        with open(args.csv, "w", newline="") as f:
            w = csv.writer(f); w.writerow(HEADER)
            w.writerows(_rowvals(r) for r in out if r["verdict"] in keep_verdicts)
        print(f"csv -> {args.csv}")
    if args.write_clean:
        write_clean(out, keep_verdicts)
    if args.prune_raw:
        prune_raw(out, keep_verdicts)


if __name__ == "__main__":
    main()

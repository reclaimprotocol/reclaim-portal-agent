#!/usr/bin/env python3
"""Fill a 'tnc url' column (col H) on the **Pending928** tab.

  --reuse      copy a known T&C from Portals TnC / India Portals TnC (free)
  --waterfall  run the T&C waterfall for the rest, via review_or_fill_tnc
  --mark-na    write N/A for rows whose portal is N/A (nothing to attach a T&C to)

Only rows with a REAL portal get a waterfall attempt. Sharded + resumable: each
shard keeps its own done-file (a single shared file loses writes when six shards
race on it — that clobbered pending928_portals.json).
"""
from __future__ import annotations
import argparse, collections, json, os, signal, sys
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
import _run_portals_review as R  # noqa: E402

TAB, COL = "Pending928", "H"
ap = argparse.ArgumentParser()
ap.add_argument("--reuse", action="store_true")
ap.add_argument("--waterfall", action="store_true")
ap.add_argument("--mark-na", action="store_true")
ap.add_argument("--shard", type=int, default=0)
ap.add_argument("--of", type=int, default=1)
ap.add_argument("--report-remaining", action="store_true")
a = ap.parse_args()

sc = SheetsClient.from_config(load_config()); sc.sheet_id = R.SHEET
svc = sc._service.spreadsheets()
def load():
    return [(r + [""] * 8)[:8] for r in
            svc.values().get(spreadsheetId=R.SHEET, range=f"'{TAB}'!A2:H100000").execute().get("values", [])]
rows = load()

def realportal(r):
    p = (r[5] or "").strip()
    return bool(p) and p.upper() != "N/A"

DONE = ROOT / (f"pending928_tnc_done_shard{a.shard}.json" if a.of > 1 else "pending928_tnc_done.json")
done = set()
for p in ROOT.glob("pending928_tnc_done*.json"):
    try: done |= set(json.loads(p.read_text()))
    except Exception: pass  # noqa: BLE001
mine = set(json.loads(DONE.read_text())) if DONE.exists() else set()

if a.reuse:
    known = collections.defaultdict(list)
    g = svc.values().get(spreadsheetId=R.SHEET, range=f"'{R.TAB}'!A1:K").execute().get("values", [])
    hdr = g[0]; W = max(len(x) for x in g)
    idx = {(x or "").strip().lower(): i for i, x in enumerate(hdr)}
    O, T = idx["organization id"], idx["t&c url"]
    for r in g[1:]:
        r = (r + [""] * W)[:W]
        o, t = (r[O] or "").strip(), (r[T] or "").strip()
        if o and t.lower().startswith("http") and t not in known[o]: known[o].append(t)
    for r in svc.values().get(spreadsheetId=R.SHEET, range="'India Portals TnC'!A2:H100000").execute().get("values", []):
        r = (r + [""] * 8)[:8]
        o, t = (r[0] or "").strip(), (r[6] or "").strip()
        if o and t.lower().startswith("http") and t not in known[o]: known[o].append(t)
    ups = []
    for i, r in enumerate(rows, start=2):
        o = (r[0] or "").strip()
        if not realportal(r) or (r[7] or "").strip():
            continue
        if known.get(o):
            ups.append({"range": f"'{TAB}'!{COL}{i}", "values": [[known[o][0]]]})
    for j in range(0, len(ups), 500):
        R._retry(lambda: svc.values().batchUpdate(spreadsheetId=R.SHEET,
                 body={"valueInputOption": "RAW", "data": ups[j:j+500]}).execute())
    print(f"reuse: filled {len(ups)} T&C cells", flush=True)
    rows = load()

if a.mark_na:
    ups = [{"range": f"'{TAB}'!{COL}{i}", "values": [["N/A"]]}
           for i, r in enumerate(rows, start=2)
           if (r[0] or "").strip() and not realportal(r) and not (r[7] or "").strip()]
    for j in range(0, len(ups), 500):
        R._retry(lambda: svc.values().batchUpdate(spreadsheetId=R.SHEET,
                 body={"valueInputOption": "RAW", "data": ups[j:j+500]}).execute())
    print(f"mark-na: wrote N/A on {len(ups)} rows (no portal)", flush=True)
    rows = load()

if a.waterfall or a.report_remaining:
    todo = [(i, r) for i, r in enumerate(rows, start=2)
            if realportal(r) and not (r[7] or "").strip()
            and f"{(r[0] or '').strip()}|{(r[5] or '').strip()}" not in done]
    todo = [t for n, t in enumerate(todo) if n % a.of == a.shard]
    if a.report_remaining:
        print(len(todo)); sys.exit(0)
    print(f"waterfall shard {a.shard}/{a.of}: {len(todo)} rows", flush=True)
    signal.signal(signal.SIGALRM, R._on_alarm)
    to = int(os.getenv("TNC_ROW_TIMEOUT", "90"))
    cache: dict = {}
    hits = 0
    for n, (row, r) in enumerate(todo, 1):
        oid, name, web, portal = (r[0] or "").strip(), (r[1] or "").strip(), (r[2] or "").strip(), (r[5] or "").strip()
        key = f"{oid}|{portal}"
        try:
            signal.alarm(to)
            gnew, _tc, tf = R.review_or_fill_tnc("", portal, web, name, cache)
            signal.alarm(0)
        except R._RowTimeout:
            signal.alarm(0); R._reset_renderer(); gnew = "N/A"
        except Exception:  # noqa: BLE001
            signal.alarm(0); gnew = "N/A"
        val = gnew if str(gnew).lower().startswith("http") else "N/A"
        R._retry(lambda: svc.values().update(spreadsheetId=R.SHEET, range=f"'{TAB}'!{COL}{row}",
                 valueInputOption="RAW", body={"values": [[val]]}).execute())
        mine.add(key); DONE.write_text(json.dumps(sorted(mine)))
        hits += val != "N/A"
        print(f"  [{n}/{len(todo)}] row{row} {name[:22]:22} {val[:52]}", flush=True)
    print(f"DONE ({hits}/{len(todo)} got a T&C)", flush=True)

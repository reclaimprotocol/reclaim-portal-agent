#!/usr/bin/env python3
"""Re-verify portal URLs on **Portals TnC** — is each one still reachable and an
actual login page? Writes a fresh verdict into the Portal-review column.

Unlike _run_portals_review.py this does NOT assume column letters: every column
is resolved from the header row, because the tab has been rearranged twice
(country inserted at B, requests at C, T&C Type appended). Running the old
hardcoded reviewer against the current layout would write verdicts into
'Category' and read 'Email Domains' as the portal.

Scope:
  --scope no-tnc   (default) orgs that have a portal but no http T&C
  --scope all      every portal row on the tab

Re-verification is FORCED — existing verdicts are overwritten, since the point
is to re-check them. Progress is tracked in reverify_done.json keyed on
(orgId, portal), so shards never redo each other's work and a killed pass
resumes. Each row is pre-marked before the fetch so a page that wedges the
headless browser costs one row, not the whole pass.
"""
from __future__ import annotations
import argparse, json, os, signal, sys
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

ap = argparse.ArgumentParser()
ap.add_argument("--scope", default="no-tnc", choices=["no-tnc", "all"])
ap.add_argument("--shard", type=int, default=0)
ap.add_argument("--of", type=int, default=1)
ap.add_argument("--batch", type=int, default=int(os.getenv("REVERIFY_BATCH", "60")))
ap.add_argument("--report-remaining", action="store_true")
a = ap.parse_args()

sc = SheetsClient.from_config(load_config()); sc.sheet_id = R.SHEET
svc = sc._service.spreadsheets()
grid = svc.values().get(spreadsheetId=R.SHEET, range=f"'{R.TAB}'!A1:Z").execute().get("values", [])
header = grid[0]; W = max(len(r) for r in grid)
rows = [(r + [""] * W)[:W] for r in grid[1:]]

def col(name):
    for i, h in enumerate(header):
        if (h or "").strip().lower() == name.lower():
            return i
    raise SystemExit(f"column {name!r} not found in {header}")

C_ORG, C_PORTAL, C_VERDICT, C_TNC = (col("Organization ID"), col("Portal URL"),
                                     col("Portal Human review"), col("T&C URL"))
A1 = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
VCOL, PCOL = A1[C_VERDICT], A1[C_PORTAL]

def real(r):
    p = (r[C_PORTAL] or "").strip()
    return bool(p) and p != "(none found)"

by = {}
for i, r in enumerate(rows, start=2):
    by.setdefault((r[C_ORG] or "").strip(), []).append((i, r))
scope_orgs = set()
for o, rs in by.items():
    if not o:
        continue
    if a.scope == "all" or not any((rr[C_TNC] or "").strip().lower().startswith("http") for _, rr in rs):
        scope_orgs.add(o)

DONE = ROOT / "reverify_done.json"
done = set()
for p in ROOT.glob("reverify_done*.json"):
    try:
        done |= set(json.loads(p.read_text()))
    except Exception:  # noqa: BLE001
        pass
MINE = ROOT / (f"reverify_done_shard{a.shard}.json" if a.of > 1 else "reverify_done.json")
mine = set(json.loads(MINE.read_text())) if MINE.exists() else set()

def key(r):
    return f"{(r[C_ORG] or '').strip()}|{(r[C_PORTAL] or '').strip()}"

todo = [(i, r) for i, r in enumerate(rows, start=2)
        if (r[C_ORG] or "").strip() in scope_orgs and real(r) and key(r) not in done]
todo = [t for n, t in enumerate(todo) if n % a.of == a.shard]

if a.report_remaining:
    print(len(todo)); sys.exit(0)

print(f"re-verify shard {a.shard}/{a.of} scope={a.scope}: {len(todo)} portal rows "
      f"(verdict->col {VCOL}, portal<-col {PCOL})", flush=True)
if a.batch and len(todo) > a.batch:
    print(f"(processing {a.batch} this invocation)", flush=True)
    todo = todo[: a.batch]

root_hosts = {M._norm_host((r[C_PORTAL] or "").strip()) for r in rows if real(r)}
signal.signal(signal.SIGALRM, R._on_alarm)
row_timeout = int(os.getenv("REVERIFY_ROW_TIMEOUT", "70"))
stats = {"green": 0, "amber": 0, "red": 0}
for n, (row, r) in enumerate(todo, 1):
    url = (r[C_PORTAL] or "").strip()
    R._retry(lambda: svc.values().update(
        spreadsheetId=R.SHEET, range=f"'{R.TAB}'!{VCOL}{row}", valueInputOption="RAW",
        body={"values": [["re-verify did not complete (page hung) — verify manually"]]}).execute())
    try:
        signal.alarm(row_timeout)
        color, note, resolved = R.review_portal(url, root_hosts)
        signal.alarm(0)
    except R._RowTimeout:
        signal.alarm(0); R._reset_renderer()
        color, note, resolved = "amber", f"re-verify timed out (>{row_timeout}s) — verify manually", None
    except Exception as e:  # noqa: BLE001
        signal.alarm(0)
        color, note, resolved = "amber", f"re-verify error ({type(e).__name__})", None
    data = [{"range": f"'{R.TAB}'!{VCOL}{row}", "values": [[note]]}]
    if resolved and resolved != url:
        data.append({"range": f"'{R.TAB}'!{PCOL}{row}", "values": [[resolved]]})
    R._retry(lambda: svc.values().batchUpdate(spreadsheetId=R.SHEET,
             body={"valueInputOption": "RAW", "data": data}).execute())
    mine.add(key(r)); MINE.write_text(json.dumps(sorted(mine)))
    stats[color] = stats.get(color, 0) + 1
    print(f"  [{n}/{len(todo)}] row{row} [{color}] {url[:56]}", flush=True)
print(f"DONE {stats}", flush=True)

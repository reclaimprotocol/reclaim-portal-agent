#!/usr/bin/env python3
"""Export the NEXT delivery batch from **Portals TnC**: every org with at least
one T&C that has not already gone out in a previous batch CSV.

Keyed on ORG, not row position. An org's rows can sit in several places on the
tab (Portals TnC has ~663 duplicated (org,portal) pairs), so "everything below
row N" both repeats already-delivered orgs and splits an org's rows across
batches. This takes whole orgs and all of their rows.

Output columns match thirdbatch.csv exactly:  orgId, portal url, tnc url
One row per unique (orgId, portal, tnc); exact duplicates collapse.

    .venv/bin/python scripts/_export_next_batch.py --out fourthbatch.csv
    .venv/bin/python scripts/_export_next_batch.py --exclude thirdbatch.csv --out x.csv
"""
from __future__ import annotations
import argparse, csv, sys
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
import _run_portals_review as R  # noqa: E402

DEFAULT_EXCLUDE = ["thirdbatch.csv", "all_orgs_portals_with_tnc.csv",
                   "reviewed_portals_with_tnc.csv", "verified_orgs_with_tnc.csv",
                   "verified_orgs_today_upto_661004.csv", "20July_portals_with_tnc.csv"]

ap = argparse.ArgumentParser()
ap.add_argument("--out", default="fourthbatch.csv")
ap.add_argument("--exclude", nargs="*", default=None,
                help="prior batch CSVs whose orgs are already delivered")
ap.add_argument("--dry-run", action="store_true")
a = ap.parse_args()
excl_files = a.exclude if a.exclude is not None else DEFAULT_EXCLUDE

delivered, per_file = set(), {}
for f in excl_files:
    p = ROOT / f
    if not p.exists():
        print(f"  ! missing, skipped: {f}"); continue
    s = {r[0].strip() for r in list(csv.reader(open(p)))[1:] if r and r[0].strip()}
    per_file[f] = len(s); delivered |= s
for f, n in per_file.items():
    print(f"  excluded {n:5} orgs from {f}")
print(f"already delivered (union): {len(delivered)} orgs")

sc = SheetsClient.from_config(load_config()); sc.sheet_id = R.SHEET
rows = sc._service.spreadsheets().values().get(
    spreadsheetId=R.SHEET, range=f"'{R.TAB}'!A2:H").execute().get("values", [])

by_org: dict[str, list] = {}
orgs_no_tnc: set[str] = set()
for r in rows:
    r = (r + [""] * 8)[:8]
    oid = (r[0] or "").strip(); portal = (r[3] or "").strip(); tnc = (r[6] or "").strip()
    if not oid or not portal or portal == "(none found)":
        continue
    if tnc.lower().startswith("http"):
        by_org.setdefault(oid, []).append((oid, portal, tnc))
    else:
        orgs_no_tnc.add(oid)

new_orgs = [o for o in by_org if o not in delivered]
out, seen = [], set()
for oid in new_orgs:
    for t in by_org[oid]:
        if t in seen:
            continue
        seen.add(t); out.append(t)

print(f"\nPortals TnC orgs with >=1 T&C : {len(by_org)}")
print(f"  already delivered           : {len(by_org) - len(new_orgs)}")
print(f"  NEW -> this batch           : {len(new_orgs)}")
print(f"rows (orgId, portal, tnc)     : {len(out)}")
print(f"orgs with portals but NO T&C  : {len(orgs_no_tnc - set(by_org))}")

if a.dry_run:
    print("\ndry-run — no file written"); sys.exit(0)
with open(ROOT / a.out, "w", newline="") as f:
    w = csv.writer(f); w.writerow(["orgId", "portal url", "tnc url"]); w.writerows(out)
print(f"\nwrote {a.out}")

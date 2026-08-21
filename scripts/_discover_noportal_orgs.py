#!/usr/bin/env python3
"""Retry portal discovery for **Portals TnC** orgs that currently have no portal
("(none found)" on every row).

These orgs failed discovery on an earlier pass, so this is a genuine retry — it
benefits from whatever has been added since (region packs, affiliation-parent
discovery, learned subdomain patterns). Country comes from the 9July tab so the
region pack for that country activates.

Writes back into Portals TnC: the first portal replaces the "(none found)" cell
on the org's existing row (col E cleared so the review pass will pick it up);
extra portals are appended as new rows. Resumable — an org that now has a real
portal is skipped.
"""
from __future__ import annotations
import os, re, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts"))
try:
    from dotenv import load_dotenv; load_dotenv(ROOT / ".env")
except Exception:  # noqa: BLE001
    pass
os.environ.setdefault("MAGIC_TNC", "0")
import _bootstrap  # noqa: F401,E402
from agent.config import load_config  # noqa: E402
from agent.sheets_client import SheetsClient  # noqa: E402
from agent import magic as G  # noqa: E402
import _run_portals_review as R  # noqa: E402

sc = SheetsClient.from_config(load_config()); sc.sheet_id = R.SHEET
svc = sc._service.spreadsheets()

ctry = {}
for r in svc.values().get(spreadsheetId=R.SHEET, range="'9July'!A2:D100000").execute().get("values", []):
    r = (r + [""] * 4)[:4]
    if (r[0] or "").strip() and (r[3] or "").strip():
        ctry.setdefault(r[0].strip(), r[3].strip())
for r in svc.values().get(spreadsheetId=R.SHEET, range="'Country'!A2:D100000").execute().get("values", []):
    r = (r + [""] * 4)[:4]
    if (r[0] or "").strip() and (r[3] or "").strip():
        ctry.setdefault(r[0].strip(), r[3].strip())

rows = [(r + [""] * 8)[:8] for r in
        svc.values().get(spreadsheetId=R.SHEET, range=f"'{R.TAB}'!A2:H").execute().get("values", [])]
byorg: dict = {}
for i, r in enumerate(rows, start=2):
    byorg.setdefault((r[0] or "").strip(), []).append((i, r))
targets = [(rs[0][0], rs[0][1]) for oid, rs in byorg.items()
           if oid and all(((rr[3] or "").strip() in ("", "(none found)")) for _, rr in rs)]
print(f"orgs with no portal: {len(targets)}", flush=True)

found_total = 0
for n, (row, r) in enumerate(targets, 1):
    oid, name, dom = (r[0] or "").strip(), (r[1] or "").strip(), (r[2] or "").strip()
    country = ctry.get(oid, "")
    primary = next((d.strip() for d in re.split(r"[,\s]+", dom) if d.strip()), "")
    if not primary:
        print(f"  [{n}/{len(targets)}] {name[:28]:28} SKIP (no domain)", flush=True); continue
    try:
        portals = G.discover(name, primary, country)
    except Exception as e:  # noqa: BLE001
        print(f"  [{n}/{len(targets)}] {name[:28]:28} ERROR {type(e).__name__}: {e}", flush=True); continue
    if not portals:
        print(f"  [{n}/{len(targets)}] {name[:28]:28} [{country}] -> still none", flush=True); continue
    first = portals[0]
    R._retry(lambda: svc.values().batchUpdate(spreadsheetId=R.SHEET, body={
        "valueInputOption": "RAW",
        "data": [{"range": f"'{R.TAB}'!D{row}", "values": [[first["url"]]]},
                 {"range": f"'{R.TAB}'!E{row}", "values": [[""]]},
                 {"range": f"'{R.TAB}'!F{row}", "values": [[first.get("category", "")]]}]}).execute())
    extra = [[oid, name, dom, p["url"], "", p.get("category", ""), "N/A", ""] for p in portals[1:]]
    if extra:
        R._retry(lambda: svc.values().append(
            spreadsheetId=R.SHEET, range=f"'{R.TAB}'!A:H", valueInputOption="RAW",
            insertDataOption="INSERT_ROWS", body={"values": extra}).execute())
    found_total += len(portals)
    print(f"  [{n}/{len(targets)}] {name[:28]:28} [{country}] -> {len(portals)} portals", flush=True)
    for p in portals[:4]:
        print(f"        {p['url'][:88]}", flush=True)
print(f"DONE — {found_total} portals across {len(targets)} orgs", flush=True)

#!/usr/bin/env python3
"""Reshape the **India No Portals** tab into ONE ROW PER PORTAL with its T&C,
written to a new tab **India No Portals - per portal**.

Source : A orgId | B name | C website | D portal url (newline-joined) | E tnc url (newline-joined)
Output : orgId | name | website | portal url | tnc url   (one row per portal)

For each org row we split D into individual portals. The T&C for each portal
comes from the aligned E line when the line counts match; otherwise (a portal
was added manually after the T&C pass) we re-run magic_tnc.find_tnc for that
portal so it gets a correct T&C. Orgs with no portal keep one row with
portal="(none found)" and tnc="N/A" so no org is lost. Non-destructive: the
original tab is left intact.
"""
from __future__ import annotations

import re
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
from _inactive import INACTIVE  # noqa: E402
from agent.config import load_config  # noqa: E402
from agent.sheets_client import SheetsClient  # noqa: E402
from agent import magic_tnc as T  # noqa: E402

SHEET = "1sDK_1VnRHIuUqBComrvwS1JvSmB_l0_4Rsf9rfezFNw"
SRC = "India No Portals"
OUT = "India No Portals - per portal"
COUNTRY = "India"


def _retry(fn, n=4):
    for i in range(n):
        try:
            return fn()
        except Exception:  # noqa: BLE001
            if i == n - 1:
                raise
            time.sleep(2 * (i + 1))


def main() -> None:
    sc = SheetsClient.from_config(load_config()); sc.sheet_id = SHEET
    svc = sc._service.spreadsheets()
    rows = _retry(lambda: svc.values().get(spreadsheetId=SHEET, range=f"'{SRC}'!A2:E").execute()).get("values", [])

    cache: dict = {}
    out = [["orgId", "name", "website", "portal url", "tnc url"]]
    n_portals = n_rerun = 0
    for r in rows:
        oid = (r[0].strip() if r and r[0] else "")
        if not oid:
            continue
        name = (r[1].strip() if len(r) > 1 and r[1] else "")
        website = (r[2].strip() if len(r) > 2 and r[2] else "")
        d = (r[3].strip() if len(r) > 3 and r[3] else "")
        e = (r[4].strip() if len(r) > 4 and r[4] else "")
        portals = [x.strip() for x in d.splitlines() if x.strip()]
        tncs = [x.strip() for x in e.splitlines() if x.strip()]

        if not portals or d == "(none found)":
            out.append([oid, name, website, "(none found)", "N/A"])
            continue

        aligned = len(tncs) == len(portals)
        uni_domain = next((dd.strip() for dd in re.split(r"[,\s]+", website) if dd.strip()), "")
        for idx, p in enumerate(portals):
            n_portals += 1
            if aligned:
                tnc = tncs[idx] or "N/A"
            elif oid in INACTIVE:
                tnc = "N/A"  # don't spend calls on inactive orgs
            else:
                # portal added after the T&C pass — resolve it now
                n_rerun += 1
                try:
                    res = T.find_tnc(p, uni_domain, name, COUNTRY, cache=cache)
                    items = res.get("tncs") or []
                    tnc = items[0]["url"] if items else "N/A"
                except Exception:  # noqa: BLE001
                    tnc = "N/A"
            out.append([oid, name, website, p, tnc])

    # write to the output tab (create/replace contents)
    titles = [s["properties"]["title"] for s in _retry(lambda: svc.get(spreadsheetId=SHEET).execute())["sheets"]]
    if OUT not in titles:
        _retry(lambda: svc.batchUpdate(spreadsheetId=SHEET, body={
            "requests": [{"addSheet": {"properties": {"title": OUT}}}]}).execute())
    _retry(lambda: svc.values().clear(spreadsheetId=SHEET, range=f"'{OUT}'!A1:Z100000").execute())
    for i in range(0, len(out), 500):
        chunk = out[i:i + 500]
        _retry(lambda: svc.values().append(
            spreadsheetId=SHEET, range=f"'{OUT}'!A1",
            valueInputOption="USER_ENTERED", insertDataOption="INSERT_ROWS",
            body={"values": chunk}).execute())
    print(f"wrote {len(out)-1} portal rows to {OUT!r} "
          f"({n_portals} real portals, {n_rerun} T&C re-run for manual additions)", flush=True)


if __name__ == "__main__":
    main()

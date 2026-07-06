#!/usr/bin/env python3
"""Ingest the 26 Indian state tabs (office sheet) into the Genie DB.

Each state tab: A=University Name, B=City, C=Category(org type), D=Website,
E=Portals URL (newline/comma separated; '(no portal found)' / blank = none).

Writes to `universities` (all rows, incl. no-portal ones → for the future
in-row Discover button) and `portals` (rows that have portals), tagged with
state / city / org_type, source='state-sheet', status='confirmed'.

Usage:  .venv/bin/python genie/etl_states.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "genie" / "core"))

import _bootstrap  # noqa: F401,E402
from agent.config import load_config  # noqa: E402
from agent.sheets_client import SheetsClient  # noqa: E402
from run_portal_sheet import PORTAL_SHEET_ID  # noqa: E402
from genie_core import db  # noqa: E402

STATES = ["Maharashtra", "Uttar Pradesh", "Karnataka", "Madhya Pradesh", "Kerala",
          "Odisha", "Jharkhand", "Delhi", "Himachal Pradesh", "Haryana", "Gujarat",
          "Goa", "Rajasthan", "Telangana", "Tamil Nadu", "West Bengal", "Uttarakhand",
          "Bihar", "Chandigarh", "Assam", "Arunachal Pradesh", "Manipur", "Meghalaya",
          "Mizoram", "Nagaland", "Jammu Kashmir"]


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:60] or "x"


def host(url: str) -> str:
    if not url or not url.lower().startswith("http"):
        return ""
    h = (urlparse(url).netloc or "").lower().split(":")[0]
    return h[4:] if h.startswith("www.") else h


def split_portals(cell: str) -> list[str]:
    if not cell or "no portal" in cell.lower():
        return []
    return [u.strip() for u in re.split(r"[\r\n,]+", cell) if u.strip().lower().startswith("http")]


def infer_category(url: str) -> str:
    # URL-only classification (canonical labels). Run genie/reclassify.py after
    # ingest to enrich these with page-content signals.
    from genie_core import categorize as cz
    cat, _score, _ev = cz.classify(url, fetch=False)
    return cat


def main() -> None:
    config = load_config()
    db.init_db()
    s = SheetsClient(sheet_id=PORTAL_SHEET_ID, universities_tab="x", portals_tab="x",
                     credentials_path=config.google_credentials_path, token_path=config.google_token_path)
    meta = s._service.spreadsheets().get(spreadsheetId=PORTAL_SHEET_ID).execute()
    tabs = {sh["properties"]["title"] for sh in meta["sheets"]}

    total_uni = total_portal = no_portal = 0
    with db.connect() as conn:
        for state in STATES:
            if state not in tabs:
                print(f"  (skip {state}: tab not found)")
                continue
            rows = s._get_values(f"'{state}'", "2:100000")
            for r in rows:
                name = str(r[0]).strip() if r else ""
                if not name:
                    continue
                city = str(r[1]).strip() if len(r) > 1 else ""
                org_type = str(r[2]).strip() if len(r) > 2 else ""
                website = str(r[3]).strip() if len(r) > 3 else ""
                if not website.lower().startswith("http"):
                    website = ""
                portals = split_portals(str(r[4]) if len(r) > 4 else "")
                dom = host(website) or (host(portals[0]) if portals else "")
                orgid = f"in:{slug(state)}:{slug(name)}"
                db.upsert_university(conn, orgid=orgid, name=name, city=city, state=state,
                                     org_type=org_type, website=website, country="India",
                                     source="state-sheet")
                total_uni += 1
                if not portals:
                    no_portal += 1
                    continue
                for p in portals:
                    db.upsert_portal(conn, orgid=orgid, university=name, domain=dom or host(p),
                                     portal_url=p, category=infer_category(p), source="state-sheet",
                                     country="India", state=state, city=city, org_type=org_type,
                                     status="confirmed")
                    total_portal += 1
        conn.commit()

    st = db.stats()
    print(f"ingested {total_uni} universities ({no_portal} with no portal), {total_portal} portals")
    print(f"DB now: {st['portals']} portals across {st['universities']} orgs")
    print("states:", ", ".join(f"{x['state']}({x['count']})" for x in db.states()))


if __name__ == "__main__":
    main()

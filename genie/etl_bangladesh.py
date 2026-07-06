#!/usr/bin/env python3
"""Ingest Bangladesh universities/colleges into the Genie DB.

Sheet 1_V2-9XVua309pXmVUTpw-k5uPDT1bU2cTlRGk5uWzSs, tabs:
  • 'Bangladesh'     — University Name | Website | Login portals
  • 'NU_Bangladesh'  — College Code | College Name | District | Type | Website

Writes to `universities` (all rows incl. no-website/no-portal) + `portals`
(rows that have login URLs), tagged country='Bangladesh', District→state.

Usage:  .venv/bin/python genie/etl_bangladesh.py
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
from genie_core import db  # noqa: E402
from genie_core import categorize as cz  # noqa: E402

SHEET_ID = "1_V2-9XVua309pXmVUTpw-k5uPDT1bU2cTlRGk5uWzSs"
COUNTRY = "Bangladesh"


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:70] or "x"


def host(url: str) -> str:
    if not url or not url.lower().startswith("http"):
        return ""
    h = (urlparse(url).netloc or "").lower().split(":")[0]
    return h[4:] if h.startswith("www.") else h


def split_portals(cell: str) -> list[str]:
    if not cell or "no portal" in cell.lower():
        return []
    return [u.strip() for u in re.split(r"[\r\n,]+", cell) if u.strip().lower().startswith("http")]


def _get(row: dict, *keys: str) -> str:
    for k in keys:
        if k in row and row[k]:
            return str(row[k]).strip()
    return ""


def main() -> None:
    cfg = load_config()
    sc = SheetsClient.from_config(cfg)
    sc.sheet_id = SHEET_ID
    meta = sc._service.spreadsheets().get(spreadsheetId=SHEET_ID).execute()
    tabs = [s["properties"]["title"] for s in meta.get("sheets", [])]

    db.init_db()
    n_uni = n_portal = 0
    with db.connect() as conn:
        for tab in tabs:
            rows = sc._read_tab_as_dicts(tab)
            print(f"{tab!r}: {len(rows)} rows")
            for r in rows:
                name = _get(r, "University Name", "College Name", "Name")
                if not name:
                    continue
                code = _get(r, "College Code")
                website = _get(r, "Website")
                district = _get(r, "District")
                org_type = _get(r, "Type")
                portals_cell = _get(r, "Login portals", "Login Portals", "Portals")
                orgid = f"bd:nu:{code}" if code else f"bd:{slug(name)}"

                db.upsert_university(
                    conn, orgid=orgid, name=name, city="", state=district,
                    org_type=org_type, website=website, country=COUNTRY, source="bangladesh-sheet")
                n_uni += 1

                for url in split_portals(portals_cell):
                    cat = cz.classify(url, fetch=False)[0]
                    db.upsert_portal(
                        conn, orgid=orgid, university=name, domain=host(url), portal_url=url,
                        category=cat, source="bangladesh-sheet", country=COUNTRY,
                        state=district, org_type=org_type, status="confirmed")
                    n_portal += 1
        conn.commit()
    print(f"\n✓ upserted {n_uni} universities, {n_portal} portals for {COUNTRY}")
    with db.connect() as c:
        u = c.execute("SELECT COUNT(*) FROM universities WHERE country=?", (COUNTRY,)).fetchone()[0]
        p = c.execute("SELECT COUNT(*) FROM portals WHERE country=?", (COUNTRY,)).fetchone()[0]
        nw = c.execute("SELECT COUNT(*) FROM universities WHERE country=? AND (website='' OR website IS NULL)", (COUNTRY,)).fetchone()[0]
    print(f"DB now: {u} Bangladesh universities ({nw} without a website), {p} portals")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Ingest the 'Verified Orgs' tab (office sheet) into the Genie DB.

These are universities whose login portals are LIVE in production. Columns:
  A=SheerID OrgID  B=University Name  C=Reclaim Protocol Login Page Url
  D=Terms URLs     E=Reclaim Protocol Provider ID   F=Legal Notes

Writes to the `verified_orgs` table (orgid keyed). The UI shows a blue
verified tick for any university/portal that matches (by orgid, name, or
exact live URL/host).

Usage:  .venv/bin/python genie/etl_verified.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "genie" / "core"))

import re  # noqa: E402
from urllib.parse import urlparse  # noqa: E402

import _bootstrap  # noqa: F401,E402
from agent.config import load_config  # noqa: E402
from agent.sheets_client import SheetsClient  # noqa: E402
from run_portal_sheet import PORTAL_SHEET_ID  # noqa: E402
from genie_core import db  # noqa: E402
from genie_core import categorize as cz  # noqa: E402

TAB = "Verified Orgs"


def _host(u: str) -> str:
    u = (u or "").strip()
    if not u.lower().startswith("http"):
        return ""
    h = (urlparse(u).netloc or "").lower().split(":")[0]
    return h[4:] if h.startswith("www.") else h


def _urls(cell: str) -> list[str]:
    return [u.strip() for u in re.split(r"[\r\n,]+", cell or "") if u.strip().lower().startswith("http")]


def _get(row: dict, *keys: str) -> str:
    for k in keys:
        if k in row and row[k]:
            return str(row[k]).strip()
    return ""


def main() -> None:
    cfg = load_config()
    sc = SheetsClient.from_config(cfg)
    sc.sheet_id = PORTAL_SHEET_ID
    rows = sc._read_tab_as_dicts(TAB)
    print(f"read {len(rows)} rows from {TAB!r}")

    db.init_db()
    n = new_uni = new_portal = 0
    with db.connect() as conn:
        for r in rows:
            orgid = _get(r, "SheerID OrgID", "OrgID", "orgid")
            name = _get(r, "SheerID University Name", "University Name", "Name")
            if not orgid and not name:
                continue
            orgid = orgid or db._name_norm(name)
            login = _get(r, "Reclaim Protocol Login Page Url", "Login Page Url", "Login URLs")
            db.upsert_verified_org(
                conn, orgid=orgid, name=name, login_urls=login,
                provider_ids=_get(r, "Reclaim Protocol Provider ID", "Provider ID"),
                terms_urls=_get(r, "Terms URLs", "Terms"),
                notes=_get(r, "Legal Notes", "Notes"),
            )
            n += 1

            # Sync into the main DB so every live org is mapped by orgid, with its
            # live portals. INSERT OR IGNORE only — never clobbers existing rows,
            # categories, or state/city we already hold.
            urls = _urls(login)
            site_host = _host(urls[0]) if urls else ""
            cur = conn.execute(
                """INSERT OR IGNORE INTO universities
                   (orgid,name,city,state,org_type,website,country,source)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (orgid, name, "", "", "", f"https://{site_host}" if site_host else "",
                 "India", "verified-orgs-sheet"))
            new_uni += cur.rowcount
            for u in urls:
                h = _host(u)
                cat = cz.classify(u, fetch=False)[0]
                cur = conn.execute(
                    """INSERT OR IGNORE INTO portals
                       (orgid,university,domain,portal_url,category,source,country,status)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (orgid, name, h, u, cat, "verified-orgs-sheet", "India", "confirmed"))
                new_portal += cur.rowcount
        conn.commit()
    vidx = db.verified_index()
    print(f"upserted {n} verified orgs  |  index: {len(vidx['orgids'])} orgids, "
          f"{len(vidx['urls'])} live URLs, {len(vidx['hosts'])} hosts")
    print(f"synced into DB: +{new_uni} new universities, +{new_portal} new live portals")


if __name__ == "__main__":
    main()

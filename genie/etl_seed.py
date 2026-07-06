#!/usr/bin/env python3
"""Seed the Genie portals DB (SQLite) from the office sheets.

Portals come from FinalActivationSheet (col B = orgid, col C = login portal).
University names + domains are joined from the SheerID Universities tab (in the
separate SheerID sheet). Category is inferred from the URL. Re-runnable
(upserts).

Usage:  .venv/bin/python genie/etl_seed.py
"""
from __future__ import annotations

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
from _run_dated_tab_portals import _resolve_tab_title  # noqa: E402
from run_portal_sheet import PORTAL_SHEET_ID  # noqa: E402
from genie_core import db  # noqa: E402

PORTAL_TAB = "FinalActivationSheet"   # office sheet: B=orgid, C=portal


def infer_category(url: str) -> str:
    u = url.lower()
    if any(k in u for k in ("erp", "iitms", "mastersofterp", "samarth", "digitaluniversity")):
        return "ERP / Student Portal"
    if "moodle" in u or "/lms" in u or "lms." in u or "elearn" in u:
        return "LMS / Moodle"
    if any(k in u for k in ("opac", "koha", "library", "lib.")):
        return "Library"
    if any(k in u for k in ("exam", "result")):
        return "Exam Portal"
    if any(k in u for k in ("login", "signin", "sign-in", "student", "auth", "portal")):
        return "Student Portal"
    return "Portal"


def host(url: str) -> str:
    h = (urlparse(url).netloc or "").lower().split(":")[0]
    return h[4:] if h.startswith("www.") else h


def main() -> None:
    config = load_config()
    db.init_db()

    # 1) name/domain map from the SheerID Universities tab
    uni_sheet = SheetsClient(sheet_id=config.google_sheet_id, universities_tab="x", portals_tab="x",
                             credentials_path=config.google_credentials_path, token_path=config.google_token_path)
    names: dict[str, tuple[str, str]] = {}
    for r in uni_sheet._get_values(f"'{config.universities_tab}'", "2:100000"):
        oid = str(r[0]).strip() if r else ""
        if not oid:
            continue
        nm = str(r[1]).strip() if len(r) > 1 else ""
        dom = str(r[5]).strip().split(",")[0].strip() if len(r) > 5 else ""
        names[oid] = (nm, dom)
    print(f"loaded {len(names)} university names")

    # 2) portals from the office FinalActivationSheet
    office = SheetsClient(sheet_id=PORTAL_SHEET_ID, universities_tab="x", portals_tab="x",
                          credentials_path=config.google_credentials_path, token_path=config.google_token_path)
    rows = office._get_values(f"'{_resolve_tab_title(office, PORTAL_TAB)}'", "3:100000")

    seen = set()
    n = 0
    with db.connect() as conn:
        for r in rows:
            oid = str(r[1]).strip() if len(r) > 1 else ""
            portal = str(r[2]).strip() if len(r) > 2 else ""
            if not oid or not portal.lower().startswith("http"):
                continue
            key = (oid, portal)
            if key in seen:
                continue
            seen.add(key)
            nm, dom = names.get(oid, ("", host(portal)))
            db.upsert_portal(conn, orgid=oid, university=nm, domain=dom or host(portal),
                             portal_url=portal, category=infer_category(portal), source="db")
            n += 1
        conn.commit()

    st = db.stats()
    print(f"seeded {n} portal rows -> {st['portals']} portals across {st['universities']} orgs")


if __name__ == "__main__":
    main()

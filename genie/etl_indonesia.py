#!/usr/bin/env python3
"""Ingest Indonesia universities into the Genie DB.

Sheet 1_V2-9XVua309pXmVUTpw-k5uPDT1bU2cTlRGk5uWzSs, tab 'Indonesia' (no header):
  Col A = University Name, Col B = ~enrollment (ignored), Col C = Login portal URL.

There's no website column, so we DERIVE the website from the portal's own
domain when the portal is on the university's domain (e.g. pintoe.utu.ac.id →
utu.ac.id); vendor-hosted portals (siakadcloud, icloudems, …) leave the website
blank so it can be added manually in the UI.

Usage:  .venv/bin/python genie/etl_indonesia.py
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
TAB = "Indonesia"
COUNTRY = "Indonesia"

_MULTI_TLDS = ("ac.id", "edu.id", "sch.id", "co.id", "or.id", "go.id", "net.id", "web.id", "edu")
# vendor portal hosts that are NOT the university's own site
_VENDOR = ("siakadcloud.com", "icloudems.com", "samarth.edu.in", "digitaluniversity.ac",
           "moodlecloud.com", "neostats.com", "sevima.com")


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:70] or "x"


def host(url: str) -> str:
    if not url or not url.lower().startswith("http"):
        return ""
    h = (urlparse(url).netloc or "").lower().split(":")[0]
    return h[4:] if h.startswith("www.") else h


def registrable(h: str) -> str:
    for suf in _MULTI_TLDS:
        if h.endswith("." + suf):
            labels = h[: -(len(suf) + 1)].split(".")
            return (labels[-1] + "." + suf) if labels else h
    parts = h.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else h


def derive_website(portal_url: str) -> str:
    h = host(portal_url)
    if not h:
        return ""
    reg = registrable(h)
    if any(reg == v or reg.endswith("." + v) for v in _VENDOR):
        return ""
    return f"https://{reg}"


def main() -> None:
    cfg = load_config()
    sc = SheetsClient.from_config(cfg)
    sc.sheet_id = SHEET_ID
    values = sc._get_values(TAB, "1:100000")

    db.init_db()
    n_uni = n_portal = n_web = 0
    with db.connect() as conn:
        for row in values:
            if not row or not str(row[0]).strip():
                continue
            name = str(row[0]).strip()
            if name.lower() in ("university name", "name", "university", "college name"):
                continue
            # portal = first cell that looks like a URL
            portal = next((str(c).strip() for c in row[1:] if str(c).strip().lower().startswith("http")), "")
            website = derive_website(portal) if portal else ""
            if website:
                n_web += 1
            orgid = f"id:{slug(name)}"
            db.upsert_university(
                conn, orgid=orgid, name=name, city="", state="", org_type="",
                website=website, country=COUNTRY, source="indonesia-sheet")
            n_uni += 1
            if portal:
                cat = cz.classify(portal, fetch=False)[0]
                db.upsert_portal(
                    conn, orgid=orgid, university=name, domain=host(portal), portal_url=portal,
                    category=cat, source="indonesia-sheet", country=COUNTRY, status="confirmed")
                n_portal += 1
        conn.commit()
    print(f"✓ {COUNTRY}: {n_uni} universities, {n_portal} portals, {n_web} websites derived from portal domains")
    with db.connect() as c:
        nw = c.execute("SELECT COUNT(*) FROM universities WHERE country=? AND (website='' OR website IS NULL)", (COUNTRY,)).fetchone()[0]
    print(f"  {nw} still without a website (addable manually in the UI)")


if __name__ == "__main__":
    main()

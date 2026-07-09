#!/usr/bin/env python3
"""Load portals discovered by Magic (written to "<Country> Portals" sheet tabs)
into the Genie DB so they appear in the UI (Browse / Search / Insights).

Reads tabs with columns: University Name | Website | Portal URL | Category.
One `universities` row per school (orgid = genie:<website-domain>) + one
`portals` row per portal (INSERT OR IGNORE, so re-running is safe).

Targets whatever GENIE_DB_URL points at — set it to the Neon URL for prod.

Usage:
  .venv/bin/python scripts/etl_magic_sheet.py --tabs "Brazil Portals,Mexico Portals"
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "genie" / "core"))
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except Exception:  # noqa: BLE001
    pass

import _bootstrap  # noqa: F401,E402
from agent.config import load_config  # noqa: E402
from agent.sheets_client import SheetsClient  # noqa: E402
from genie_core import db  # noqa: E402

DEFAULT_SHEET = "1_V2-9XVua309pXmVUTpw-k5uPDT1bU2cTlRGk5uWzSs"


def _host(u: str) -> str:
    u = (u or "").strip()
    if "://" not in u:
        u = "http://" + u
    h = (urlparse(u).netloc or "").lower().split(":")[0]
    return h[4:] if h.startswith("www.") else h


def _country_of(tab: str) -> str:
    return tab.replace("Portals", "").strip() or "Global"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tabs", required=True, help="comma-separated portal tabs")
    ap.add_argument("--sheet", default=DEFAULT_SHEET)
    args = ap.parse_args()

    cfg = load_config()
    sc = SheetsClient.from_config(cfg); sc.sheet_id = args.sheet

    unis: dict[str, tuple] = {}
    portals: list[tuple] = []
    for tab in [t.strip() for t in args.tabs.split(",") if t.strip()]:
        country = _country_of(tab)
        rows = sc._get_values(tab, "A2:D5000")
        n = 0
        for r in rows:
            name = (r[0] if r else "").strip()
            website = (r[1] if len(r) > 1 else "").strip()
            url = (r[2] if len(r) > 2 else "").strip()
            cat = (r[3] if len(r) > 3 else "").strip()
            if not name or not url or url == "(none found)":
                continue
            dom = _host(website) or _host(url)
            orgid = f"genie:{dom}"
            unis[orgid] = (orgid, name, "", "", "", f"https://{dom}", country, "magic-sheet")
            portals.append((orgid, name, _host(url), url, cat, "magic", country))
            n += 1
        print(f"{tab}: {n} portal rows ({country})")

    db.init_db()
    with db.connect() as conn:
        conn.executemany(
            """INSERT OR IGNORE INTO universities
               (orgid,name,city,state,org_type,website,country,source)
               VALUES (?,?,?,?,?,?,?,?)""", list(unis.values()))
        conn.executemany(
            """INSERT OR IGNORE INTO portals
               (orgid,university,domain,portal_url,category,source,country)
               VALUES (?,?,?,?,?,?,?)""", portals)
        conn.commit()
    print(f"loaded: {len(unis)} universities, {len(portals)} portal rows into "
          f"{'Postgres/Neon' if db.is_postgres() else 'SQLite'}")


if __name__ == "__main__":
    main()

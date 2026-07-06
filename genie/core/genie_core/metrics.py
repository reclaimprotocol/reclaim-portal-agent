"""Per-URL popularity metric — OpenPageRank (domain authority + rank). Cached in
the Genie DB so we don't re-hit the API on every page view.

Key comes from the repo-root .env: OPR_API_KEY.
(Cloudflare Radar was dropped — its bucket was effectively constant across
universities, so it carried no signal.)
"""
from __future__ import annotations

import concurrent.futures
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests

try:  # load OPR_API_KEY / CLOUDFLARE_API_TOKEN from repo-root .env
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[3] / ".env")
except Exception:
    pass

from . import db

OPR_ENDPOINT = "https://openpagerank.keywordseverywhere.com/v1/domains/bulk"
_MULTI_TLDS = ("ac.in", "edu.in", "co.in", "gov.in", "org.in", "net.in", "nic.in",
               "res.in", "ac.bd", "edu.bd", "gov.bd", "ac.uk")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS metrics (
    host          TEXT PRIMARY KEY,
    opr_domain    TEXT,
    opr_authority REAL,
    opr_rank      INTEGER,
    cf_bucket     TEXT,
    cf_rank       INTEGER,
    fetched_at    TEXT
);
"""


def _ensure() -> None:
    with db.connect() as c:
        c.executescript(_SCHEMA)


def _host(url: str) -> str:
    u = url.strip()
    if "://" not in u:
        u = "http://" + u
    h = (urlparse(u).netloc or "").lower().split(":")[0]
    return h[4:] if h.startswith("www.") else h


def _registrable(host: str) -> str:
    for suf in _MULTI_TLDS:
        if host.endswith("." + suf):
            labels = host[: -(len(suf) + 1)].split(".")
            return (labels[-1] + "." + suf) if labels else host
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def _fetch_opr(domain: str) -> dict:
    key = os.getenv("OPR_API_KEY")
    if not key:
        return {}
    try:
        r = requests.post(OPR_ENDPOINT,
                          headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                          json={"domains": [domain]}, timeout=20)
        it = (r.json().get("results") or [{}])[0]
        return {"authority": it.get("open_page_rank"), "rank": it.get("rank")}
    except Exception:
        return {}


def _row_to_metrics(host: str, row) -> dict:
    return {"host": host, "opr_authority": row["opr_authority"], "opr_rank": row["opr_rank"],
            "fetched_at": row["fetched_at"], "cached": True}


def get_metrics(url: str, refresh: bool = False) -> dict:
    """OpenPageRank for one URL. Cached in DB (keyed by host)."""
    _ensure()
    host = _host(url)
    if not host:
        return {"host": "", "error": "bad url"}
    if not refresh:
        with db.connect() as c:
            row = c.execute("SELECT * FROM metrics WHERE host=?", (host,)).fetchone()
        if row:
            return _row_to_metrics(host, row)
    reg = _registrable(host)
    opr = _fetch_opr(reg)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with db.connect() as c:
        c.execute(
            """INSERT INTO metrics (host, opr_domain, opr_authority, opr_rank, fetched_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT (host) DO UPDATE SET opr_domain=excluded.opr_domain,
                 opr_authority=excluded.opr_authority, opr_rank=excluded.opr_rank,
                 fetched_at=excluded.fetched_at""",
            (host, reg, opr.get("authority"), opr.get("rank"), now),
        )
        c.commit()
    return {"host": host, "opr_domain": reg, "opr_authority": opr.get("authority"),
            "opr_rank": opr.get("rank"), "fetched_at": now, "cached": False}


def get_metrics_batch(urls: list[str], refresh: bool = False, workers: int = 8) -> list[dict]:
    """Metrics for many URLs concurrently (cache-first)."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        return list(ex.map(lambda u: get_metrics(u, refresh=refresh), urls))

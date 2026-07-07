"""Portals repository. Runs on SQLite (local dev, default) OR Postgres (set
GENIE_DB_URL=postgresql://…). All SQL lives here behind a tiny connection
wrapper that normalizes placeholders + row access across both backends, so the
search / API / training code above never changes."""
from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path

from .models import Portal, UniversityPortals

try:  # honor GENIE_DB_URL / GENIE_DB_PATH from the repo-root .env everywhere
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[3] / ".env")
except Exception:
    pass

DB_PATH = os.getenv("GENIE_DB_PATH") or str(Path(__file__).resolve().parents[2] / "genie.db")
DB_URL = os.getenv("GENIE_DB_URL", "").strip()
_IS_PG = DB_URL.startswith("postgres")

# Countries Genie supports (shown in the UI even when a list isn't loaded yet).
COUNTRIES = ["India", "Bangladesh", "Indonesia"]
DEFAULT_COUNTRY = "India"

_AUTO_PK = "SERIAL PRIMARY KEY" if _IS_PG else "INTEGER PRIMARY KEY AUTOINCREMENT"


def is_postgres() -> bool:
    return _IS_PG


class _DualRow:
    """A row usable both by index (r[0]) and by column name (r['x']), and
    convertible via dict(r) — matching sqlite3.Row semantics for Postgres."""
    __slots__ = ("_cols", "_vals")

    def __init__(self, cols, vals):
        self._cols = cols
        self._vals = vals

    def __getitem__(self, k):
        if isinstance(k, int):
            return self._vals[k]
        return self._vals[self._cols.index(k)]

    def keys(self):
        return list(self._cols)

    def __iter__(self):
        return iter(self._vals)

    def __len__(self):
        return len(self._vals)


def _pg_row_factory(cursor):
    cols = [d.name for d in (cursor.description or [])]
    def make(values):
        return _DualRow(cols, values)
    return make


def _translate(sql: str) -> str:
    """SQLite SQL → Postgres: '?'→'%s' and 'INSERT OR IGNORE'→ON CONFLICT DO NOTHING."""
    ioc = "INSERT OR IGNORE" in sql
    s = sql.replace("INSERT OR IGNORE", "INSERT") if ioc else sql
    s = s.replace("?", "%s")
    if ioc:
        s = s.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
    return s


class _Conn:
    """Thin wrapper giving both backends the same .execute/.executemany/.commit
    surface and a context manager that commits (or rolls back) then closes."""
    def __init__(self, raw, pg: bool):
        self.raw = raw
        self.pg = pg

    def execute(self, sql: str, params=()):
        cur = self.raw.cursor()
        cur.execute(_translate(sql) if self.pg else sql, params)
        return cur

    def executemany(self, sql: str, seq):
        cur = self.raw.cursor()
        cur.executemany(_translate(sql) if self.pg else sql, list(seq))
        return cur

    def executescript(self, script: str):
        if self.pg:
            for stmt in filter(str.strip, script.split(";")):
                self.raw.cursor().execute(_translate(stmt))
        else:
            self.raw.executescript(script)

    def commit(self):
        self.raw.commit()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *_):
        try:
            if exc_type is None:
                self.raw.commit()
            else:
                self.raw.rollback()
        finally:
            self.raw.close()


def connect(path: str | None = None) -> _Conn:
    if _IS_PG:
        import psycopg  # lazy: only needed when GENIE_DB_URL is set
        raw = psycopg.connect(DB_URL, row_factory=_pg_row_factory)
        return _Conn(raw, True)
    raw = sqlite3.connect(path or DB_PATH)
    raw.row_factory = sqlite3.Row
    return _Conn(raw, False)


def _columns(c, table: str) -> set[str]:
    if _IS_PG:
        rows = c.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name=?", (table,)).fetchall()
        return {r[0] for r in rows}
    return {r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}


def init_db(path: str | None = None) -> None:
    with connect(path) as c:
        c.execute(f"""CREATE TABLE IF NOT EXISTS portals (
                       id {_AUTO_PK},
                       orgid TEXT, university TEXT, domain TEXT, portal_url TEXT NOT NULL,
                       category TEXT DEFAULT '', source TEXT DEFAULT '',
                       affiliated_from TEXT DEFAULT '', country TEXT DEFAULT 'India',
                       UNIQUE(orgid, portal_url) )""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_portals_university ON portals(university)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_portals_domain ON portals(domain)")
        c.execute("""CREATE TABLE IF NOT EXISTS universities (
                       orgid TEXT PRIMARY KEY, name TEXT, city TEXT, state TEXT,
                       org_type TEXT, website TEXT, country TEXT DEFAULT 'India', source TEXT )""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_uni_state ON universities(state)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_uni_country ON universities(country)")
        # migrate portals table: country, state, city, org_type, status
        cols = _columns(c, "portals")
        adds = {"country": "TEXT DEFAULT 'India'", "state": "TEXT DEFAULT ''",
                "city": "TEXT DEFAULT ''", "org_type": "TEXT DEFAULT ''",
                "status": "TEXT DEFAULT 'confirmed'"}
        for col, decl in adds.items():
            if col not in cols:
                c.execute(f"ALTER TABLE portals ADD COLUMN {col} {decl}")
        c.execute("CREATE INDEX IF NOT EXISTS idx_portals_country ON portals(country)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_portals_state ON portals(state)")
        c.execute("UPDATE portals SET country='India' WHERE country IS NULL OR country=''")
        c.execute(f"""CREATE TABLE IF NOT EXISTS feedback (
                       id {_AUTO_PK},
                       orgid TEXT, url TEXT, verdict TEXT, reason TEXT, created_at TEXT )""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_feedback_orgid ON feedback(orgid)")
        # migrate feedback: capture features for rule mining (Level 1/2 training)
        fcols = _columns(c, "feedback")
        for col in ("category", "source", "host", "reasoning"):
            if col not in fcols:
                c.execute(f"ALTER TABLE feedback ADD COLUMN {col} TEXT DEFAULT ''")
        # learned global rules mined from feedback (human-gated, reversible)
        c.execute(f"""CREATE TABLE IF NOT EXISTS learned_rules (
                       id {_AUTO_PK},
                       rule_type TEXT, pattern TEXT, action TEXT DEFAULT 'flag',
                       status TEXT DEFAULT 'proposed', support INTEGER DEFAULT 0,
                       orgs INTEGER DEFAULT 0, confirms INTEGER DEFAULT 0,
                       examples TEXT DEFAULT '', created_at TEXT, updated_at TEXT,
                       UNIQUE(rule_type, pattern) )""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_rules_status ON learned_rules(status)")
        # Verified Orgs — universities whose portals are live in production.
        c.execute("""CREATE TABLE IF NOT EXISTS verified_orgs (
                       orgid TEXT PRIMARY KEY, name TEXT, name_norm TEXT,
                       login_urls TEXT, provider_ids TEXT, terms_urls TEXT,
                       notes TEXT, source TEXT )""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_verified_namenorm ON verified_orgs(name_norm)")
        c.commit()


def backfill_universities_from_portals(path: str | None = None) -> int:
    """Ensure every org that has portals also exists in the universities table
    (so the university-centric view + no-portal Discover buttons are complete)."""
    with connect(path) as c:
        rows = c.execute(
            """SELECT orgid, MAX(university), MAX(domain), MAX(country), MAX(state), MAX(city), MAX(org_type)
               FROM portals GROUP BY orgid""").fetchall()
        n = 0
        for orgid, name, domain, country, state, city, org_type in rows:
            if not orgid:
                continue
            exists = c.execute("SELECT 1 FROM universities WHERE orgid=?", (orgid,)).fetchone()
            if exists:
                continue
            website = f"https://{domain}" if domain else ""
            c.execute("""INSERT OR IGNORE INTO universities
                         (orgid,name,city,state,org_type,website,country,source)
                         VALUES (?,?,?,?,?,?,?,?)""",
                      (orgid, name or "", city or "", state or "", org_type or "",
                       website, country or "India", "portals-backfill"))
            n += 1
        c.commit()
    return n


def upsert_university(conn, *, orgid, name, city="", state="", org_type="",
                      website="", country="India", source="") -> None:
    conn.execute(
        """INSERT INTO universities (orgid, name, city, state, org_type, website, country, source)
           VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT (orgid) DO UPDATE SET name=excluded.name, city=excluded.city,
             state=excluded.state, org_type=excluded.org_type, website=excluded.website,
             country=excluded.country, source=excluded.source""",
        (orgid, name, city, state, org_type, website, country, source),
    )


def states(country: str = "India", path: str | None = None) -> list[dict]:
    """Distinct states/districts (with university counts) for a country — based on
    the universities table so it's complete even where few portals exist."""
    with connect(path) as c:
        rows = c.execute(
            """SELECT state, COUNT(*) FROM universities
               WHERE country=? AND state IS NOT NULL AND state<>'' GROUP BY state ORDER BY state""",
            (country,)).fetchall()
    return [{"state": r[0], "count": r[1]} for r in rows]


def upsert_portal(conn: sqlite3.Connection, *, orgid: str, university: str, domain: str,
                  portal_url: str, category: str = "", source: str = "",
                  affiliated_from: str = "", country: str = DEFAULT_COUNTRY,
                  state: str = "", city: str = "", org_type: str = "",
                  status: str = "confirmed") -> None:
    conn.execute(
        """INSERT INTO portals (orgid, university, domain, portal_url, category, source, affiliated_from, country, state, city, org_type, status)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT (orgid, portal_url) DO UPDATE SET
             university=excluded.university, domain=excluded.domain,
             category=excluded.category, source=excluded.source,
             affiliated_from=excluded.affiliated_from, country=excluded.country,
             state=excluded.state, city=excluded.city, org_type=excluded.org_type,
             status=excluded.status""",
        (orgid, university, domain, portal_url, category, source, affiliated_from,
         country, state, city, org_type, status),
    )


def search(query: str, limit: int = 20, country: str | None = None,
           state: str | None = None, path: str | None = None) -> list[UniversityPortals]:
    """Fuzzy match on university name, domain, or portal URL. Groups portals by
    (orgid, university, domain). Optional country + state filter."""
    q = f"%{query.strip().lower()}%"
    params = [q, q, q]
    cclause = ""
    if country:
        cclause += " AND country = ?"
        params.append(country)
    if state:
        cclause += " AND state = ?"
        params.append(state)
    with connect(path) as c:
        rows = c.execute(
            f"""SELECT orgid, university, domain, portal_url, category, source, affiliated_from
               FROM portals
               WHERE (lower(university) LIKE ? OR lower(domain) LIKE ? OR lower(portal_url) LIKE ?){cclause}
               ORDER BY university, domain""",
            params,
        ).fetchall()
    grouped: dict[tuple, UniversityPortals] = {}
    for r in rows:
        key = (r["orgid"], r["university"], r["domain"])
        up = grouped.get(key)
        if up is None:
            up = UniversityPortals(orgid=r["orgid"] or "", university=r["university"] or "",
                                   domain=r["domain"] or "")
            grouped[key] = up
        up.portals.append(Portal(url=r["portal_url"], category=r["category"] or "",
                                 source=r["source"] or "", affiliated_from=r["affiliated_from"] or ""))
        if len(grouped) >= limit and key not in grouped:
            break
    results = list(grouped.values())[:limit]
    # tag verified universities (live in production) + their live portals
    vidx = verified_index(path)
    if vidx["orgids"] or vidx["names"] or vidx["urls"]:
        for up in results:
            up.verified = is_org_verified(vidx, orgid=up.orgid, name=up.university,
                                          portal_urls=[p.url for p in up.portals])
            for p in up.portals:
                p.verified = is_url_live(vidx, p.url)
    return results


def list_portals(offset: int = 0, limit: int = 50, category: str | None = None,
                 q: str | None = None, country: str | None = None,
                 state: str | None = None, path: str | None = None) -> dict:
    """Paginated flat listing of the portals table, for a browse view.
    Optional `country`, `state`, `category` and free-text `q` filters."""
    where, params = [], []
    if country:
        where.append("country = ?")
        params.append(country)
    if state:
        where.append("state = ?")
        params.append(state)
    if category:
        where.append("category = ?")
        params.append(category)
    if q and q.strip():
        like = f"%{q.strip().lower()}%"
        where.append("(lower(university) LIKE ? OR lower(domain) LIKE ? OR lower(portal_url) LIKE ?)")
        params += [like, like, like]
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    with connect(path) as c:
        total = c.execute(f"SELECT COUNT(*) FROM portals {clause}", params).fetchone()[0]
        rows = c.execute(
            f"""SELECT id, orgid, university, domain, portal_url, category, source,
                       affiliated_from, state, city, org_type, status
                FROM portals {clause} ORDER BY university, domain
                LIMIT ? OFFSET ?""",
            (*params, max(1, min(limit, 500)), max(0, offset)),
        ).fetchall()
    vidx = verified_index(path)
    out = []
    for r in rows:
        d = dict(r)
        d["portal_verified"] = is_url_live(vidx, d.get("portal_url", ""))
        d["verified"] = is_org_verified(vidx, orgid=d.get("orgid", ""),
                                        name=d.get("university", ""), portal_urls=[d.get("portal_url", "")])
        out.append(d)
    return {"total": total, "offset": offset, "limit": limit, "portals": out}


def update_university_website(orgid: str, website: str, path: str | None = None) -> None:
    with connect(path) as c:
        c.execute("UPDATE universities SET website=? WHERE orgid=?", (website, orgid))
        c.commit()


def update_portal_category(portal_id: int, category: str, path: str | None = None) -> None:
    with connect(path) as c:
        c.execute("UPDATE portals SET category=? WHERE id=?", (category, portal_id))
        c.commit()


def categories(path: str | None = None) -> list[str]:
    with connect(path) as c:
        return [r[0] for r in c.execute(
            "SELECT DISTINCT category FROM portals WHERE category<>'' ORDER BY category")]


def country_counts(path: str | None = None) -> list[dict]:
    """Per supported country: university count (primary), plus portal count and
    universities-covered. `count` = universities so the UI matches Insights."""
    with connect(path) as c:
        unis = dict(c.execute("SELECT country, COUNT(*) FROM universities GROUP BY country").fetchall())
        portals = dict(c.execute("SELECT country, COUNT(*) FROM portals GROUP BY country").fetchall())
        covered = dict(c.execute("SELECT country, COUNT(DISTINCT orgid) FROM portals GROUP BY country").fetchall())
    ordered = COUNTRIES + [k for k in (unis | portals) if k and k not in COUNTRIES]
    return [{"country": k, "count": unis.get(k, 0), "universities": unis.get(k, 0),
             "portals": portals.get(k, 0), "universities_covered": covered.get(k, 0)}
            for k in ordered]


def list_universities(offset: int = 0, limit: int = 40, country: str | None = None,
                      state: str | None = None, q: str | None = None,
                      only_missing: bool = False, path: str | None = None) -> dict:
    """University-centric paginated listing (LEFT JOIN portals) — includes
    universities that have zero portals, so the UI can offer a Discover button.
    Each item carries its portals (with status). `only_missing` = no-portal only."""
    where, params = [], []
    if country:
        where.append("u.country = ?")
        params.append(country)
    if state:
        where.append("u.state = ?")
        params.append(state)
    if q and q.strip():
        like = f"%{q.strip().lower()}%"
        where.append("(lower(u.name) LIKE ? OR lower(u.website) LIKE ?)")
        params += [like, like]
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    # NB: Postgres can't reference the SELECT alias `pcount` in HAVING (SQLite
    # can); use the aggregate expression so both backends work.
    having = "HAVING COUNT(p.id) = 0" if only_missing else ""
    with connect(path) as c:
        base = f"""FROM universities u
                   LEFT JOIN portals p ON p.orgid = u.orgid
                   {clause}
                   GROUP BY u.orgid {having}"""
        total = c.execute(f"SELECT COUNT(*) FROM (SELECT u.orgid, COUNT(p.id) pcount {base})", params).fetchone()[0]
        rows = c.execute(
            f"""SELECT u.orgid, u.name, u.city, u.state, u.org_type, u.website, u.country,
                       COUNT(p.id) AS pcount
                {base}
                ORDER BY pcount ASC, u.name ASC
                LIMIT ? OFFSET ?""",
            (*params, max(1, min(limit, 200)), max(0, offset)),
        ).fetchall()
        orgids = [r["orgid"] for r in rows]
        pmap: dict[str, list] = {o: [] for o in orgids}
        if orgids:
            ph = ",".join("?" * len(orgids))
            prows = c.execute(
                f"""SELECT id, orgid, portal_url, category, source, status
                    FROM portals WHERE orgid IN ({ph}) ORDER BY category, portal_url""",
                orgids).fetchall()
            for pr in prows:
                pmap[pr["orgid"]].append({
                    "id": pr["id"], "url": pr["portal_url"], "category": pr["category"] or "",
                    "source": pr["source"] or "", "status": pr["status"] or "confirmed"})
    vidx = verified_index(path)
    items = []
    for r in rows:
        ports = pmap.get(r["orgid"], [])
        for p in ports:
            p["verified"] = is_url_live(vidx, p["url"])
        items.append({"orgid": r["orgid"], "name": r["name"], "city": r["city"] or "",
                      "state": r["state"] or "", "org_type": r["org_type"] or "",
                      "website": r["website"] or "", "country": r["country"] or "India",
                      "verified": is_org_verified(vidx, orgid=r["orgid"], name=r["name"],
                                                  portal_urls=[p["url"] for p in ports]),
                      "portals": ports})
    return {"total": total, "offset": offset, "limit": limit, "universities": items}


def _host(u: str) -> str:
    """Normalize a URL/domain to a bare host (mirrors discover.host_of)."""
    from urllib.parse import urlparse
    u = (u or "").strip()
    if not u:
        return ""
    if "://" not in u:
        u = "http://" + u
    h = (urlparse(u).netloc or "").lower().split(":")[0]
    return h[4:] if h.startswith("www.") else h


def lookup_by_domain(url_or_domain: str, path: str | None = None) -> dict:
    """DB-first check for Discover live: return the university + portals we
    already have for this site (matched on host or the genie:{host} org id)."""
    d = _host(url_or_domain)
    if not d:
        return {"found": False, "domain": "", "portals": []}
    with connect(path) as c:
        rows = c.execute(
            """SELECT id, orgid, university, domain, portal_url, category, source, status
               FROM portals WHERE domain=? OR domain=? OR orgid=?
               ORDER BY category, portal_url""",
            (d, f"www.{d}", f"genie:{d}")).fetchall()
    if not rows:
        return {"found": False, "domain": d, "portals": []}
    vidx = verified_index(path)
    orgid = rows[0]["orgid"]; uni = rows[0]["university"] or ""
    portals = []
    for r in rows:
        portals.append({"id": r["id"], "url": r["portal_url"], "category": r["category"] or "",
                        "source": r["source"] or "", "status": r["status"] or "confirmed",
                        "verified": is_url_live(vidx, r["portal_url"])})
    return {"found": True, "orgid": orgid, "university": uni, "domain": d,
            "verified": is_org_verified(vidx, orgid=orgid),
            "portals": portals}


def export_rows(country: str | None = None, state: str | None = None,
                category: str | None = None, path: str | None = None) -> list[dict]:
    """Flat portal rows for CSV export, with optional country/state/category filter."""
    where, params = [], []
    if country:
        where.append("country = ?"); params.append(country)
    if state:
        where.append("state = ?"); params.append(state)
    if category:
        where.append("category = ?"); params.append(category)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    with connect(path) as c:
        rows = c.execute(
            f"""SELECT university, domain, category, portal_url, country, state, city,
                       org_type, status, source
                FROM portals {clause} ORDER BY country, state, university, category""",
            params).fetchall()
    return [dict(r) for r in rows]


def get_university(orgid: str, path: str | None = None) -> dict | None:
    """Full profile for one university: details + all portals (with verified/live
    status) + verified status + a domain to fetch a logo from."""
    with connect(path) as c:
        u = c.execute(
            "SELECT orgid,name,city,state,org_type,website,country FROM universities WHERE orgid=?",
            (orgid,)).fetchone()
        prows = c.execute(
            """SELECT id, portal_url, category, source, status, domain
               FROM portals WHERE orgid=? ORDER BY category, portal_url""",
            (orgid,)).fetchall()
    if not u and not prows:
        return None
    if u:
        name, city, state = u["name"], u["city"], u["state"]
        org_type, website, country = u["org_type"], u["website"], u["country"]
    else:
        with connect(path) as c:
            agg = c.execute(
                """SELECT MAX(university), MAX(domain), MAX(country), MAX(state), MAX(city), MAX(org_type)
                   FROM portals WHERE orgid=?""", (orgid,)).fetchone()
        name, dom0, country, state, city, org_type = agg
        website = f"https://{dom0}" if dom0 else ""
    vidx = verified_index(path)
    portals = []
    for r in prows:
        portals.append({"id": r["id"], "url": r["portal_url"], "category": r["category"] or "",
                        "source": r["source"] or "", "status": r["status"] or "confirmed",
                        "verified": is_url_live(vidx, r["portal_url"])})
    domain = _host(website) or (prows[0]["domain"] if prows else "")
    return {"orgid": orgid, "name": name or "", "city": city or "", "state": state or "",
            "org_type": org_type or "", "website": website or "", "country": country or "India",
            "domain": domain,
            "verified": is_org_verified(vidx, orgid=orgid),
            "portals": portals}


def get_disputed(orgid: str, path: str | None = None) -> set[str]:
    """Normalized URLs a human marked wrong for this org — suppressed in discovery."""
    with connect(path) as c:
        rows = c.execute(
            "SELECT url FROM feedback WHERE orgid=? AND verdict='wrong'", (orgid,)).fetchall()
    return {_norm_url(r[0]) for r in rows if r[0]}


def _norm_url(u: str) -> str:
    return (u or "").strip().lower().rstrip("/").replace("https://", "").replace("http://", "").lstrip("www.")


def _name_norm(s: str) -> str:
    import re as _re
    return _re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def upsert_verified_org(conn, *, orgid, name, login_urls="", provider_ids="",
                        terms_urls="", notes="", source="verified-orgs-sheet") -> None:
    conn.execute(
        """INSERT INTO verified_orgs (orgid, name, name_norm, login_urls, provider_ids, terms_urls, notes, source)
           VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT (orgid) DO UPDATE SET name=excluded.name, name_norm=excluded.name_norm,
             login_urls=excluded.login_urls, provider_ids=excluded.provider_ids,
             terms_urls=excluded.terms_urls, notes=excluded.notes, source=excluded.source""",
        (str(orgid), name, _name_norm(name), login_urls, provider_ids, terms_urls, notes, source),
    )


def verified_index(path: str | None = None) -> dict:
    """Sets for O(1) verified-status checks: orgids, normalized names, and the
    exact live portal URLs / hosts pulled from the Verified Orgs sheet."""
    with connect(path) as c:
        rows = c.execute("SELECT orgid, name_norm, login_urls FROM verified_orgs").fetchall()
    orgids, names, urls, hosts = set(), set(), set(), set()
    for r in rows:
        if r["orgid"]:
            orgids.add(str(r["orgid"]))
        if r["name_norm"]:
            names.add(r["name_norm"])
        for line in re.split(r"[\r\n,]+", r["login_urls"] or ""):
            nu = _norm_url(line)
            if nu:
                urls.add(nu)
                hosts.add(nu.split("/")[0])
    return {"orgids": orgids, "names": names, "urls": urls, "hosts": hosts}


def is_org_verified(vidx: dict, *, orgid: str = "", name: str = "",
                    portal_urls: list[str] | None = None) -> bool:
    """A university is 'live/verified' iff its SheerID orgid is one of the 520
    Verified Orgs. Orgid is the authoritative key — every verified org is synced
    into the DB by orgid (etl_verified) so this is consistent everywhere; name /
    host matching is intentionally NOT used (it double-counted duplicate rows).
    `name`/`portal_urls` are accepted for backward-compat but ignored."""
    return bool(orgid) and str(orgid) in vidx["orgids"]


def is_url_live(vidx: dict, url: str) -> bool:
    """A portal is 'live in production' iff its exact URL is a Verified Orgs
    login URL (host-only matching removed — a different path on the same host
    isn't necessarily the live login page)."""
    return _norm_url(url) in vidx["urls"]


def confirm_portal(*, orgid: str, url: str, university: str = "", category: str = "",
                   country: str = "India", state: str = "", city: str = "",
                   org_type: str = "", domain: str = "", created_at: str = "",
                   path: str | None = None) -> None:
    """Human-approve a portal: persist it as confirmed + log positive feedback.
    If the org has no university row yet (e.g. a Discover-live paste-a-URL run),
    create one so the confirmed portal shows up in Search/Browse/Curate."""
    with connect(path) as c:
        u = c.execute("SELECT name, website, state, city, org_type, country FROM universities WHERE orgid=?",
                      (orgid,)).fetchone()
        name = university or (u["name"] if u else "")
        st = state or (u["state"] if u else "")
        ci = city or (u["city"] if u else "")
        ot = org_type or (u["org_type"] if u else "")
        co = country or (u["country"] if u else "India")
        dom = _norm_url(domain) if domain else (_norm_url(u["website"]) if u and u["website"] else _norm_url(url))
        if u is None:
            upsert_university(c, orgid=orgid, name=name, city=ci, state=st, org_type=ot,
                              website=(f"https://{dom}" if dom else url), country=co,
                              source="discover-live")
        upsert_portal(c, orgid=orgid, university=name, domain=dom, portal_url=url,
                      category=category, source="human-confirmed", country=co,
                      state=st, city=ci, org_type=ot, status="confirmed")
        c.execute("INSERT INTO feedback (orgid,url,verdict,reason,category,source,host,reasoning,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                  (orgid, url, "confirmed", "", category, "", _host(url), "", created_at))
        c.commit()


def dispute_portal(*, orgid: str, url: str, reason: str = "", category: str = "",
                   source: str = "", reasoning: str = "", created_at: str = "",
                   path: str | None = None) -> None:
    """Human-reject a portal: log negative feedback (suppresses it in future
    discovery for this org) and remove it from the portals table if present.
    `category`/`source`/`reasoning` are captured as features for rule mining and
    for the human/LLM review loop; `reason` is the (optional) human comment."""
    with connect(path) as c:
        c.execute("INSERT INTO feedback (orgid,url,verdict,reason,category,source,host,reasoning,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                  (orgid, url, "wrong", reason, category, source, _host(url), reasoning, created_at))
        c.execute("DELETE FROM portals WHERE orgid=? AND portal_url=?", (orgid, url))
        c.commit()


def list_disputes(offset: int = 0, limit: int = 50, q: str | None = None,
                  path: str | None = None) -> dict:
    """The dispute review log for the Training tab: each wrong portal with the
    agent's saved reasoning, the university name, and the human comment. Adds a
    `covered` flag = whether an active learned rule already suppresses it."""
    where = ["f.verdict='wrong'"]
    params: list = []
    if q and q.strip():
        like = f"%{q.strip().lower()}%"
        where.append("(lower(f.url) LIKE ? OR lower(u.name) LIKE ? OR lower(f.reason) LIKE ?)")
        params += [like, like, like]
    clause = "WHERE " + " AND ".join(where)
    with connect(path) as c:
        total = c.execute(
            f"SELECT COUNT(*) FROM feedback f LEFT JOIN universities u ON u.orgid=f.orgid {clause}",
            params).fetchone()[0]
        rows = c.execute(
            f"""SELECT f.id, f.orgid, f.url, f.reason, f.category, f.source, f.reasoning,
                       f.host, f.created_at, u.name AS university, u.website AS website
                FROM feedback f LEFT JOIN universities u ON u.orgid=f.orgid
                {clause} ORDER BY f.id DESC LIMIT ? OFFSET ?""",
            (*params, max(1, min(limit, 200)), max(0, offset))).fetchall()
    from . import training
    active = active_rules(path)
    items = []
    for r in rows:
        d = dict(r)
        action, pattern = training.apply_rules(d["url"], active) if active else ("", "")
        d["covered"] = action  # '' | 'flag' | 'deny'
        d["covered_by"] = pattern
        items.append(d)
    return {"total": total, "offset": offset, "limit": limit, "disputes": items}


def update_dispute(dispute_id: int, *, comment: str, path: str | None = None) -> None:
    with connect(path) as c:
        c.execute("UPDATE feedback SET reason=? WHERE id=?", (comment, dispute_id))
        c.commit()


def delete_dispute(dispute_id: int, path: str | None = None) -> None:
    with connect(path) as c:
        c.execute("DELETE FROM feedback WHERE id=?", (dispute_id,))
        c.commit()


def list_rules(status: str | None = None, path: str | None = None) -> list[dict]:
    where, params = "", []
    if status:
        where = "WHERE status=?"; params = [status]
    with connect(path) as c:
        rows = c.execute(
            f"""SELECT id, rule_type, pattern, action, status, support, orgs, confirms,
                       examples, created_at, updated_at
                FROM learned_rules {where}
                ORDER BY (status='proposed') DESC, support DESC, orgs DESC""",
            params).fetchall()
    import json as _json
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["examples"] = _json.loads(d.get("examples") or "[]")
        except Exception:
            d["examples"] = []
        out.append(d)
    return out


def active_rules(path: str | None = None) -> list[dict]:
    with connect(path) as c:
        rows = c.execute(
            "SELECT rule_type, pattern, action FROM learned_rules WHERE status='active'").fetchall()
    return [dict(r) for r in rows]


def set_rule(rule_id: int, *, status: str | None = None, action: str | None = None,
             updated_at: str = "", path: str | None = None) -> None:
    sets, params = [], []
    if status is not None:
        sets.append("status=?"); params.append(status)
    if action is not None:
        sets.append("action=?"); params.append(action)
    if not sets:
        return
    sets.append("updated_at=?"); params.append(updated_at)
    params.append(rule_id)
    with connect(path) as c:
        c.execute(f"UPDATE learned_rules SET {', '.join(sets)} WHERE id=?", params)
        c.commit()


def create_rule(*, rule_type: str, pattern: str, action: str = "deny",
                now: str = "", path: str | None = None) -> dict:
    """Create (or activate) a global rule directly from one dispute — the
    'Make a rule from this' action. Counts support/orgs/confirms from the whole
    feedback log so the rule card shows meaningful stats + shared-vendor risk."""
    from . import training
    pattern = (pattern or "").strip().lower()
    if not pattern or rule_type not in ("host", "pattern"):
        raise ValueError("rule_type must be host|pattern with a non-empty pattern")
    with connect(path) as c:
        fb = c.execute("SELECT orgid, url, host, verdict FROM feedback").fetchall()
    support, confirms, orgs, examples = 0, 0, set(), []
    for r in fb:
        if rule_type == "host":
            h = r["host"] or training._host(r["url"])
            match = h == pattern or h.endswith("." + pattern)
        else:
            match = pattern in training.path_tokens(r["url"])
        if not match:
            continue
        if r["verdict"] == "wrong":
            support += 1; orgs.add(r["orgid"])
            if len(examples) < 5:
                examples.append(r["url"])
        elif r["verdict"] == "confirmed":
            confirms += 1
    import json as _json
    ex = _json.dumps(examples)
    with connect(path) as c:
        existing = c.execute("SELECT id FROM learned_rules WHERE rule_type=? AND pattern=?",
                             (rule_type, pattern)).fetchone()
        if existing:
            c.execute("""UPDATE learned_rules SET action=?, status='active', support=?, orgs=?,
                         confirms=?, examples=?, updated_at=? WHERE id=?""",
                      (action, support, len(orgs), confirms, ex, now, existing[0]))
            rid = existing[0]
        else:
            sql = ("""INSERT INTO learned_rules
                      (rule_type,pattern,action,status,support,orgs,confirms,examples,created_at,updated_at)
                      VALUES (?,?,?,?,?,?,?,?,?,?)""" + (" RETURNING id" if _IS_PG else ""))
            cur = c.execute(sql, (rule_type, pattern, action, "active", support, len(orgs), confirms, ex, now, now))
            rid = cur.fetchone()[0] if _IS_PG else cur.lastrowid
        c.commit()
    return {"id": rid, "rule_type": rule_type, "pattern": pattern, "action": action,
            "support": support, "orgs": len(orgs), "confirms": confirms}


def feedback_stats(path: str | None = None) -> dict:
    with connect(path) as c:
        disputes = c.execute("SELECT COUNT(*) FROM feedback WHERE verdict='wrong'").fetchone()[0]
        confirms = c.execute("SELECT COUNT(*) FROM feedback WHERE verdict='confirmed'").fetchone()[0]
        orgs = c.execute("SELECT COUNT(DISTINCT orgid) FROM feedback WHERE verdict='wrong'").fetchone()[0]
        active = c.execute("SELECT COUNT(*) FROM learned_rules WHERE status='active'").fetchone()[0]
        proposed = c.execute("SELECT COUNT(*) FROM learned_rules WHERE status='proposed'").fetchone()[0]
    return {"disputes": disputes, "confirms": confirms, "orgs_disputed": orgs,
            "rules_active": active, "rules_proposed": proposed}


def upsert_rule(*, rule_type: str, pattern: str, support: int, orgs: int, confirms: int,
                examples: list[str], now: str = "", path: str | None = None) -> str:
    """Insert a mined rule as 'proposed', or refresh counts on an existing one
    (preserving the human's status/action). Returns 'new' | 'updated'."""
    import json as _json
    ex = _json.dumps(examples[:5])
    with connect(path) as c:
        existing = c.execute("SELECT id FROM learned_rules WHERE rule_type=? AND pattern=?",
                             (rule_type, pattern)).fetchone()
        if existing:
            c.execute("""UPDATE learned_rules SET support=?, orgs=?, confirms=?, examples=?, updated_at=?
                         WHERE id=?""", (support, orgs, confirms, ex, now, existing[0]))
            c.commit()
            return "updated"
        c.execute("""INSERT INTO learned_rules
                     (rule_type,pattern,action,status,support,orgs,confirms,examples,created_at,updated_at)
                     VALUES (?,?,?,?,?,?,?,?,?,?)""",
                  (rule_type, pattern, "flag", "proposed", support, orgs, confirms, ex, now, now))
        c.commit()
        return "new"


def stats(path: str | None = None) -> dict:
    with connect(path) as c:
        portals = c.execute("SELECT COUNT(*) FROM portals").fetchone()[0]
        unis = c.execute("SELECT COUNT(DISTINCT orgid) FROM portals").fetchone()[0]
    return {"portals": portals, "universities": unis}


def insights(path: str | None = None) -> dict:
    """Aggregate dashboard stats for the Quick Insights page.

    'Live' numbers use the Verified Orgs sheet as ground truth (520 orgs / 593
    login URLs) — NOT a per-row match, which would double-count institutions
    that appear as both a SheerID-orgid row and a state-sheet row."""
    vidx = verified_index(path)
    with connect(path) as c:
        uni_rows = c.execute("SELECT orgid, country, state FROM universities").fetchall()
        pcounts = dict(c.execute("SELECT orgid, COUNT(*) FROM portals GROUP BY orgid").fetchall())
        portal_rows = c.execute("SELECT orgid, portal_url, country FROM portals").fetchall()
        cat_rows = c.execute(
            "SELECT category, COUNT(*) FROM portals GROUP BY category ORDER BY COUNT(*) DESC").fetchall()

    country: dict[str, dict] = {}
    state: dict[str, dict] = {}
    total_uni = len(uni_rows)
    with_portal = 0
    db_orgids: set[str] = set()
    for r in uni_rows:
        db_orgids.add(str(r["orgid"]))
        co = r["country"] or "India"
        cs = country.setdefault(co, {"universities": 0, "with_portal": 0, "portals": 0})
        cs["universities"] += 1
        has = pcounts.get(r["orgid"], 0) > 0
        if has:
            with_portal += 1; cs["with_portal"] += 1
        if r["state"]:
            st = state.setdefault(r["state"], {"universities": 0, "with_portal": 0})
            st["universities"] += 1
            if has:
                st["with_portal"] += 1

    total_portals = len(portal_rows)
    for pr in portal_rows:
        co = pr["country"] or "India"
        country.setdefault(co, {"universities": 0, "with_portal": 0, "portals": 0})["portals"] += 1

    # Ground truth from the Verified Orgs sheet
    live_universities = len(vidx["orgids"])          # 520
    live_portals = len(vidx["urls"])                  # distinct live login URLs
    live_mapped = len(vidx["orgids"] & db_orgids)     # of those, how many we hold by orgid

    by_country = [{"country": k, **v} for k, v in sorted(country.items(), key=lambda kv: -kv[1]["universities"])]
    top_states = [{"state": k, **v} for k, v in sorted(state.items(), key=lambda kv: -kv[1]["universities"])][:15]
    by_category = [{"category": c or "Uncategorized", "count": n} for c, n in cat_rows]

    return {
        "universities": {"total": total_uni, "with_portal": with_portal,
                         "zero_portal": total_uni - with_portal,
                         "live": live_universities, "live_mapped_in_db": live_mapped,
                         "coverage_pct": round(100 * with_portal / total_uni, 1) if total_uni else 0},
        "portals": {"total": total_portals, "live": live_portals,
                    "avg_per_covered_uni": round(total_portals / with_portal, 1) if with_portal else 0},
        "verified_orgs": live_universities,
        "by_country": by_country,
        "by_category": by_category,
        "top_states": top_states,
    }

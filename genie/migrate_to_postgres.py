#!/usr/bin/env python3
"""One-time migration: copy the Genie SQLite DB into a Postgres database.

Reads the local SQLite file directly and writes every table into the Postgres
target given by GENIE_DB_URL. Idempotent for the natural-key tables
(universities / portals / verified_orgs / metrics use INSERT OR IGNORE → skips
dupes); feedback / learned_rules have autoincrement ids, so run this against a
FRESH/empty Postgres to avoid duplicate rows.

Usage:
  export GENIE_DB_URL='postgresql://user:pass@host:5432/genie'
  .venv/bin/python genie/migrate_to_postgres.py            # uses ./genie.db
  .venv/bin/python genie/migrate_to_postgres.py /path/to/genie.db
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "genie" / "core"))

# Tables + whether their integer 'id' PK should be dropped (let Postgres assign).
TABLES = [
    ("universities", False),
    ("verified_orgs", False),
    ("metrics", False),
    ("portals", True),
    ("feedback", True),
    ("learned_rules", True),
]


def main() -> None:
    if not os.getenv("GENIE_DB_URL", "").startswith("postgres"):
        sys.exit("Set GENIE_DB_URL='postgresql://…' first (the Postgres target).")
    sqlite_path = sys.argv[1] if len(sys.argv) > 1 else str(ROOT / "genie" / "genie.db")
    if not Path(sqlite_path).exists():
        sys.exit(f"SQLite file not found: {sqlite_path}")

    src = sqlite3.connect(sqlite_path)
    src.row_factory = sqlite3.Row

    from genie_core import db  # imported AFTER GENIE_DB_URL is set → targets Postgres
    from genie_core import metrics
    assert db.is_postgres(), "genie_core did not pick up Postgres — check GENIE_DB_URL"
    print("creating Postgres schema…")
    db.init_db()
    metrics._ensure()  # metrics table lives in metrics.py, not init_db

    for table, drop_id in TABLES:
        try:
            cols = [r[1] for r in src.execute(f"PRAGMA table_info({table})").fetchall()]
        except sqlite3.OperationalError:
            print(f"  {table}: not in SQLite, skipping")
            continue
        if not cols:
            print(f"  {table}: no columns, skipping")
            continue
        ins = [c for c in cols if not (drop_id and c == "id")]
        rows = src.execute(f"SELECT {', '.join(ins)} FROM {table}").fetchall()
        if not rows:
            print(f"  {table}: 0 rows")
            continue
        ph = ", ".join(["?"] * len(ins))
        sql = f"INSERT OR IGNORE INTO {table} ({', '.join(ins)}) VALUES ({ph})"
        with db.connect() as dst:
            # batch to keep memory + round-trips reasonable
            batch = [tuple(r) for r in rows]
            for i in range(0, len(batch), 500):
                dst.executemany(sql, batch[i:i + 500])
            dst.commit()
        print(f"  {table}: {len(rows)} rows migrated")

    with db.connect() as dst:
        u = dst.execute("SELECT COUNT(*) FROM universities").fetchone()[0]
        p = dst.execute("SELECT COUNT(*) FROM portals").fetchone()[0]
        v = dst.execute("SELECT COUNT(*) FROM verified_orgs").fetchone()[0]
    print(f"\n✓ Postgres now has {u} universities, {p} portals, {v} verified orgs")
    print("Point the API at it by keeping GENIE_DB_URL set when you run uvicorn.")


if __name__ == "__main__":
    main()

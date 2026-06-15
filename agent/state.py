"""SQLite-backed state store so the pipeline is resumable after interruption."""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

STAGES: tuple[str, ...] = (
    "discovery",
    "confidence",
    "tc_finder",
    "tc_analyzer",
    "sheet_writer",
)

# orgid_status tracks the *latest* stage transition for each OrgID plus the
# final per-run summary fields (completed_at, portals_found). These summary
# fields are populated when the pipeline reaches a terminal state (success /
# failed_write / failed_discovery).
#
# stage_results stores the per-stage JSON output so a later stage on resume
# can pick up the previous stage's output without re-running it.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS orgid_status (
    orgid          TEXT PRIMARY KEY,
    stage          TEXT NOT NULL,
    status         TEXT NOT NULL,
    last_error     TEXT,
    updated_at     REAL NOT NULL,
    completed_at   REAL,
    portals_found  INTEGER
);

CREATE TABLE IF NOT EXISTS stage_results (
    orgid       TEXT NOT NULL,
    stage       TEXT NOT NULL,
    result_json TEXT,
    updated_at  REAL NOT NULL,
    PRIMARY KEY (orgid, stage)
);

-- Stage C analyzer cache, keyed by normalised T&C URL. Two portals (or two
-- OrgIDs) that share a T&C document analyse it once.
CREATE TABLE IF NOT EXISTS tc_analyzer_cache (
    tc_url      TEXT PRIMARY KEY,
    result_json TEXT NOT NULL,
    updated_at  REAL NOT NULL
);

-- Stage C vendor-level T&C cache (Part 3). Maps a campus-software vendor to
-- one authoritative verdict so every college on that vendor resolves
-- instantly without re-fetching/re-analysing. Seeded from
-- config.VENDOR_TC_MAP on first open; grown at runtime as new vendors are
-- auto-learned by the analyzer.
CREATE TABLE IF NOT EXISTS vendor_tc_map (
    vendor_name   TEXT PRIMARY KEY,
    vendor_tc_url TEXT,
    verdict       TEXT,
    reasoning     TEXT,
    last_analyzed REAL
);

CREATE INDEX IF NOT EXISTS idx_orgid_status_status ON orgid_status(status);
"""

# Additive migrations for pre-existing databases that were created before
# `completed_at` / `portals_found` were added. SQLite's `ALTER TABLE ADD
# COLUMN` is a no-op if the column is missing; if it already exists we
# swallow the OperationalError.
_MIGRATIONS: tuple[str, ...] = (
    "ALTER TABLE orgid_status ADD COLUMN completed_at REAL",
    "ALTER TABLE orgid_status ADD COLUMN portals_found INTEGER",
)


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._apply_migrations()
        self._seed_vendor_tc_map()
        self._conn.commit()

    def _apply_migrations(self) -> None:
        for stmt in _MIGRATIONS:
            try:
                self._conn.execute(stmt)
            except sqlite3.OperationalError:
                pass  # column already exists

    def _seed_vendor_tc_map(self) -> None:
        """Seed `vendor_tc_map` from config.VENDOR_TC_MAP with the verified
        verdicts. INSERT OR IGNORE so operator edits and auto-learned rows
        are never clobbered on re-open; only missing seed vendors are added.
        Lazy import keeps state.py free of an import-time config dependency.
        """
        try:
            from .config import VENDOR_TC_MAP
        except Exception:  # pragma: no cover - config import shouldn't fail
            return
        now = time.time()
        for vendor_name, info in VENDOR_TC_MAP.items():
            try:
                self._conn.execute(
                    """
                    INSERT OR IGNORE INTO vendor_tc_map
                        (vendor_name, vendor_tc_url, verdict, reasoning, last_analyzed)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        vendor_name,
                        info.get("tc_url", "") or "",
                        info.get("verdict"),
                        "seed (config.VENDOR_TC_MAP)",
                        now,
                    ),
                )
            except sqlite3.OperationalError:
                pass

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "StateStore":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def mark_stage(
        self,
        orgid: str,
        stage: str,
        status: str,
        error: str | None = None,
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO orgid_status (orgid, stage, status, last_error, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(orgid) DO UPDATE SET
                    stage = excluded.stage,
                    status = excluded.status,
                    last_error = excluded.last_error,
                    updated_at = excluded.updated_at
                """,
                (orgid, stage, status, error, time.time()),
            )

    def mark_final(
        self,
        orgid: str,
        *,
        status: str,
        stage: str = "sheet_writer",
        portals_found: int | None = None,
        error: str | None = None,
    ) -> None:
        """Terminal per-OrgID transition. Sets completed_at and portals_found."""
        now = time.time()
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO orgid_status
                    (orgid, stage, status, last_error, updated_at, completed_at, portals_found)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(orgid) DO UPDATE SET
                    stage = excluded.stage,
                    status = excluded.status,
                    last_error = excluded.last_error,
                    updated_at = excluded.updated_at,
                    completed_at = excluded.completed_at,
                    portals_found = excluded.portals_found
                """,
                (orgid, stage, status, error, now, now, portals_found),
            )

    def save_result(self, orgid: str, stage: str, result: Any) -> None:
        payload = json.dumps(result, default=str)
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO stage_results (orgid, stage, result_json, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(orgid, stage) DO UPDATE SET
                    result_json = excluded.result_json,
                    updated_at = excluded.updated_at
                """,
                (orgid, stage, payload, time.time()),
            )

    def get_result(self, orgid: str, stage: str) -> Any | None:
        row = self._conn.execute(
            "SELECT result_json FROM stage_results WHERE orgid = ? AND stage = ?",
            (orgid, stage),
        ).fetchone()
        if row is None or row["result_json"] is None:
            return None
        return json.loads(row["result_json"])

    def status_for(self, orgid: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM orgid_status WHERE orgid = ?",
            (orgid,),
        ).fetchone()

    def list_by_status(self, status: str) -> list[sqlite3.Row]:
        return list(
            self._conn.execute(
                "SELECT * FROM orgid_status WHERE status = ? ORDER BY updated_at",
                (status,),
            )
        )

    def all_statuses(self) -> list[sqlite3.Row]:
        return list(self._conn.execute("SELECT * FROM orgid_status ORDER BY updated_at"))

    def is_done(self, orgid: str) -> bool:
        row = self.status_for(orgid)
        return row is not None and row["status"] == "success"

    # ---- Stage C analyzer cache ----

    def get_tc_cache(self, tc_url: str) -> Any | None:
        row = self._conn.execute(
            "SELECT result_json FROM tc_analyzer_cache WHERE tc_url = ?",
            (tc_url,),
        ).fetchone()
        if row is None:
            return None
        return json.loads(row["result_json"])

    def set_tc_cache(self, tc_url: str, result: Any) -> None:
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO tc_analyzer_cache (tc_url, result_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(tc_url) DO UPDATE SET
                    result_json = excluded.result_json,
                    updated_at = excluded.updated_at
                """,
                (tc_url, json.dumps(result, default=str), time.time()),
            )

    # ---- Stage C vendor-level T&C cache (Part 3) ----

    def get_vendor_tc(self, vendor_name: str) -> dict[str, Any] | None:
        """Return {vendor_tc_url, verdict, reasoning, last_analyzed} for a
        vendor, or None when unknown / no verdict recorded yet."""
        if not vendor_name:
            return None
        row = self._conn.execute(
            "SELECT vendor_tc_url, verdict, reasoning, last_analyzed "
            "FROM vendor_tc_map WHERE vendor_name = ?",
            (vendor_name,),
        ).fetchone()
        if row is None or not row["verdict"]:
            return None
        return {
            "vendor_tc_url": row["vendor_tc_url"],
            "verdict": row["verdict"],
            "reasoning": row["reasoning"],
            "last_analyzed": row["last_analyzed"],
        }

    def set_vendor_tc(
        self,
        vendor_name: str,
        vendor_tc_url: str | None,
        verdict: str,
        reasoning: str | None = None,
    ) -> None:
        """Upsert a vendor verdict — used both to refresh seeds and to
        AUTO-LEARN a newly discovered vendor so all future colleges on it
        resolve instantly."""
        if not vendor_name:
            return
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO vendor_tc_map
                    (vendor_name, vendor_tc_url, verdict, reasoning, last_analyzed)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(vendor_name) DO UPDATE SET
                    vendor_tc_url = excluded.vendor_tc_url,
                    verdict       = excluded.verdict,
                    reasoning     = excluded.reasoning,
                    last_analyzed = excluded.last_analyzed
                """,
                (vendor_name, vendor_tc_url or "", verdict, reasoning, time.time()),
            )

    def get_vendor_names(self) -> list[str]:
        """All known vendor names (seed + auto-learned) — used by the finder
        to detect a learned vendor by signature substring on later colleges."""
        rows = self._conn.execute(
            "SELECT vendor_name FROM vendor_tc_map"
        ).fetchall()
        return [r["vendor_name"] for r in rows if r["vendor_name"]]

    def purge_orgid(self, orgid: str) -> tuple[int, int]:
        """Delete every row for this OrgID from both state tables.

        Returns (stage_results_deleted, orgid_status_deleted).
        """
        with self._tx() as conn:
            sr = conn.execute(
                "DELETE FROM stage_results WHERE orgid = ?", (orgid,)
            ).rowcount
            os = conn.execute(
                "DELETE FROM orgid_status WHERE orgid = ?", (orgid,)
            ).rowcount
        return (sr, os)

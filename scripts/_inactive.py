"""Canonical set of INACTIVE org IDs — orgs the user has marked to exclude from
ALL portal / T&C discovery runs. Backed by scripts/inactive_orgs.csv (orgId,name).

Usage in a runner:
    from _inactive import INACTIVE          # set[str] of org ids
    if oid in INACTIVE: continue            # skip
"""
from __future__ import annotations

import csv
from pathlib import Path

_CSV = Path(__file__).resolve().parent / "inactive_orgs.csv"


def load_inactive() -> set[str]:
    if not _CSV.exists():
        return set()
    out: set[str] = set()
    with open(_CSV, newline="") as f:
        for row in csv.DictReader(f):
            oid = (row.get("orgId") or "").strip()
            if oid:
                out.add(oid)
    return out


INACTIVE: set[str] = load_inactive()

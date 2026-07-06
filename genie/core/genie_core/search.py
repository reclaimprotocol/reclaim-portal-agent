"""Feature 1 — search the portals DB we've already built."""
from __future__ import annotations

from . import db
from .models import UniversityPortals


def search_portals(query: str, limit: int = 20, country: str | None = None,
                   state: str | None = None) -> list[UniversityPortals]:
    if not query or not query.strip():
        return []
    return db.search(query, limit=limit, country=country, state=state)

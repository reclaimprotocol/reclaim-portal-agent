"""genie_core — the single source of Genie logic. Every surface (FastAPI now,
MCP later, CLI) is a thin adapter over these functions."""
from .models import Portal, UniversityPortals, ProgressEvent
from .search import search_portals
from .discover import discover_portals, host_of
from .metrics import get_metrics, get_metrics_batch
from . import db
from . import training

__all__ = [
    "Portal", "UniversityPortals", "ProgressEvent",
    "search_portals", "discover_portals", "host_of",
    "get_metrics", "get_metrics_batch", "db", "training",
]

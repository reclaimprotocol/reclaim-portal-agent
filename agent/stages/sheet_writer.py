"""Stage D — Sheet Writer (one row per OrgID).

Aggregates every portal Stage A discovered for this OrgID into a single
Portals-tab row using the 5-column schema:

    OrgID | University Name | Portal URLs | T&C URLs | Overall T&C Verdict

Multi-value cells hold "\\n"-joined strings — Google Sheets renders newlines
as in-cell line breaks, matching SheerID's existing Universities-tab
convention. T&C URLs are deduplicated (case-insensitive, trailing-slash
stripped); the overall verdict is the majority vote across per-portal
verdicts via `tc_analyzer.aggregate_verdicts`.

Per-portal debug fields (category, JS-rendered, source, per-portal verdict,
evidence, reasoning) deliberately do NOT make it to the sheet — they stay
in state.db where they're useful for filtering / debugging without
cluttering SheerID's view.

Upsert by OrgID: if a row exists we overwrite it in place; otherwise we
append. `scripts/purge_orgid.py` is still the way to truly remove an
OrgID's row.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from ..config import CATEGORY_ORDER, CATEGORY_REMAP_FOR_SORTING, load_config
from ..sheets_client import SheetsClient
from . import tc_analyzer

if TYPE_CHECKING:
    from ..pipeline import PipelineContext

logger = logging.getLogger(__name__)


def run(ctx: "PipelineContext") -> dict[str, Any]:
    config = load_config()
    orgid = str(ctx.orgid)

    discovery_result = ctx.results.get("discovery") or {}
    portals = discovery_result.get("portals") or []
    university_name = (
        discovery_result.get("university_name")
        or str(ctx.row.get("SheerID University Name", "")).strip()
    )

    if not portals:
        logger.warning("[%s] sheet_writer called with no portals; nothing to write", orgid)
        return {"portals_written": 0, "rows_written": 0}

    analyzer_result = ctx.results.get("tc_analyzer") or {}

    # Order portals by category (CATEGORY_ORDER post-remap), URL within
    # category. Portals whose category isn't in CATEGORY_ORDER (post-remap)
    # bucket into "Other" and append last. Sort happens up front so
    # downstream loops (T&C-URL dedup, verdict aggregation) iterate in the
    # same order the cell renders.
    sorted_portals = sorted(
        (p for p in portals if (p.get("url") or "").strip()),
        key=_portal_sort_key,
    )

    portal_urls_list: list[str] = [
        (portal.get("url") or "").strip() for portal in sorted_portals
    ]

    # T&C is now resolved per-OrgID (Stage C collects ALL terms/privacy/
    # disclaimer pages across the university site + portal hosts), so we
    # aggregate over the analyses directly rather than per portal. Every
    # analysis carries its (tc_url, verdict); `aggregate_verdicts_by_url`
    # lets a binding Terms page outweigh permissive privacy/disclaimer pages.
    tc_urls_seen: set[str] = set()
    tc_urls_ordered: list[str] = []
    url_verdict_pairs: list[tuple[str, str]] = []
    for analysis in (analyzer_result.get("tc_analyses") or []):
        verdict = str(analysis.get("verdict") or "Yes (No T&C Found)")
        tc_url = str(analysis.get("tc_url") or "").strip()
        url_verdict_pairs.append((tc_url, verdict))
        if tc_url:
            key = _normalize_tc_url_for_dedup(tc_url)
            if key not in tc_urls_seen:
                tc_urls_seen.add(key)
                tc_urls_ordered.append(tc_url)

    overall_verdict = tc_analyzer.aggregate_verdicts_by_url(url_verdict_pairs)

    new_row: dict[str, Any] = {
        "OrgID": orgid,
        "University Name": university_name,
        "Portal URLs": "\n".join(portal_urls_list),
        "T&C URLs": "\n".join(tc_urls_ordered),
        "Overall T&C Verdict": overall_verdict,
    }

    sheets = SheetsClient.from_config(config)
    sheets.ensure_portals_header()
    existing_rows = sheets.read_portals_by_orgid(orgid)

    if existing_rows:
        # Overwrite the first existing row for this OrgID. Any duplicate
        # rows from a legacy schema get left in place — purge_orgid.py is
        # the explicit cleanup tool.
        row_num, _ = existing_rows[0]
        values = [new_row.get(col, "") for col in SheetsClient.PORTALS_COLUMNS]
        sheets.update_portal_rows([(row_num, values)])
        outcome = "updated"
    else:
        sheets.append_portal_rows([new_row])
        outcome = "appended"

    logger.info(
        "[%s] sheet_writer: %s row "
        "(portals=%d, unique_tc_urls=%d, overall_verdict=%s)",
        orgid, outcome, len(portal_urls_list), len(tc_urls_ordered), overall_verdict,
    )

    return {
        "portals_written": len(portal_urls_list),
        "rows_written": 1,
        "tc_urls_written": len(tc_urls_ordered),
        "overall_verdict": overall_verdict,
        "outcome": outcome,
    }


# ------------------------------------------------------------------ helpers

def _portal_sort_key(portal: dict[str, Any]) -> tuple[int, str]:
    """Sort key: (category index in CATEGORY_ORDER post-remap, url lowered).
    Categories outside CATEGORY_ORDER (and outside the remap) go to a
    bucket index past the last canonical one, so they render last."""
    raw_cat = str(portal.get("category") or "Other")
    sort_cat = CATEGORY_REMAP_FOR_SORTING.get(raw_cat, raw_cat)
    try:
        cat_idx = CATEGORY_ORDER.index(sort_cat)
    except ValueError:
        cat_idx = len(CATEGORY_ORDER)  # Other → after every named bucket
    return (cat_idx, (portal.get("url") or "").lower())


def _normalize_portal_url(url: str) -> str:
    """Lower-case scheme+host, strip trailing slash on path. Mirrors the
    Stage A dedup key so the analyzer→writer hand-off agrees."""
    p = urlsplit(str(url))
    scheme = (p.scheme or "https").lower()
    host = p.netloc.lower().split(":")[0]
    path = (p.path or "").rstrip("/")
    return f"{scheme}://{host}{path}"


def _normalize_tc_url_for_dedup(url: str) -> str:
    """Case-insensitive + trailing-slash-stripped key for T&C URL dedup
    inside an OrgID's row. Lighter than `tc_analyzer.normalize_tc_url`
    (which strips session IDs) — for sheet display we want byte-identical
    URLs to fold but otherwise preserve query strings if present."""
    s = str(url).strip().lower()
    if "?" in s or "#" in s:
        return s.rstrip("/")
    return s.rstrip("/")

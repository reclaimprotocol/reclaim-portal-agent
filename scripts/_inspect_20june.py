#!/usr/bin/env python3
"""Throwaway: dump structure of one or more tabs."""
from __future__ import annotations
import sys
import _bootstrap  # noqa: F401

from agent.config import load_config
from agent.sheets_client import SheetsClient
from _run_dated_tab_portals import _resolve_tab_title
from run_portal_sheet import PORTAL_SHEET_ID

config = load_config()
sheets = SheetsClient(
    sheet_id=PORTAL_SHEET_ID, universities_tab="x", portals_tab="x",
    credentials_path=config.google_credentials_path, token_path=config.google_token_path,
)
for tab in sys.argv[1:]:
    title = _resolve_tab_title(sheets, tab)
    qtab = f"'{title}'"
    print("=" * 70)
    print("RESOLVED TAB:", repr(title))
    rows = sheets._get_values(qtab, "1:6")
    for i, r in enumerate(rows, 1):
        print(f"ROW {i}: {r}")
    allrows = sheets._get_values(qtab, "1:100000")
    print("TOTAL rows:", len(allrows))

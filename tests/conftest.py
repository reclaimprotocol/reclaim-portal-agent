"""Shared test setup."""

import pytest


@pytest.fixture(autouse=True)
def _no_real_browser(monkeypatch):
    """The click-through launches Chromium and loads live pages. No test may do
    that by accident; the tests that exercise it replace `_click_through`."""
    from agent import click_through
    monkeypatch.setattr(click_through, "ENABLED", False)

"""Tests for the OpenAI web-search candidate adapter (no network/API key)."""

import asyncio

from agent.search_fallback import execute_search_fallback, parse_results


def test_parse_results_extracts_and_deduplicates_web_search_sources() -> None:
    response = {
        "output": [{
            "type": "web_search_call",
            "action": {"sources": [
                {"url": "https://portal.example.edu/login", "title": "Student portal"},
                {"url": "https://portal.example.edu/login", "title": "Duplicate"},
                {"url": "https://moodle.example.edu/login", "title": "Moodle"},
            ]},
        }],
    }
    assert parse_results(response) == [
        {"url": "https://portal.example.edu/login", "text": "Student portal"},
        {"url": "https://moodle.example.edu/login", "text": "Moodle"},
    ]


def test_parse_results_keeps_direct_portal_url_written_in_search_answer() -> None:
    response = {
        "output_text": "The official guide says to log in at "
                       "https://portal.example.edu/login.",
        "output": [{"action": {"sources": [
            {"url": "https://www.example.edu/student-guide", "title": "Student guide"},
        ]}}],
    }
    assert parse_results(response) == [
        {"url": "https://www.example.edu/student-guide", "text": "Student guide"},
        {"url": "https://portal.example.edu/login", "text": "OpenAI web-search direct URL"},
    ]


def test_search_uses_web_search_and_returns_cited_sources(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    calls: list[dict] = []

    class Responses:
        async def create(self, **kwargs):
            calls.append(kwargs)
            return {"output": [{"action": {"sources": [
                {"url": "https://lms.example.edu/login", "title": "LMS"},
            ]}}]}

    class Client:
        responses = Responses()

    results = asyncio.run(execute_search_fallback(
        "Example University", "example.edu", client=Client(),
    ))
    assert results == [{"url": "https://lms.example.edu/login", "text": "LMS"}]
    assert calls[0]["tools"][0]["type"] == "web_search"
    assert calls[0]["tool_choice"] == "required"
    assert "student-facing login portals" in calls[0]["input"]

"""Anthropic Claude API wrapper with retry + rate-limit handling."""
from __future__ import annotations

import logging
import time
from typing import Any

import anthropic
from anthropic import APIStatusError, RateLimitError

logger = logging.getLogger(__name__)


class AnthropicClient:
    def __init__(self, api_key: str, model: str) -> None:
        self._client = anthropic.Anthropic(api_key=api_key)
        self.model = model

    def complete(
        self,
        system: str,
        user: str,
        max_tokens: int = 2048,
        max_retries: int = 5,
    ) -> str:
        attempt = 0
        while True:
            try:
                msg = self._client.messages.create(
                    model=self.model,
                    max_tokens=max_tokens,
                    system=system,
                    messages=[{"role": "user", "content": user}],
                )
                return self._extract_text(msg)
            except RateLimitError as err:
                attempt = self._handle_rate_limit(err, attempt, max_retries, label="complete")
            except APIStatusError as err:
                attempt = self._handle_status_error(err, attempt, max_retries, label="complete")

    def complete_with_web_search(
        self,
        *,
        system: str,
        user: str,
        max_uses: int = 5,
        model: str | None = None,
        max_tokens: int = 4096,
        max_retries: int = 3,
    ) -> str:
        """Call messages.create with the `web_search_20250305` server tool.

        Returns the concatenated text from Claude's final response. Intermediate
        server_tool_use / web_search_tool_result blocks are handled by the API
        internally — we only surface the final text Claude produced.
        """
        tool = {
            "type": "web_search_20250305",
            "name": "web_search",
            "max_uses": max_uses,
        }
        attempt = 0
        while True:
            try:
                msg = self._client.messages.create(
                    model=model or self.model,
                    max_tokens=max_tokens,
                    system=system,
                    tools=[tool],
                    messages=[{"role": "user", "content": user}],
                )
                return self._extract_text(msg)
            except RateLimitError as err:
                attempt = self._handle_rate_limit(err, attempt, max_retries, label="web_search")
            except APIStatusError as err:
                attempt = self._handle_status_error(err, attempt, max_retries, label="web_search")

    # -------------------------------------------------------------- helpers

    @staticmethod
    def _extract_text(msg: Any) -> str:
        return "".join(
            getattr(block, "text", "")
            for block in msg.content
            if getattr(block, "type", None) == "text"
        )

    def _handle_rate_limit(
        self, err: RateLimitError, attempt: int, max_retries: int, *, label: str
    ) -> int:
        wait = self._retry_after(err) or min(60.0, 2.0 ** attempt)
        logger.warning("Claude %s rate-limited; sleeping %.1fs (attempt %d)", label, wait, attempt + 1)
        time.sleep(wait)
        if attempt + 1 > max_retries:
            raise err
        return attempt + 1

    def _handle_status_error(
        self, err: APIStatusError, attempt: int, max_retries: int, *, label: str
    ) -> int:
        if 500 <= err.status_code < 600 and attempt < max_retries:
            wait = min(60.0, 2.0 ** attempt)
            logger.warning("Claude %s %d; backing off %.1fs", label, err.status_code, wait)
            time.sleep(wait)
            return attempt + 1
        raise err

    @staticmethod
    def _retry_after(err: Any) -> float | None:
        response = getattr(err, "response", None)
        headers = getattr(response, "headers", None) or {}
        val = headers.get("retry-after") or headers.get("Retry-After")
        try:
            return float(val) if val is not None else None
        except (TypeError, ValueError):
            return None

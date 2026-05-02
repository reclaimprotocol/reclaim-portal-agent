"""Orchestrator: iterate OrgIDs, run pipeline, handle top-level rate limits."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Iterable

from anthropic import RateLimitError
from googleapiclient.errors import HttpError

from .config import Config
from .pipeline import PipelineContext, run_pipeline
from .sheets_client import SheetsClient
from .stages.js_renderer import JSRenderer
from .state import StateStore

logger = logging.getLogger(__name__)


@dataclass
class RunStats:
    done: int = 0
    skipped: int = 0
    failed: int = 0
    stubbed: int = 0

    def as_dict(self) -> dict[str, int]:
        return {"done": self.done, "skipped": self.skipped, "failed": self.failed, "stubbed": self.stubbed}


def _rate_limit_sleep(seconds: float, reason: str) -> None:
    logger.warning("Rate-limit pause (%s): sleeping %.0fs", reason, seconds)
    time.sleep(seconds)


class Orchestrator:
    def __init__(self, config: Config, state: StateStore) -> None:
        self.config = config
        self.state = state

    def process(
        self,
        rows: Iterable[dict],
        limit: int | None = None,
        *,
        force: bool = False,
    ) -> RunStats:
        stats = RunStats()
        processed = 0

        # One shared Playwright browser per run (launched lazily on first
        # escalation). Closed via __exit__ regardless of exceptions.
        js_renderer: JSRenderer | None = None
        if self.config.enable_js_rendering:
            js_renderer = JSRenderer(
                timeout_seconds=self.config.js_rendering_timeout_seconds,
                user_agent=self.config.user_agent,
            )

        try:
            for row in rows:
                if limit is not None and processed >= limit:
                    break
                orgid = self._extract_orgid(row)
                if not orgid:
                    continue

                if self.state.is_done(orgid):
                    if force:
                        prior = self.state.status_for(orgid)
                        prior_status = prior["status"] if prior else "unknown"
                        logger.info(
                            "[%s] --force passed, ignoring prior status (was: %s)",
                            orgid, prior_status,
                        )
                    else:
                        logger.info("[%s] already complete — skipping", orgid)
                        stats.skipped += 1
                        continue

                ctx = PipelineContext(
                    orgid=orgid,
                    row=row,
                    deps={"js_renderer": js_renderer} if js_renderer else {},
                )
                try:
                    ok = run_pipeline(ctx, self.state)
                    if ok:
                        stats.done += 1
                    else:
                        stats.stubbed += 1
                except RateLimitError:
                    _rate_limit_sleep(60, "Claude API")
                    continue
                except HttpError as err:
                    if getattr(err, "resp", None) is not None and err.resp.status == 429:
                        _rate_limit_sleep(60, "Google Sheets API")
                        continue
                    logger.exception("[%s] unrecoverable HttpError", orgid)
                    stats.failed += 1
                except Exception:
                    logger.exception("[%s] pipeline raised", orgid)
                    stats.failed += 1

                processed += 1
        finally:
            if js_renderer is not None:
                js_renderer.close()
        return stats

    @staticmethod
    def _extract_orgid(row: dict) -> str:
        return SheetsClient.extract_orgid(row)

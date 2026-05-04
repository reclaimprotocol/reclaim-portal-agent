"""Runs the pipeline stages for a single OrgID.

Current stage flow
------------------
Stage A (discovery) is fully implemented. Stages B and C are soft stubs
that log a "skipped (not yet implemented)" line and return a sentinel; the
pipeline therefore proceeds **A → (skip B) → (skip C) → D**. When B and C
are filled in later, they'll slot back in without pipeline changes.

Terminal states (written via `StateStore.mark_final`):
* `success`          — every stage completed (B/C may be skipped).
* `failed_discovery` — Stage A returned zero portals.
* `failed_write`     — Stage D raised after retries were exhausted.

Per-stage failures on A record status=`failed` and abort. The orchestrator
separately handles RateLimitError / Sheets 429 by pausing the whole run.

Caching
-------
Each stage's result is cached in `state_results` so resume after
interruption is cheap. The sheet_writer stage is deliberately *not*
cached — re-runs must actually re-write the sheet so the delete+append
idempotency holds.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from .state import StateStore
from .stages import confidence, discovery, sheet_writer, tc_analyzer, tc_finder

logger = logging.getLogger(__name__)


@dataclass
class PipelineContext:
    orgid: str
    row: dict[str, Any]
    results: dict[str, Any] = field(default_factory=dict)
    deps: dict[str, Any] = field(default_factory=dict)


StageFn = Callable[[PipelineContext], dict[str, Any]]


@dataclass(frozen=True)
class StageSpec:
    name: str
    func: StageFn


STAGES: tuple[StageSpec, ...] = (
    StageSpec("discovery", discovery.run),
    StageSpec("confidence", confidence.run),
    StageSpec("tc_finder", tc_finder.run),
    StageSpec("tc_analyzer", tc_analyzer.run),
    StageSpec("sheet_writer", sheet_writer.run),
)

# Stages whose result must not be cached — re-running them is the point
# (e.g. sheet_writer needs to actually rewrite the sheet each time).
_NEVER_CACHED: frozenset[str] = frozenset({"sheet_writer"})


def run_pipeline(ctx: PipelineContext, state: StateStore) -> bool:
    """Run every stage for `ctx.orgid`. Returns True if the pipeline reached
    a terminal success (possibly with B/C skipped); False otherwise.
    """
    # Stages access state via ctx.deps (e.g. tc_analyzer's URL-keyed cache).
    ctx.deps.setdefault("state", state)
    for spec in STAGES:
        # --- cache short-circuit (except for stages we never cache) ---
        if spec.name not in _NEVER_CACHED:
            cached = state.get_result(ctx.orgid, spec.name)
            if cached is not None:
                if isinstance(cached, dict) and cached.get("skipped"):
                    logger.info(
                        "[%s] stage=%s previously skipped; skipping again",
                        ctx.orgid, spec.name,
                    )
                    continue
                logger.info("[%s] stage=%s cached; skipping", ctx.orgid, spec.name)
                ctx.results[spec.name] = cached
                if spec.name == "discovery" and not cached.get("portals"):
                    state.mark_final(
                        ctx.orgid, status="failed_discovery",
                        stage="discovery",
                        error="no portals found (cached)",
                    )
                    return False
                continue

        # --- run the stage ---
        state.mark_stage(ctx.orgid, spec.name, "in_progress")
        logger.info("[%s] stage=%s running", ctx.orgid, spec.name)
        try:
            result = spec.func(ctx)
        except Exception as err:
            if spec.name == "sheet_writer":
                state.mark_final(
                    ctx.orgid, status="failed_write",
                    stage="sheet_writer",
                    error=repr(err),
                )
            else:
                state.mark_stage(ctx.orgid, spec.name, "failed", repr(err))
            logger.exception("[%s] stage=%s failed", ctx.orgid, spec.name)
            raise

        # --- soft-stub skipped stages: no save_result, no "done" ---
        if isinstance(result, dict) and result.get("skipped"):
            # Leave the orgid_status row as whatever the previous stage set
            # (we did mark_stage in_progress above; overwrite it with a
            # neutral marker so inspect_state isn't stuck on "in_progress").
            state.mark_stage(ctx.orgid, spec.name, "skipped")
            continue

        ctx.results[spec.name] = result

        # Fix 3 — discovery runs that tripped the wall-clock budget
        # are not cached. The result-shape is the same as a normal run
        # (`portals` may be empty or partial), but we want the next
        # batch run to retry from scratch with full budget rather than
        # see the cached partial/empty result and skip. This auto-
        # retries budget-tripped OrgIDs without requiring manual purge.
        budget_tripped = (
            spec.name == "discovery"
            and isinstance(result, dict)
            and bool(result.get("completed_with_timeout"))
        )
        if spec.name not in _NEVER_CACHED and not budget_tripped:
            state.save_result(ctx.orgid, spec.name, result)
        if budget_tripped:
            logger.warning(
                "[%s] stage=%s budget tripped; result NOT cached "
                "(next run will retry)",
                ctx.orgid, spec.name,
            )

        # --- Stage A special-case: empty portals means abort ---
        if spec.name == "discovery" and not result.get("portals"):
            reason = result.get("reason") or "no portals found"
            if budget_tripped:
                # Fix 3 — non-terminal: leave the OrgID in a retryable
                # state. mark_stage (not mark_final) so completed_at
                # stays null and the orchestrator's "skip already-done"
                # check won't pick this up as finished.
                state.mark_stage(
                    ctx.orgid, "discovery", "budget_tripped",
                    error="discovery wall-clock budget exceeded",
                )
                logger.warning(
                    "[%s] discovery budget exceeded with 0 portals; "
                    "marked budget_tripped (will retry next run)",
                    ctx.orgid,
                )
            else:
                state.mark_final(
                    ctx.orgid, status="failed_discovery",
                    stage="discovery", error=reason,
                )
                logger.warning(
                    "[%s] discovery found 0 portals (%s); aborting pipeline",
                    ctx.orgid, reason,
                )
            return False

        state.mark_stage(ctx.orgid, spec.name, "done")
        logger.info("[%s] stage=%s done", ctx.orgid, spec.name)

    # --- terminal success ---
    sw_result = ctx.results.get("sheet_writer") or {}
    portals_written = int(sw_result.get("portals_written", 0))
    state.mark_final(
        ctx.orgid, status="success",
        stage="sheet_writer",
        portals_found=portals_written,
    )
    logger.info(
        "[%s] pipeline done: status=success portals_written=%d",
        ctx.orgid, portals_written,
    )
    return True

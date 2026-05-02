"""Stage B — Portal Confidence Scoring. **Stubbed for now.**

Will score each Stage A candidate on a 0-100 rule-based scale so Stage C
can pick the top-N portals worth analysing for T&C.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..pipeline import PipelineContext

logger = logging.getLogger(__name__)


def run(ctx: "PipelineContext") -> dict[str, Any]:
    logger.info("[%s] stage=confidence skipped (not yet implemented)", ctx.orgid)
    return {"skipped": True, "reason": "not yet implemented"}

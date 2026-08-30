"""Genie-V3 · Layer 1 — Model choice architecture.

A single entry point for structured LLM extraction, backed by OpenRouter through
an `instructor`-patched OpenAI client, with a hard-failover cascade across three
model tiers.

WHY A CASCADE AT ALL
--------------------
The V2 agent called one model (`google/gemini-2.5-flash`) with a hand-rolled
`requests.post` + `_extract_json()` regex parser. Two failure modes cost us real
portals in production:

  * a throttled call returned "" and the candidate was silently dropped — under
    batch load whole universities came back with 0 portals purely from 429s;
  * malformed/truncated JSON fell through the regex parser and became "no
    portals found", indistinguishable from a genuine negative.

`instructor` removes the second class (the model is forced into the Pydantic
schema and re-asked on validation failure), and the tier cascade removes the
first: any failure — validation, rate limit, transport, or an EMPTY result —
forwards the *identical* system prompt and university payload to the next model.

TIER ORDER IS EVIDENCE-BASED
----------------------------
From the 2026-08-29 benchmark over 200 orgs with human-confirmed ground truth
(only the suggestion model varied; judge held constant):

    google/gemini-3.7-flash     78.0% GT-hit   1.61 verified logins/org
    anthropic/claude-sonnet-5   76.5%          1.54
    openai/gpt-5-mini           72.0%          1.40
    qwen/qwen3.7-flash          72.0%          1.30
    google/gemini-2.5-flash     69.5%          1.20   <- the V2 default, last

so Tier 1 is the measured winner and the final tier is the strongest
independent fallback rather than a cheap one — by the time we reach Tier 3 the
first two have already failed, and correctness matters more than price.

NOTE ON THE REQUESTED TIER 3
----------------------------
`anthropic/claude-3.5-sonnet` was specified but is **not served by OpenRouter**
(checked against the live /models endpoint: the only 3.x Anthropic model is
`claude-3-haiku`). A dead tier would make the last rung of the cascade always
throw, so Tier 3 is `anthropic/claude-sonnet-5` — the same family, currently
served, and the #2 finisher in our own benchmark. Override with
`GENIE_MODEL_TIERS` if you want different IDs.

Env:
    OPENROUTER_API_KEY   required
    GENIE_MODEL_TIERS    optional, comma-separated model IDs (overrides default)
    GENIE_LLM_TIMEOUT    per-request timeout, seconds (default 90)
    GENIE_LLM_RETRIES    instructor re-asks per tier on validation error (default 1)
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence, TypeVar

import instructor
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    OpenAI,
    RateLimitError,
)
from pydantic import BaseModel, Field, ValidationError

logger = logging.getLogger("genie.layer1")

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

#: Ordered failover path. Tier 1 is the benchmark winner; each later tier is a
#: *different vendor*, so a provider-wide outage or a systematic refusal cannot
#: take out the whole cascade.
DEFAULT_MODEL_TIERS: tuple[str, ...] = (
    "google/gemini-3.7-flash",     # Tier 1 — 78.0% GT-hit, cheapest of the three
    "openai/gpt-4o-mini",          # Tier 2 — different vendor, fast, inexpensive
    "anthropic/claude-sonnet-5",   # Tier 3 — strongest fallback (76.5% measured)
)

T = TypeVar("T", bound=BaseModel)


# --------------------------------------------------------------------------- #
#  Default schema                                                             #
# --------------------------------------------------------------------------- #
class PortalCandidate(BaseModel):
    """One judged candidate. Mirrors the fields the V2 judge already returned,
    so downstream consumers (accept filters, sheet writers) need no rework."""

    url: str = Field(description="The exact login URL, absolute and https where possible.")
    is_portal: bool = Field(description="True only if this is a login for the GENERAL student body.")
    category: str = Field(
        default="",
        description=("One of: Student Portal, LMS, SSO, Student Information System, "
                     "Exam/Results, Fees, Library, Webmail, Admissions/Application, Other."),
    )
    central: bool = Field(default=True, description="Serves the whole institution, not one lab/department.")
    belongs: bool = Field(default=True, description="Belongs to THIS university (own domain or a branded tenant).")
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    reason: str = Field(default="", max_length=300)


class PortalExtraction(BaseModel):
    """Default response model: the list the cascade checks for emptiness."""

    portals: list[PortalCandidate] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
#  Errors                                                                      #
# --------------------------------------------------------------------------- #
class EmptyResultError(RuntimeError):
    """A tier returned a schema-valid response with no portals.

    Treated as a FAILURE, not an answer. A genuinely portal-less university is
    indistinguishable from a model that shrugged, and in V2 that ambiguity was
    the single largest source of false 'no portal found'. Escalating costs one
    extra call; being wrong costs a delivered org."""


class AllTiersFailedError(RuntimeError):
    """Every tier failed. Carries the per-tier cause so batch logs stay useful."""

    def __init__(self, failures: list[tuple[str, str]]):
        self.failures = failures
        detail = " | ".join(f"{m}: {e}" for m, e in failures)
        super().__init__(f"all {len(failures)} model tiers failed — {detail}")


#: Exceptions that mean "this tier is no good, try the next one". Auth errors are
#: deliberately NOT here: a bad key fails identically on every tier, so cascading
#: would just burn three calls to reach the same place.
RETRYABLE = (
    ValidationError,          # schema mismatch instructor could not repair
    RateLimitError,           # 429
    APITimeoutError,
    APIConnectionError,
    APIStatusError,           # 5xx / provider error
    EmptyResultError,
)


@dataclass
class TierAttempt:
    """Per-tier outcome, for observability (Layer 4 wants this)."""

    model: str
    ok: bool
    error: str = ""
    seconds: float = 0.0
    n_items: int = 0


@dataclass
class CascadeResult:
    """What `extract` returns: the parsed object plus how we got there."""

    data: Any
    model: str
    attempts: list[TierAttempt] = field(default_factory=list)

    @property
    def tier(self) -> int:
        return len(self.attempts)

    @property
    def escalated(self) -> bool:
        return self.tier > 1


# --------------------------------------------------------------------------- #
#  Client                                                                      #
# --------------------------------------------------------------------------- #
def _api_key() -> str:
    key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY is not set — Layer 1 cannot run.")
    return key


_client: Any = None


def get_client(force_new: bool = False) -> Any:
    """Process-wide instructor-patched OpenAI client bound to OpenRouter.

    JSON mode (not tool-calling) because the cascade spans three vendors and
    OpenRouter's tool-call translation is not uniform across them; JSON is the
    one mode all three honour identically, which keeps the tiers comparable."""
    global _client
    if _client is None or force_new:
        _client = instructor.from_openai(
            OpenAI(
                base_url=OPENROUTER_BASE_URL,
                api_key=_api_key(),
                timeout=float(os.getenv("GENIE_LLM_TIMEOUT", "90")),
                max_retries=0,      # the cascade owns retrying, not the SDK
                default_headers={
                    "HTTP-Referer": "https://github.com/reclaimprotocol",
                    "X-Title": "Genie-V3 portal discovery",
                },
            ),
            mode=instructor.Mode.JSON,
        )
    return _client


def model_tiers() -> tuple[str, ...]:
    raw = os.getenv("GENIE_MODEL_TIERS", "").strip()
    if raw:
        tiers = tuple(m.strip() for m in raw.split(",") if m.strip())
        if tiers:
            return tiers
    return DEFAULT_MODEL_TIERS


#: Field names that hold the portal list, across the schemas we ship.
#: `PortalExtraction` uses `portals`; `schemas.IntegratedDiscoveryOutput` uses
#: `discovered_portals`. Miss one and empty-escalation silently never fires for
#: that schema — the failure mode this cascade exists to prevent.
_PORTAL_LIST_FIELDS = ("portals", "discovered_portals")


def _portal_items(obj: Any) -> list | None:
    for name in _PORTAL_LIST_FIELDS:
        items = getattr(obj, name, None)
        if isinstance(items, list):
            return items
    return None


def _default_is_empty(obj: Any) -> bool:
    """Empty = the schema's portal list came back with nothing in it.

    Only applies when the schema actually exposes one of `_PORTAL_LIST_FIELDS`;
    a caller passing an unrelated schema is never judged 'empty' by accident."""
    items = _portal_items(obj)
    return items is not None and len(items) == 0


def _payload(university: str | dict[str, Any] | None, candidate_links: Sequence[Any]) -> str:
    """Render the user turn. Identical bytes are sent to every tier — that is the
    whole point of the cascade, so a tier change never silently changes the ask."""
    import json

    if isinstance(university, dict):
        uni = json.dumps(university, ensure_ascii=False)
    else:
        uni = str(university or "")
    links = json.dumps(list(candidate_links), ensure_ascii=False, default=str)
    return f"UNIVERSITY:\n{uni}\n\nCANDIDATE_LINKS:\n{links}"


# --------------------------------------------------------------------------- #
#  The cascade                                                                 #
# --------------------------------------------------------------------------- #
def extract(
    system_prompt: str,
    candidate_links: Sequence[Any],
    schema: type[T] = PortalExtraction,  # type: ignore[assignment]
    *,
    university: str | dict[str, Any] | None = None,
    tiers: Sequence[str] | None = None,
    is_empty: Callable[[Any], bool] | None = None,
    max_retries: int | None = None,
    temperature: float = 0.0,
) -> CascadeResult:
    """Run `system_prompt` + `candidate_links` through the tier cascade.

    Returns the first schema-valid, NON-EMPTY result. On validation error, rate
    limit, transport error, or an empty portals list, the *exact same* system
    prompt and university payload go to the next model — nothing is re-worded
    between tiers, so an escalation changes only the model.

    Raises `AllTiersFailedError` when every tier fails, with the per-tier cause.
    """
    import time

    client = get_client()
    path = tuple(tiers) if tiers else model_tiers()
    empty_check = is_empty or _default_is_empty
    retries = max_retries if max_retries is not None else int(os.getenv("GENIE_LLM_RETRIES", "1"))

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": _payload(university, candidate_links)},
    ]

    attempts: list[TierAttempt] = []
    failures: list[tuple[str, str]] = []

    for idx, model in enumerate(path, start=1):
        t0 = time.monotonic()
        try:
            result = client.chat.completions.create(
                model=model,
                response_model=schema,
                messages=messages,
                max_retries=retries,
                temperature=temperature,
            )
            if empty_check(result):
                raise EmptyResultError(f"{model} returned 0 portals")

            n = len(_portal_items(result) or [])
            attempts.append(TierAttempt(model, True, "", round(time.monotonic() - t0, 2), n))
            if idx > 1:
                logger.info("layer1: tier %d (%s) succeeded after %d failure(s)",
                            idx, model, idx - 1)
            return CascadeResult(data=result, model=model, attempts=attempts)

        except AuthenticationError as exc:
            # Same key on every tier — cascading cannot help. Fail loudly.
            raise RuntimeError(f"OpenRouter rejected OPENROUTER_API_KEY: {exc}") from exc

        except RETRYABLE as exc:
            why = f"{type(exc).__name__}: {str(exc)[:180]}"
            attempts.append(TierAttempt(model, False, why, round(time.monotonic() - t0, 2)))
            failures.append((model, why))
            logger.warning("layer1: tier %d/%d (%s) failed -> %s",
                           idx, len(path), model, why)
            continue

        except Exception as exc:  # noqa: BLE001 — instructor wraps provider errors
            # instructor raises InstructorRetryException once its own re-asks are
            # exhausted; it is not an openai error type, so catch broadly and
            # keep cascading rather than aborting the whole row.
            why = f"{type(exc).__name__}: {str(exc)[:180]}"
            attempts.append(TierAttempt(model, False, why, round(time.monotonic() - t0, 2)))
            failures.append((model, why))
            logger.warning("layer1: tier %d/%d (%s) failed -> %s",
                           idx, len(path), model, why)
            continue

    raise AllTiersFailedError(failures)


def extract_or_none(*args: Any, **kwargs: Any) -> CascadeResult | None:
    """`extract` that returns None instead of raising when all tiers fail.

    For batch runners, where one dead university must never kill the shard."""
    try:
        return extract(*args, **kwargs)
    except AllTiersFailedError as exc:
        logger.error("layer1: %s", exc)
        return None

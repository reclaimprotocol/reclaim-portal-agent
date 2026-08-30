"""Genie-V3 · Cascading inference layer with the Multi-Stage Waterfall prompt.

Takes the filtered candidate list from `agent/filters.py` and asks a model to do
two things in ONE pass: identify the student portals, and map each one to the
legal documents that govern it.

TWO SPEC VALUES CORRECTED HERE — both verified against the live API, and both
would have failed in ways that are easy to misread:

1.  `base_url="https://openrouter.ai"` does not work. That host serves the
    marketing site; `GET /models` returns 200 so it *looks* alive, but the SDK
    appends `/chat/completions` and gets HTML back, surfacing as a baffling
    `AttributeError: 'str' object has no attribute 'choices'`. The API lives at
    `https://openrouter.ai/api/v1`, which returns a clean completion.

2.  `anthropic/claude-3-5-sonnet-latest` is not served by OpenRouter (nor is
    `anthropic/claude-3.5-sonnet`). Tier 3 would have thrown on every single
    call — and because the cascade swallows tier failures, the symptom would be
    a quiet drop in quality, not an error. Tier 3 is `anthropic/claude-sonnet-5`,
    which measured 76.5% against human ground truth in our 200-org benchmark,
    second only to the tier-1 model. Override with GENIE_MODEL_TIERS.

Relationship to `agent/llm_router.py`: that module is the generic, schema-
agnostic cascade. This one is the portal-discovery-specific orchestrator with
the waterfall prompt baked in. They deliberately share no code so this file
reads as a single self-contained unit; if that duplication becomes a
maintenance problem, this is the one to fold into llm_router.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Sequence

import instructor
from openai import OpenAI

from agent.schemas import IntegratedDiscoveryOutput

logger = logging.getLogger("genie.cascade")

# --------------------------------------------------------------------------- #
#  Client                                                                      #
# --------------------------------------------------------------------------- #
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"   # NOT bare openrouter.ai

client = instructor.from_openai(
    OpenAI(
        base_url=OPENROUTER_BASE_URL,
        api_key=os.getenv("OPENROUTER_API_KEY"),
        timeout=float(os.getenv("GENIE_LLM_TIMEOUT", "90")),
        max_retries=0,          # the cascade owns retrying, not the SDK
        default_headers={
            "HTTP-Referer": "https://github.com/reclaimprotocol",
            "X-Title": "Genie-V3 portal discovery",
        },
    ),
    # MD_JSON, not JSON. Verified 2026-08-31 across all three tiers: Claude
    # returns its object wrapped in a ```json ... ``` markdown fence, which
    # Mode.JSON cannot parse — so tier 3, our strongest fallback, failed on
    # EVERY call. Because the cascade swallows tier failures, that surfaced as
    # "all 3 tiers exhausted" rather than an error, and the row was written off
    # as having no portals. MD_JSON strips the fence and all three tiers pass.
    mode=instructor.Mode.MD_JSON,
)

#: Ordered failover path. Each tier is a DIFFERENT vendor, so a provider-wide
#: outage or a systematic refusal cannot take out the whole cascade.
MODEL_CASCADE: list[str] = [
    "google/gemini-3.7-flash",     # 78.0% GT-hit in the 200-org benchmark
    "openai/gpt-4o-mini",          # different vendor, fast, inexpensive
    "anthropic/claude-sonnet-5",   # 76.5% — strongest fallback, see module docs
]
if os.getenv("GENIE_MODEL_TIERS", "").strip():
    MODEL_CASCADE = [m.strip() for m in os.environ["GENIE_MODEL_TIERS"].split(",") if m.strip()]


# --------------------------------------------------------------------------- #
#  Multi-Stage Waterfall System Prompt                                         #
# --------------------------------------------------------------------------- #
#  Model-agnostic on purpose: the identical bytes go to Gemini, GPT and Claude,
#  so an escalation changes only the model, never the ask. The stage names are
#  the EXACT Literal values in AutomatedComplianceMetrics — any paraphrase fails
#  schema validation and burns a tier.
WATERFALL_SYSTEM_PROMPT = """You are a university student-portal discovery and compliance-mapping agent.

You receive one university and a pre-filtered list of links harvested from its
website. Each link has a `url`, the anchor `text` it was published under, and a
`kind` hint of "portal" or "legal". The hint is advisory — judge the link
yourself.

## TASK 1 — Identify functional student portals

Return every DISTINCT login system used by the GENERAL ENROLLED STUDENT BODY.
Classify each into exactly one `category`:

  - "ERP"                   full academic/administrative system or Student
                            Information System (records, registration,
                            attendance): Samarth, MasterSoft, TOTVS RM, Jacad,
                            TCS iON, BIIS-style institutional systems.
  - "LMS (Moodle/Canvas)"   learning platform: Moodle, Canvas, Blackboard,
                            Chamilo, or a local "AVA" / "Aula Virtual" /
                            "campus virtual" / "VLE".
  - "Fee Payment"           the institution's own fee/tuition portal reached
                            behind a student login.
  - "UMS/MIS"               University/Management Information System: results,
                            marks, exam and hall-ticket portals.
  - "General Student Login" a central student login or SSO fronting several
                            services, fitting none of the above.

Set `exact_url` to the precise login endpoint, not a homepage. Preserve any
routing fragment (e.g. `#/login`, `#!/login/tenant`) — removing it breaks the
address. Set `portal_system_name` to the product or platform a student would
name. Set `confidence_score` honestly: >=0.9 only with direct evidence, <0.5
when the portal may serve a single department, may belong to another
institution, or when you are unsure.

REJECT and do NOT return: admissions/applicant portals for PROSPECTIVE
students, staff/faculty/HR logins, alumni and careers portals, CMS admin
backends (wp-admin), publisher or database SSO (Elsevier, JSTOR, EBSCO), pure
third-party payment gateways, and bare SAML/IdP metadata endpoints.

## TASK 2 — Map each portal to its governing legal pages

For EVERY portal you return, populate `compliance_metrics` by finding its Terms
& Conditions and Privacy Policy. Work the four stages IN ORDER and STOP at the
first stage that yields a real URL. Record which stage produced the hit in
`waterfall_discovery_stage`, using EXACTLY one of these strings:

  "Stage 1: Direct Page Link"
      A legal link present on, or published alongside, the portal's own landing
      page — same host, or linked in its footer. Strongest evidence: these terms
      demonstrably govern this portal.

  "Stage 2: Domain Trim Root"
      Strip the portal host one label at a time toward its parent root and look
      for legal pages there. For `biis.buet.ac.bd`, try `buet.ac.bd`. Recurse
      level by level; stop at the registrable root, never beyond it.

  "Stage 3: Vendor Cross-Reference"
      When the portal sits on a third-party SaaS tenant (e.g. `*.jacad.com.br`,
      `*.samarth.edu.in`, `*.cloudtotvs.com.br`, `*.arellanolms.com`,
      `*.moodle.com`), use that VENDOR's corporate terms/privacy pages. Legit
      when the university publishes none of its own for the hosted system.

  "Stage 4: University Root Rescue"
      Last resort: the apex university website's own terms/privacy. Weakest
      evidence — it may not mention the portal at all — so prefer any earlier
      stage, and flag it by recording this stage honestly.

  "None Found"
      No legal URL was located. `tnc_url` and `privacy_policy_url` must BOTH be
      null in this case.

`tnc_url` and `privacy_policy_url` are SEPARATE documents. Do not copy one into
the other unless a single published page genuinely serves both purposes.

## TERMINAL GUARDRAIL — do not invent URLs

You may ONLY return a legal URL that appears in the supplied candidate list, or
that is the documented corporate legal page of a third-party SaaS vendor you
recognise under Stage 3. You must NOT construct, guess, complete, or infer a
path. Do not append `/privacy`, `/terms`, `/privacy-policy` or any similar
suffix to a domain on the assumption that it probably exists.

If the provided list contains no legal URL for a portal, you MUST set
`tnc_url` = null, `privacy_policy_url` = null, and
`waterfall_discovery_stage` = "None Found".

A truthful null is correct and expected. A plausible-looking fabricated URL is
a FAILURE — it is worse than returning nothing, because it is shipped to a
customer as verified fact.

## OUTPUT

Echo `org_id`, `university_name` and `official_domain` back exactly as given.
Return an empty `discovered_portals` list only if genuinely nothing qualifies —
never pad it with homepages or guesses to appear thorough.
"""


def _build_user_payload(
    org_id: str,
    university_name: str,
    official_domain: str,
    filtered_candidates_list: Sequence[dict[str, Any]],
) -> str:
    """Render the user turn. Built ONCE and reused verbatim across every tier,
    so an escalation changes the model and nothing else."""
    slim = [
        {"url": c.get("url", ""), "text": (c.get("text") or "")[:160],
         "kind": c.get("kind", "")}
        for c in filtered_candidates_list
        if c.get("url")
    ]
    return (
        f"ORG_ID: {org_id}\n"
        f"UNIVERSITY_NAME: {university_name}\n"
        f"OFFICIAL_DOMAIN: {official_domain}\n\n"
        f"CANDIDATE_LINKS ({len(slim)}):\n"
        f"{json.dumps(slim, ensure_ascii=False, indent=1)}"
    )


def _empty_result(org_id: str, university_name: str,
                  official_domain: str) -> IntegratedDiscoveryOutput:
    """Baseline schema block returned when every tier fails.

    The pipeline must keep moving: one unreachable university cannot be allowed
    to halt a batch of four thousand."""
    return IntegratedDiscoveryOutput(
        org_id=org_id,
        university_name=university_name,
        official_domain=official_domain,
        discovered_portals=[],
    )


def execute_model_cascade(
    org_id: str,
    university_name: str,
    official_domain: str,
    filtered_candidates_list: Sequence[dict[str, Any]],
    *,
    models: Sequence[str] | None = None,
    trace: dict[str, Any] | None = None,
) -> IntegratedDiscoveryOutput:
    """Run the waterfall prompt down the model cascade; first good answer wins.

    Escalates to the next model on ANY of: network/transport exception, schema
    validation failure, or a completely empty `discovered_portals` array.

    That last trigger is deliberate. A model that shrugged and a university that
    genuinely has no portal produce byte-identical output, and in V2 that
    ambiguity was our largest source of false "no portal found". Escalating
    costs one extra call; being wrong costs a delivered org.

    Returns a baseline empty schema block if every tier fails — never raises.
    """
    path = list(models) if models else MODEL_CASCADE
    # A plain mutable dict is used rather than a return-value change so the
    # signature stays as specified. asyncio.to_thread copies the context, so a
    # contextvar would NOT propagate back out of the worker thread; a dict does.
    if trace is not None:
        trace.update({"tier": 0, "model": "", "escalated": False, "failures": []})
    user_payload = _build_user_payload(
        org_id, university_name, official_domain, filtered_candidates_list
    )
    messages = [
        {"role": "system", "content": WATERFALL_SYSTEM_PROMPT},
        {"role": "user", "content": user_payload},
    ]

    for tier, model_slug in enumerate(path, start=1):
        try:
            result = client.chat.completions.create(
                model=model_slug,
                response_model=IntegratedDiscoveryOutput,
                messages=messages,
                max_retries=int(os.getenv("GENIE_LLM_RETRIES", "1")),
                temperature=0.0,
            )

            if not result.discovered_portals:
                raise ValueError("model returned an empty discovered_portals array")

            if trace is not None:
                trace.update({"tier": tier, "model": model_slug,
                              "escalated": tier > 1})
            if tier > 1:
                logger.warning("[API CASCADE ESCALATION] tier %d (%s) answered "
                               "after %d failure(s)", tier, model_slug, tier - 1)
            return result

        except Exception as exc:  # noqa: BLE001 — any failure must cascade, not abort
            why = f"{type(exc).__name__}: {str(exc)[:180]}"
            if trace is not None:
                trace["failures"].append({"model": model_slug, "error": why})
            logger.warning("[API CASCADE ESCALATION] tier %d/%d '%s' failed for "
                           "org %s: %s", tier, len(path), model_slug, org_id, why)
            continue

    logger.error("[API CASCADE ESCALATION] all %d tiers exhausted for org %s (%s) "
                 "— returning empty baseline schema", len(path), org_id, university_name)
    return _empty_result(org_id, university_name, official_domain)

"""Genie-V3 · Data contract for OpenRouter / Instructor calls.

These Pydantic models are not just validation — with `instructor` they ARE the
prompt. Every `Field(description=...)` is serialised into the JSON schema the
model is forced to fill, so the descriptions below carry the rules we learned
the hard way in V2 (prefer the exact login endpoint over a homepage; a shared
vendor tenant still belongs to the university; a T&C found by trimming to the
root is weaker evidence than one linked from the portal itself).

Keep descriptions specific and behavioural. Vague ones produce vague output.
"""
from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field

# --------------------------------------------------------------------------- #
#  Compliance / legal-page discovery                                           #
# --------------------------------------------------------------------------- #
#: The four levels of the T&C waterfall, in the order they are attempted. Which
#: stage produced a hit is provenance, not trivia: a Stage-1 link found on the
#: portal page itself is strong evidence the terms actually govern that portal,
#: while a Stage-4 root rescue is a best-effort fallback that human review
#: rejects far more often. Downstream ranking and QA both depend on this.
WaterfallStage = Literal[
    "Stage 1: Direct Page Link",
    "Stage 2: Domain Trim Root",
    "Stage 3: Vendor Cross-Reference",
    "Stage 4: University Root Rescue",
    "None Found",
]


class AutomatedComplianceMetrics(BaseModel):
    """Legal pages governing a single portal, plus how they were found."""

    tnc_url: Optional[str] = Field(
        default=None,
        description=(
            "Absolute URL of the Terms & Conditions / Terms of Use / Terms of "
            "Service page that governs THIS portal, resolved by the waterfall "
            "rules. Must be the terms document itself, not a page that merely "
            "links to it, and not a cookie banner or accessibility statement. "
            "For a portal hosted on a third-party SaaS tenant, the vendor's "
            "terms are acceptable when the university publishes none. Use null "
            "when no terms page was verified — never guess a plausible URL."
        ),
    )
    privacy_policy_url: Optional[str] = Field(
        default=None,
        description=(
            "Absolute URL of the Privacy Policy / Privacy Notice / Data "
            "Protection page governing THIS portal (e.g. 'Política de "
            "Privacidade', 'Aviso de Privacidad', 'LGPD'). This is a SEPARATE "
            "document from the terms; do not repeat tnc_url here unless the "
            "single published page genuinely covers both. Use null when none "
            "was verified."
        ),
    )
    waterfall_discovery_stage: WaterfallStage = Field(
        description=(
            "Which stage of the T&C waterfall produced the legal link(s), i.e. "
            "how much confidence the provenance deserves. "
            "'Stage 1: Direct Page Link' — linked directly from the portal page "
            "or its footer (strongest). "
            "'Stage 2: Domain Trim Root' — found by trimming the portal URL to "
            "its host/root and looking there. "
            "'Stage 3: Vendor Cross-Reference' — taken from the third-party SaaS "
            "vendor that hosts the portal (e.g. Samarth, TOTVS RM, Jacad, "
            "Moodle), matched via the vendor whitelist. "
            "'Stage 4: University Root Rescue' — last-resort fallback to the "
            "university's main website when the portal's own domain yields "
            "nothing (weakest; verify before shipping). "
            "'None Found' — nothing verified; tnc_url and privacy_policy_url "
            "must both be null in that case."
        ),
    )


# --------------------------------------------------------------------------- #
#  A single discovered portal                                                  #
# --------------------------------------------------------------------------- #
PortalCategory = Literal[
    "ERP",
    "LMS (Moodle/Canvas)",
    "Fee Payment",
    "UMS/MIS",
    "General Student Login",
]


class StudentPortalLink(BaseModel):
    """One verified student-facing login portal for a university."""

    category: PortalCategory = Field(
        description=(
            "What kind of system this login fronts. "
            "'ERP' — full academic/administrative ERP or Student Information "
            "System (admissions-to-alumni records, e.g. Samarth, MasterSoft, "
            "TOTVS RM, Jacad). "
            "'LMS (Moodle/Canvas)' — learning platform: Moodle, Canvas, "
            "Blackboard, Chamilo, an 'AVA'/'Aula Virtual'/'campus virtual'. "
            "'Fee Payment' — the institution's fee/tuition portal reached "
            "behind a student login (NOT a bare third-party checkout gateway). "
            "'UMS/MIS' — University/Management Information System: results, "
            "attendance, exam and marks portals. "
            "'General Student Login' — a central student login or SSO that "
            "fronts several services and fits none of the above. "
            "Pick the single best fit; if a portal spans several, choose the "
            "one a student would primarily use it for."
        ),
    )
    exact_url: str = Field(
        description=(
            "The EXACT login endpoint, absolute and https where available — the "
            "page carrying the username/password form (e.g. "
            "'https://biis.buet.ac.bd/BIIS_WEB/Login.do'), not the university "
            "homepage and not a landing page that links to the login. If the "
            "entry URL redirects, give the URL a student finally lands on. "
            "Preserve any fragment that routes a single-page app to its login "
            "(e.g. '/#/login/{tenant}') — stripping it breaks the link."
        ),
    )
    portal_system_name: str = Field(
        description=(
            "Human-readable name of the product or platform behind the login, "
            "as a student or administrator would call it — e.g. 'Samarth eGov', "
            "'Moodle', 'TOTVS RM Portal do Aluno', 'Jacad', 'TCS iON', "
            "'MasterSoft', 'Canvas', 'Microsoft Azure AD SSO'. If the vendor is "
            "genuinely unidentifiable, use the institution's own name for it "
            "(e.g. 'BIIS — BUET Integrated Information System'). Never leave "
            "this blank and never invent a vendor you have no evidence for."
        ),
    )
    compliance_metrics: AutomatedComplianceMetrics = Field(
        description=(
            "The Terms and Privacy pages governing this specific portal, plus "
            "the waterfall stage that produced them. Populate per-portal: two "
            "portals of the same university often sit on different domains and "
            "are governed by different documents."
        ),
    )
    confidence_score: float = Field(
        ge=0.0,
        le=1.0,
        description=(
            "How confident you are that this is a genuine login for the GENERAL "
            "enrolled student body of THIS university, from 0.0 to 1.0. "
            "Use >=0.9 only with direct evidence (a visible password field, or "
            "an unmistakable branded tenant of a known platform). Use 0.6-0.8 "
            "for a strong but indirect signal such as a login-shaped URL that "
            "responds. Use <0.5 when the portal may serve only one "
            "department/lab, may belong to a different institution, or when you "
            "are guessing — a low score is far more useful than false certainty."
        ),
    )


# --------------------------------------------------------------------------- #
#  Master output                                                               #
# --------------------------------------------------------------------------- #
class IntegratedDiscoveryOutput(BaseModel):
    """Everything the agent concluded about one university, in one object."""

    org_id: str = Field(
        description=(
            "The organisation's ID exactly as supplied in the input payload "
            "(SheerID OrgID, e.g. '661700'). Echo it back verbatim — it is the "
            "join key used to write results back to the sheet. Never invent, "
            "reformat, or zero-pad it."
        ),
    )
    university_name: str = Field(
        description=(
            "The institution's official name as supplied in the input payload, "
            "echoed back unchanged, including any non-Latin script or "
            "diacritics (e.g. 'Universidade Federal do Paraná', "
            "'جامعة سفنكس'). Do not translate, expand, or abbreviate it."
        ),
    )
    official_domain: str = Field(
        description=(
            "The university's primary email/website domain as supplied, bare "
            "host with no scheme or trailing slash (e.g. 'buet.ac.bd'). This is "
            "the reference used to decide whether a portal is on-domain or on a "
            "third-party vendor tenant."
        ),
    )
    discovered_portals: List[StudentPortalLink] = Field(
        default_factory=list,
        description=(
            "Every distinct student login portal verified for this university. "
            "One entry per DISTINCT system — collapse pure path variants of the "
            "same login ('/', '/login', '/login/index.php' on one host) into a "
            "single entry, but keep genuinely separate systems on a shared host "
            "as separate entries. Return an empty list only when nothing "
            "qualifies; never pad the list with guesses, homepages, admission "
            "portals, or staff-only logins to appear thorough."
        ),
    )

"""Genie-V3 · Data contract for OpenRouter / Instructor calls.

The model is now a pure EXTRACTION parser. It reports what it sees in the
crawled payload as two flat, unlinked sets — portals here, legal links there —
and does not decide which terms govern which portal.

That mapping moved to `agent/graph_matcher.py`, a deterministic weighted
bipartite matcher. The reason is measurable: over 200 human-reviewed orgs the
model-driven waterfall returned `None Found` for 96 of 149 portals, and where it
did answer it leaned on Stage 4 (apex-root rescue) — a guess that human review
rejects more often than any other stage. Association is a scoring problem with
an exact optimum, not a judgement call, and a model asked to do both jobs at
once does the second one badly.

With `instructor`, every `Field(description=...)` is serialised into the JSON
schema the model must fill, so the descriptions below are the prompt. Keep them
behavioural.
"""
from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field

PortalCategory = Literal[
    "ERP",
    "LMS (Moodle/Canvas)",
    "Fee Payment",
    "UMS/MIS",
    "General Student Login",
]


class DiscoveredPortal(BaseModel):
    """One student login endpoint, as observed. No compliance mapping."""

    category: PortalCategory = Field(
        description=(
            "What kind of system this login fronts. "
            "'ERP' — academic/administrative system or Student Information "
            "System (Samarth, MasterSoft, TOTVS RM, Jacad, TCS iON). "
            "'LMS (Moodle/Canvas)' — Moodle, Canvas, Blackboard, Chamilo, or a "
            "local 'AVA'/'Aula Virtual'/'campus virtual'/'VLE'. "
            "'Fee Payment' — the institution's fee/tuition portal behind a "
            "student login. "
            "'UMS/MIS' — results, marks, attendance, exam and hall-ticket "
            "portals. "
            "'General Student Login' — a central student login or SSO fronting "
            "several services. Pick the single best fit."
        ),
    )
    exact_url: str = Field(
        description=(
            "The login URL EXACTLY as it appears in the supplied candidate "
            "list — copy it verbatim, do not normalise, shorten, or reconstruct "
            "it. Preserve any routing fragment ('#/login', '#!/login/tenant'): "
            "removing it breaks the address on SPA platforms."
        ),
    )
    portal_system_name: str = Field(
        description=(
            "Product or platform a student would name: 'Samarth eGov', "
            "'Moodle', 'TOTVS RM Portal do Aluno', 'TCS iON', 'Canvas', "
            "'Azure AD SSO'. If genuinely unidentifiable, use the "
            "institution's own name for it. Never invent a vendor."
        ),
    )


class HarvestedLegalLink(BaseModel):
    """A legal/policy link seen in the payload. Unlinked to any portal."""

    url: str = Field(
        description=(
            "URL of a Terms & Conditions, Terms of Use, Privacy Policy, Data "
            "Protection, Disclaimer or Cookie-Policy page, copied VERBATIM "
            "from the candidate list. Report every such link you find, even if "
            "you cannot tell which portal it governs — deciding that is not "
            "your job. Never construct or guess a path such as '/privacy'."
        ),
    )
    anchor_text: str = Field(
        description=(
            "The visible link text exactly as published ('Privacy Policy', "
            "'Política de Privacidade', 'Termos de Uso', 'Điều khoản sử dụng'). "
            "This wording is scored downstream, so preserve the original "
            "language and do not translate it. Empty string if unlabelled."
        ),
    )
    is_primary_compliance_target: bool = Field(
        default=False,
        description=(
            "True ONLY if this document, read in its own language, is a core "
            "Privacy Policy / Data Protection notice or Terms of Service / "
            "Terms of Use / Conditions of Use. "
            "False for everything adjacent: refund, cancellation, return, "
            "shipping, billing, cookie notices, accessibility statements, "
            "sitemaps and help pages. You are the only component that can read "
            "the language, so this flag is the judgement being asked of you — "
            "a refund policy and a privacy policy both contain the word "
            "'policy' and are indistinguishable to a downstream regex."
        ),
    )
    detected_native_keyword: Optional[str] = Field(
        default=None,
        description=(
            "The EXACT raw phrase, in its original language and script, that "
            "made you classify this as a legal document — copied verbatim from "
            "the link text or URL: 'Chính sách bảo mật', 'นโยบายความเป็นส่วนตัว', "
            "'Kebijakan Privasi', 'Aviso de Privacidad', 'Datenschutz'. "
            "Do NOT translate it and do not invent a phrase that is not there. "
            "These terms are harvested into the agent's dictionary, so a "
            "language seen once is recognised locally ever after. Null if the "
            "link was labelled only in English or was unlabelled."
        ),
    )


class IntegratedDiscoveryOutput(BaseModel):
    """Two flat sets. Association happens later, in the graph matcher."""

    org_id: str = Field(
        description=(
            "The organisation ID exactly as supplied in the payload (e.g. "
            "'661700'). Echo it back verbatim — it is the join key for writing "
            "results back. Never invent, reformat or zero-pad it."
        ),
    )
    university_name: str = Field(
        description=(
            "Institution name as supplied, echoed unchanged, including "
            "non-Latin script and diacritics ('Universidade Federal do Paraná', "
            "'جامعة سفنكس'). Do not translate or abbreviate."
        ),
    )
    official_domain: str = Field(
        description=(
            "The university's primary domain as supplied — bare host, no "
            "scheme, no trailing slash (e.g. 'buet.ac.bd')."
        ),
    )
    discovered_portals: List[DiscoveredPortal] = Field(
        default_factory=list,
        description=(
            "Every DISTINCT student login system in the payload. Collapse pure "
            "path variants of one login on a host ('/', '/login', "
            "'/login/index.php') into a single entry, but keep genuinely "
            "separate systems on a shared host as separate entries. Empty list "
            "if nothing qualifies — never pad with homepages or guesses."
        ),
    )
    harvested_legal_links: List[HarvestedLegalLink] = Field(
        default_factory=list,
        description=(
            "Every legal/policy link in the payload, flat and UNLINKED. Do not "
            "attempt to pair these with portals and do not filter them by which "
            "portal you think they belong to — report them all; a downstream "
            "graph matcher scores the associations. Empty list if none appear."
        ),
    )

"""Typed configuration loaded from environment (.env)."""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent

load_dotenv(ROOT / ".env")

logger = logging.getLogger(__name__)

DOMAIN_OVERRIDES_PATH = ROOT / "domain_overrides.json"


# --- Stage A / Stage C performance budgets -------------------------------
#
# Tuned against the SPPU baseline (~3 min Stage A → <60s target). Per-request
# timeouts trade off recall (longer = waits out flaky Indian-uni hosts) against
# latency (shorter = the fastest p95). These values keep recall on the SPPU
# golden set while bringing total Stage A under 60s.
HTTP_TIMEOUT_SECONDS: int = 8
DUCKDUCKGO_TIMEOUT_SECONDS: int = 12
JS_RENDERING_TIMEOUT_SECONDS: int = 15
# Fix 1 — bumped from 90s. Multi-campus college groups (LNCT,
# Manipal, Amity, Symbiosis) routinely hit 100-120s Stage A even with
# the MAX_SIBLING_ROOTS_TO_PROBE=3 cap. Env-overridable so per-batch
# tuning is possible without code edits. `load_dotenv` runs above so
# the .env value is honoured.
TOTAL_DISCOVERY_BUDGET_SECONDS: int = int(
    os.environ.get("TOTAL_DISCOVERY_BUDGET_SECONDS", "150")
)
TOTAL_TC_BUDGET_SECONDS: int = int(
    os.environ.get("TOTAL_TC_BUDGET_SECONDS", "60")
)


# --- Stage A search engine selection (Gemini Pro via OpenRouter) ---------
#
# When `GEMINI_SEARCH_ENABLED=true` AND `OPENROUTER_API_KEY` is set, the
# discovery search phase asks Gemini for the university's portal URLs
# first; DDG only runs when Gemini returns zero candidates (or is
# disabled, or the OpenRouter call errors). Gemini's index covers the
# long tail of smaller Indian universities (St. Xavier's Ranchi,
# Patliputra, …) that DDG misses.
#
# Read from env directly (rather than threaded through `Config`) because
# `gemini_search` in `discovery_rules` reads them at call time — same
# pattern as the budget / timeout constants above. Disabling is a
# zero-cost no-op: when the key is missing the function returns []
# immediately and the orchestrator falls through to DDG.
OPENROUTER_API_KEY: str = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL: str = os.getenv(
    "OPENROUTER_MODEL", "google/gemini-2.0-flash-001"
)
GEMINI_SEARCH_ENABLED: bool = os.getenv(
    "GEMINI_SEARCH_ENABLED", "true"
).lower() == "true"


# --- Known shared-platform short-circuit ---------------------------------
#
# When a candidate URL host ends with one of these patterns, validation is
# skipped entirely and the candidate is accepted with the cached category.
# These platforms are verified across many universities; running validation
# again (HTTP fetch + Playwright fallback for SPA tenants) is wasted work.
KNOWN_SHARED_PLATFORM_PATTERNS: dict[str, dict[str, Any]] = {
    "samarth.edu.in": {"category": "Student Portal", "validated": True},
    "samarth.ac.in": {"category": "Student Portal", "validated": True},
    "digitaluniversity.ac": {"category": "Student Portal", "validated": True},
    "digitaluniversity.ac.in": {"category": "Student Portal", "validated": True},
    "myloft.xyz": {"category": "Library", "validated": True},
    # Bug B — knimbus.com tenants (`<inst>.knimbus.com`) follow a
    # documented login URL shape `/portal/v2/default/login` but the
    # platform's wildcard router redirects unauthenticated requests to
    # `/portal/v2/default/landingPage#/?signin=true`. The redirect
    # destination is uglier and not a stable bookmarkable URL —
    # `canonical_path` tells the URL-normalisation layer to rewrite the
    # stored path back to the login form.
    "knimbus.com": {
        "category": "Library",
        "validated": True,
        "canonical_path": "/portal/v2/default/login",
    },
    "cognibot.in": {"category": "LMS/Moodle", "validated": True},
    # Bihar state-government UMS — university subdomains like
    # `pu.bihar-ums.com/login` (Patna University). Hard verification still
    # applies (DNS, status, body) — adding it here exempts these hosts
    # from the off-domain filter so an organically-discovered URL gets
    # validated rather than dropped.
    "bihar-ums.com": {"category": "Student Portal", "validated": True},
    # Edumarshal — third-party multi-tenant ERP used by many Indian
    # colleges (MAIT and similar). Tenants are reached via either the
    # bare `app.edumarshal.com` / `beta.edumarshal.com` shells or
    # institution-prefixed subdomains. The `endswith` match in
    # `host_is_known_shared_platform` covers all of them with a single
    # entry; rule-C accepts the host without requiring a static login
    # form (the platform is JS-rendered).
    "edumarshal.com": {"category": "Student Portal", "validated": True},
}


# Stage C — curated path list for the university-level T&C fallback.
# Tried in order against the university root; first one passing the strict
# validation in `tc_finder._validate_university_tc_url` wins. Order encodes
# specificity (T&C-specific → privacy → disclaimer) plus CMS variants for
# DU/NIC-built sites and older Indian-uni "/disclaimer.html" patterns.
UNIVERSITY_TC_FALLBACK_PATHS: tuple[str, ...] = (
    # ---- Terms and conditions variants ------------------------------
    # Hyphenated, underscored, no-separator, with `.html` / `.php`
    # extensions. Indian-uni CMS templates (Drupal, WordPress, Joomla,
    # NIC-built sites, hand-rolled PHP) all pick a different shape.
    "/terms-and-conditions",
    "/terms-and-conditions.html",
    "/terms-and-conditions.php",
    "/terms-conditions",
    "/terms-conditions.html",
    "/terms-conditions.php",
    "/terms-condition",
    "/terms-condition.html",       # GLS University pattern
    "/terms-condition.php",
    "/terms_conditions",
    "/terms_conditions.html",
    "/terms_and_conditions",
    "/terms_and_conditions.html",
    "/terms-of-use",
    "/terms-of-use.html",
    "/terms-of-service",
    "/terms-of-service.html",
    "/terms",
    "/terms.html",
    "/terms.php",
    "/tos",
    "/tnc",
    "/tnc.html",
    "/tnc.php",
    "/t-and-c",
    "/t-and-c.html",
    "/en/page/terms-condition",
    "/en/page/terms-conditions",

    # ---- Privacy policy variants ------------------------------------
    "/privacy-policy",
    "/privacy-policy.html",
    "/privacy-policy.php",
    "/privacy_policy",
    "/privacy_policy.html",
    "/privacy-statement",
    "/privacy-statement.html",
    "/privacy",
    "/privacy.html",
    "/privacy.php",
    "/en/page/privacy-policy",

    # ---- Disclaimer variants ----------------------------------------
    "/disclaimer",
    "/disclaimer.html",
    "/disclaimer.php",
    "/disclaimers",
    "/disclaimers.html",
    "/website-disclaimer",
    "/website-disclaimer.html",
    "/site-disclaimer",
    "/legal-disclaimer",
    "/en/page/disclaimer",

    # ---- Website / web / general policies ---------------------------
    # Indian government NIC-template sites typically expose a "Website
    # Policy" page that bundles disclaimer + copyright + privacy.
    "/website-policy",
    "/website-policy.html",
    "/website-policies",
    "/website-policies.html",
    "/web-policy",
    "/web-policy.html",
    "/policies",
    "/policy",

    # ---- Legal / copyright ------------------------------------------
    "/legal",
    "/legal.html",
    "/legal-notice",
    "/legal-notice.html",
    "/copyright",
    "/copyright.html",
    "/copyright-policy",
    "/copyright-policy.html",
    "/hyperlinking-policy",
    "/hyperlinking-policy.html",

    # ---- NIC / govt template query-string forms ---------------------
    # Older Indian-uni CMS sites encode the page as a query parameter
    # against `index.php` or the bare root.
    "/index.php?page=disclaimer",
    "/index.php?page=privacy-policy",
    "/index.php?page=terms",
    "/index.php?page=tnc",
    "/?page=disclaimer",
    "/?page=terms",

    # ---- Common CMS prefixes (`/p/` `/page/` `/pages/`) -------------
    "/p/terms-and-conditions",
    "/p/disclaimer",
    "/p/privacy-policy",
    "/page/terms-and-conditions",
    "/page/disclaimer",
    "/page/privacy-policy",
    "/pages/terms-and-conditions",
    "/pages/disclaimer",
    "/pages/privacy-policy",

    # ---- University-specific common patterns ------------------------
    "/about/disclaimer",
    "/about/terms",
    "/about/privacy",
    "/about-us/disclaimer",
    "/info/disclaimer",
    "/info/terms",
    "/important-links/disclaimer",
)

# Words/abbreviations we expect in a T&C page's <title> or <h1>. The body
# can mention them incidentally (footer link text, nav menus); the title
# is what disambiguates "this *is* a T&C page" from "this page mentions T&Cs".
TC_TITLE_KEYWORDS: frozenset[str] = frozenset({
    "terms", "conditions", "disclaimer", "privacy", "policy", "tos", "agreement",
    "legal",
})

# A page whose <title> *primary* phrase (the part before the first " - ",
# " | ", or " :: ") is one of these is treated as definitively a T&C-style
# page — strict body-size / anchor-count / homepage-indicator checks are
# bypassed. CMS-built Indian-uni pages (DU, NIC sites) wrap the actual
# legal text in the full site chrome (huge nav, footer, sidebar), pushing
# raw HTML over 100KB and anchor counts past 400 even though the legal
# content itself is tiny. The title is the trustworthy signal.
TC_STRONG_TITLE_PHRASES: tuple[str, ...] = (
    "disclaimer",
    "privacy policy",
    "privacy notice",
    "terms and conditions",
    "terms of use",
    "terms of service",
    "terms",
)

# Phrases that strongly indicate a page is the homepage (or a content hub),
# not a T&C document. If any of these are in the body, reject the URL even
# if title/h1 looked promising.
HOMEPAGE_INDICATORS: tuple[str, ...] = (
    "notifications",
    "tenders",
    "grievance redressal",
    "anti-ragging",
    "covid",
    "admissions open",
)

# Page body byte-length window for a T&C-shaped page. Real T&C/privacy/
# disclaimer pages are 5–30KB; CMS-chrome-heavy ones can reach 80KB.
# Anything larger is almost always a homepage (Indian-uni homepages are
# 100KB+). Anything smaller is almost always an empty-page / soft-error.
TC_PAGE_MAX_BYTES: int = 80_000
TC_PAGE_MIN_BYTES: int = 500

# Anchor-tag count above which a page is almost certainly a portal/homepage,
# not a T&C document.
TC_PAGE_MAX_ANCHOR_TAGS: int = 50

# Body substrings that mark the page as an error / soft-404 / exception
# screen even when status was 200. Lowercased before matching. Catches
# IIS/.NET, Apache, generic CMS error templates, and the explicit
# "page not found" / "technical issue" copy that ASPX sites tend to use.
TC_HTML_ERROR_INDICATORS: tuple[str, ...] = (
    "technical issue",
    "an error has occurred",
    "an error occurred",
    "error occurred",
    "page not found",
    "404 not found",
    "the resource cannot be found",
    "server error in",
    "an exception of type",
    "service unavailable",
    "request could not be processed",
    "an unexpected error",
    "object reference not set",
)

# URL paths that mark the URL itself as an error endpoint regardless of
# whether the response was 200. Most ASPX-built sites custom-route 404s
# through `/custom.htm?aspxerrorpath=...` — a 200 OK with error body.
TC_URL_ERROR_PATH_PATTERNS: tuple[str, ...] = (
    "/custom.htm", "/error.htm", "/404.htm",
    "/errorpage", "/error.aspx", "/error.html", "/notfound.html",
    "/notfound",
)

TC_URL_ERROR_QUERY_PARAMS: tuple[str, ...] = (
    "aspxerrorpath", "errorpage", "notfound",
)


# PDF validation thresholds for T&C documents served as application/pdf.
TC_PDF_MIN_TEXT_LEN: int = 500

# Stage C — Bug 38 phrase-based PDF validation. Single-word matches
# ("terms", "agreement", "liability") accept too many non-T&C documents
# (AICTE approval letters that happen to mention "agreement", annual
# reports that mention "liability"). Phrases are far harder to hit
# incidentally — a document carrying ≥ TC_PDF_PHRASES_NEEDED of these
# is overwhelmingly likely to be a T&C / privacy / disclaimer doc.
# Substring (case-insensitive) match against extracted PDF text.
TC_PDF_REQUIRED_PHRASES: tuple[str, ...] = (
    "terms and conditions",
    "terms of use",
    "terms of service",
    "privacy policy",
    "disclaimer",
    "website policy",
    "intellectual property",
    "liability",
    "as is",
    "without warranty",
    "governing law",
    "unauthorized use",
    "all rights reserved",
)
TC_PDF_PHRASES_NEEDED: int = 2

# Stage C — Bug 38 PDF body rejection signals. If the first
# `TC_PDF_REJECTION_HEAD_CHARS` of extracted PDF text contain any of
# these substrings, the PDF is treated as a non-T&C document
# (accreditation, prospectus, recruitment notice, …) and rejected even
# if it later mentions T&C phrases incidentally. The head-only window
# avoids rejecting genuine T&Cs that happen to reference an annual
# report in a citation list at the end. Lowercased before matching.
TC_PDF_REJECTION_SIGNALS: tuple[str, ...] = (
    "aicte approval",
    "all india council for technical education",
    "accreditation",
    "naac grade",
    "nirf ranking",
    "annual report",
    "prospectus",
    "fee structure",
    "syllabus",
    "examination schedule",
    "admit card",
    "result notification",
    "tender notice",
    "recruitment notice",
)
TC_PDF_REJECTION_HEAD_CHARS: int = 1000

# Stage C — Bug 38 URL-path rejection patterns. Applied pre-fetch in the
# strict validation gate: if the candidate URL's lowercase path contains
# any of these substrings, reject without making the HTTP request. This
# is the cheapest filter on the AICTE / accreditation / prospectus PDF
# noise that Indian-uni footers commonly link to. Substring match —
# case-insensitive against the URL path component only (queries are
# allowed to contain these tokens incidentally).
TC_URL_REJECTION_PATTERNS: tuple[str, ...] = (
    "aicte", "naac", "nirf",
    "approval", "accreditation",
    "prospectus", "syllabus",
    "result", "admitcard",
    "tender", "recruitment",
    "annual-report", "annual_report",
)


# Paranoid mode — when True, every URL that passes the strict validator
# is re-fetched once more right before being returned. The two fetches are
# compared via difflib similarity; if the second body differs by more
# than (1 - TC_PARANOID_MIN_SIMILARITY) of the first, reject. Catches
# "URL passes validation, gets cached, then turns out to be 404 by the
# time the user opens the sheet" — at the cost of 2x HTTP per accepted URL.
TC_FINDER_PARANOID_MODE: bool = True
TC_PARANOID_MIN_SIMILARITY: float = 0.5

# Stage C — domains we never want to *infer* as a university's main site.
# These are shared platforms (Samarth, MKCL DigitalUniversity, MyLoft, Knimbus)
# whose tenants serve many universities; a portal hosted on one of these tells
# us nothing about the owning university's primary website.
SHARED_PLATFORM_DOMAINS: frozenset[str] = frozenset({
    "samarth.edu.in", "samarth.ac.in",
    "digitaluniversity.ac.in", "digitaluniversity.ac",
    "myloft.xyz", "knimbus.com",
})


# Stage A — admission-portal detection. Admission portals are aimed at
# *prospective* applicants (new-student registration, online application
# forms, prospectus, fee payment for application). The agent's target is
# *enrolled* student logins, so admission portals must be filtered out.
#
# Detection runs in 4 layers (see `discovery_rules.is_admission_portal`):
#
#   Layer 1 — URL only (path/host substring). Cheap, runs pre-fetch in
#     the candidate-gathering phase to save HTTP calls. The `/register`
#     and `/registration` paths get a counter-signal exception so an
#     "existing student registration" page isn't mis-rejected.
#   Layer 2 — page text content. Strong signals (single match enough),
#     moderate signals (need ≥2), and counter signals (enrolled-student
#     features) that disable the moderate path.
#   Layer 3 — title / <h1> phrase match. Fast — runs before the full
#     body parse — and decisive when the page openly identifies as an
#     admission portal in its primary phrase.
#   Layer 4 — known admission-platform host blocklist (JoSAA, KCET,
#     state CETs, NTA portals, …). These are central admission systems
#     for many universities and are never enrolled-student portals.

URL_ADMISSION_PATH_KEYWORDS: tuple[str, ...] = (
    # Direct admission words
    "/admission", "/admissions",
    "/apply", "/application", "/applications",
    "/enroll", "/enrollment", "/enrolment",
    "/register", "/registration",  # see counter-signal exception below
    # New-student-specific
    "/newreg", "/new_reg", "/newregistration", "/new_registration",
    "/freshreg", "/fresh_reg", "/freshregistration",
    "/newstudent", "/new_student", "/new-student",
    "/newuser", "/new_user", "/new-user",
    "/newapplicant", "/new_applicant",
    "/firstyear", "/first_year", "/first-year",
    "/signup", "/sign_up", "/sign-up",
    "/createaccount", "/create_account", "/create-account",
    # Indian-uni specific
    "/ugadm", "/pgadm", "/phadm",
    "/ugregistration", "/pgregistration",
    "/prospectus",
    "/onlineadmission", "/online_admission", "/online-admission",
    "/admform", "/adm_form", "/adm-form",
    "/counselling", "/counseling",
    "/merit", "/meritlist", "/merit_list",
    "/allotment",
    "/document_verification", "/docverification",
)

URL_ADMISSION_HOST_KEYWORDS: tuple[str, ...] = (
    "admission", "admissions",
    "apply", "enroll", "enrolment",
    "newadmission", "freshregistration",
    "onlineadmission",
)

# `/register` / `/registration` URL exception. If the path *also*
# contains any of these tokens, the URL-layer reject is skipped and
# the page proceeds to content evaluation — the destination is likely
# an existing-student login surface, not new-applicant signup.
URL_ADMISSION_REGISTER_EXEMPT_TOKENS: tuple[str, ...] = (
    "student", "login", "signin", "existing",
)

# Layer 2 — page text signals. Single match of any STRONG signal is
# enough to reject. Substring (case-insensitive) match against the full
# page text extracted via BeautifulSoup `get_text()`.
STRONG_ADMISSION_SIGNALS: tuple[str, ...] = (
    "new student registration",
    "fresh student registration",
    "first year registration",
    "new applicant registration",
    "online admission form",
    "admission application form",
    "apply for admission",
    "start your application",
    "new user registration",
    "register as new user",
    "create new account",
    "not yet registered? register",
    "don't have an account? register",
    "prospective student",
    "applicant login",
    "candidate registration",
    "new candidate",
    # Bug 40 — exam-form / generic-form registration phrases. Indian
    # universities sometimes expose a form-registration page (exam
    # form, fee form, scholarship form) with a password input; the
    # field is "Form No." not student id, and the page invites the
    # user to "click here to apply" rather than to log in.
    "apply for new form",
    "click here to apply",
    "apply for form",
    "new form registration",
    "form submission",
    "submit application",
)

# Need ≥2 of these to reject (and zero counter-signals), OR ≥4 even
# with counter-signals.
MODERATE_ADMISSION_SIGNALS: tuple[str, ...] = (
    "father's name", "father name",
    "mother's name", "mother name",
    "date of birth",
    "upload photo", "upload photograph",
    "upload signature",
    "upload documents", "upload certificate",
    "qualifying examination",
    "year of passing",
    "board of examination", "board name",
    "category general", "category obc", "category sc", "category st",
    "general/obc/sc/st",
    "application fee",
    "application number",
    "entrance exam", "entrance test",
    "merit list",
    "counselling",
    "seat allotment",
    "document verification",
    "fresh registration",
    "new registration",
    "apply now",
    "start application",
    "10th marks", "12th marks",
    "hsc marks", "ssc marks",
    "passing year",
    "stream arts science commerce",
    "domicile",
    "income certificate",
    "caste certificate",
)

# Counter-signals — features that only an enrolled-student portal would
# expose. Suppress the moderate-signal reject when present (a single
# strong signal still rejects regardless).
STUDENT_LOGIN_COUNTER_SIGNALS: tuple[str, ...] = (
    "enrollment number",
    "enrolment number",
    "roll number",
    "university roll no",
    "student id",
    "student code",
    "registration number",  # already-enrolled student's reg number
    "already registered",
    "existing student",
    "forgot password",
    "change password",
    "fee receipt",
    "admit card download",
    "exam form",
    "result",
    "attendance",
    "timetable", "time table",
    "library",
    "hostel",
    "scholarship",
    "back paper", "backlog",
)

# Layer 3 — <title> / <h1> phrases. Match → reject (without parsing
# full body), with one exception: if the same title contains both
# "login" AND "existing", proceed to the full content check (it's
# probably an existing-student login labeled as a "registration
# portal").
TITLE_ADMISSION_PHRASES: tuple[str, ...] = (
    "admission portal",
    "admissions portal",
    "online admission",
    "admission form",
    "new registration",
    "student registration",
    "applicant portal",
    "candidate portal",
    "apply online",
    "application portal",
    "admission management",
    "admission system",
    "college admission",
    "university admission",
)

# Layer 4 — known central admission platforms. Hosts that match
# (host == entry or host.endswith("." + entry)) are rejected outright.
KNOWN_ADMISSION_PLATFORMS: tuple[str, ...] = (
    "wbnsouadmissions.com",
    "josaa.nic.in",
    "csab.nic.in",
    "upseat.in",
    "kcet.karnataka.gov.in",
    "tgeapcet.nic.in",
    "mahacet.org",
    "jeemain.nta.nic.in",
    "neet.nta.nic.in",
)


# Stage A — Fix 3 hard-blocked instance hosts. Specific tenant subdomains
# of state-platforms or other multi-tenant systems that surface in
# Stage A search results for unrelated OrgIDs (e.g. Bihar UMS state-
# platform tenants like `nou.bihar-ums.com` for Nalanda Open University).
# Bug 43's foreign-state reject already filters these for non-Bihar
# OrgIDs, but the explicit per-host blocklist is a belt-and-suspenders
# guard that runs *before* any HTTP fetch and applies regardless of
# OrgID state — so they never enter the candidate queue at all.
#
# Match is exact host equality OR strict subdomain
# (`host == entry or host.endswith("." + entry)`).
KNOWN_INSTANCE_BLOCKLIST: tuple[str, ...] = (
    "nou.bihar-ums.com",     # Nalanda Open University
    "jpv.bihar-ums.com",     # Jai Prakash Vishwavidyalaya
    "ppu.bihar-ums.com",     # Patliputra University
    "ppuponline.in",         # Patliputra University online portal
    # Patna University. Belongs ONLY to OrgID 663894 (Patna). Listed
    # here so other OrgIDs that surface it via DDG (SOL DU, GJUST,
    # Sathyabama, …) reject it at pre-filter. The blocklist runs
    # *before* `host_belongs_to_org`, so OrgID 663894 itself can't
    # reach this URL through `extra_effective_domains` matching either
    # — Patna's override now uses `force_accept_seed_urls` to inject
    # `pu.bihar-ums.com/login` directly past pre-filter.
    "pu.bihar-ums.com",
    # MIT University Shillong (Meghalaya). Surfaces in DDG results for
    # other MIT-named institutions (MIT ADT Pune, MIT Manipal, …)
    # because R6's startswith-on-base-label matches "mituniversity"
    # against "mituniversityindia". One blocklist entry suffices —
    # `host_in_instance_blocklist` matches subdomains via endswith,
    # so `erp.mituniversityindia.edu.in` is also rejected.
    "mituniversityindia.edu.in",
)


# Stage A — auto-derived shortnames that are too generic to drive R6
# (shortname-in-domain) matching. These are common Indian college
# acronyms shared across many institutions: MIT (ADT, Shillong,
# Manipal, Muzaffarpur), IIIT (Hyderabad, Bangalore, …), NIT
# (Trichy, Surathkal, Warangal, …), etc.
#
# When an OrgID's auto-derived shortname (leftmost label of a
# configured domain) appears here, R6 does NOT use it — even though
# its length passes the ≥4 floor. Operator-curated `exact_shortnames`
# in `domain_overrides.json` are NEVER filtered by this list (the
# operator is presumed to know the disambiguator); the filter only
# applies to the auto-derived set.
#
# Note: this does NOT prevent prefix-leak when an OrgID's own
# auto-shortname is a prefix of ANOTHER institution's name (e.g.
# "mituniversity" prefix of "mituniversityindia"). Use
# `KNOWN_INSTANCE_BLOCKLIST` for those host-specific conflicts.
AMBIGUOUS_SHORTNAMES: frozenset[str] = frozenset({
    "mit",     # MIT ADT, MIT Shillong, MIT Manipal, MIT Muzaffarpur, …
    "iet",     # Institute of Engineering & Technology — many colleges
    "iiit",    # multiple IIITs (Hyderabad, Bangalore, Allahabad, …)
    "nit",     # multiple NITs (Trichy, Surathkal, Warangal, …)
    "bit",     # multiple BITs (Mesra, Sindri, Durg, …)
    "sit",     # multiple SITs (Tumkur, Pune, …)
    "git",     # multiple GITs
})


def host_in_instance_blocklist(host: str) -> bool:
    """True iff `host` equals or is a subdomain of any
    `KNOWN_INSTANCE_BLOCKLIST` entry."""
    if not host:
        return False
    h = host.lower().lstrip(".")
    for entry in KNOWN_INSTANCE_BLOCKLIST:
        if h == entry or h.endswith("." + entry):
            return True
    return False


# Stage A — Bug 40 login-form audience check. After a candidate page
# clears the existing strict gate (`passes_login_signal_gate` rule-A:
# real login form on the page), examine the form's *primary identifier*
# field. The agent's target is enrolled-student logins, so a form whose
# primary identifier asks for "From No.", "Application No.",
# "Challan No." etc. is something else (exam-form registration, fee
# challan, admission application) even when it has a password input
# and a `/login.php` URL.
#
# `discovery_rules.classify_login_form_audience(html)` is the decision
# surface. It returns `"non_student"` to reject and `"student"` to keep.

# Field labels / placeholders / nearby text whose presence on a form
# indicates an enrolled-student login. ANY one of these found in the
# combined label+placeholder+aria-label+name+id text → keep.
STUDENT_IDENTITY_FIELD_SIGNALS: tuple[str, ...] = (
    "enrollment number", "enrolment number",
    "roll number", "roll no", "roll no.",
    "student id", "student code", "student no",
    "registration number", "reg no", "reg. no",
    "university id", "university roll",
    "admission number",
    "scholar number",
    "prn number", "prn no",
    "form number",
    "username",
    "user id", "user name",
    "mobile number",
    "email",
    "employee id",
    # Bug 1 — additional Indian-uni labels observed on the SOL DU
    # student portal (`web.sol.du.ac.in/student-login`) and similar
    # legacy CMS-built forms.
    "bar code", "barcode",
    "sol roll", "sol id",
    "id card", "id no",
    # Section 8 — exam / hall-ticket / seat / library labels
    # observed across more Indian-uni portals.
    "exam roll",
    "htno", "hall ticket no", "hall ticket number",
    "seat no", "seat number",
    "lib id", "library id",
    "admission no", "scholar no",
)

# Field labels matched only against the *primary* identifier field
# (first non-password / non-hidden / non-submit input). When the
# primary field carries one of these → reject outright. These are
# never used to identify an enrolled student.
EXPLICIT_NON_STUDENT_FIELD_SIGNALS: tuple[str, ...] = (
    "from no",
    "form no",
    "challan no", "challan number",
    "application no", "application number",
    "token no", "token number",
    "dd number", "demand draft",
    "transaction id", "transaction number",
)

# Body-content fallback signals. When the form's labels match neither
# STUDENT_IDENTITY_FIELD_SIGNALS nor EXPLICIT_NON_STUDENT_FIELD_SIGNALS
# (i.e. a generic "Login" + password page), look for ≥2 of these in
# the visible page text. A real student portal almost always says
# "student" / mentions "semester" / "department" / "course" somewhere.
STUDENT_CONTEXT_SIGNALS: tuple[str, ...] = (
    "student", "students",
    "enrolled", "enrollment", "enrolment",
    "academic", "semester", "session",
    "college", "department",
    "faculty", "programme", "course",
)
STUDENT_CONTEXT_SIGNALS_NEEDED: int = 2


# Stage A — Bug 29/30 state-platform hints. State-government UMS / DU
# platforms host many universities under the same root, distinguished only
# by an institutional subdomain (e.g. `pu.bihar-ums.com` for Patna,
# `ppu.bihar-ums.com` for Patliputra). When two universities share an
# ambiguous shortname (e.g. "pup" matches both Punjabi University Patiala
# and Patliputra/Patna confusion), shortname matching alone can't
# disambiguate; the membership check in `discovery.host_belongs_to_org`
# additionally requires the state-platform host's institutional subdomain
# prefix to be in the OrgID's `exact_shortnames`.
#
# Maps a state name (as it appears in `domain_overrides[orgid]["state"]`)
# to the platform domains served by that state's government / DU. Hosts
# match if `host == entry` or `host.endswith("." + entry)`.
STATE_PLATFORM_HINTS: dict[str, tuple[str, ...]] = {
    "Bihar": ("bihar-ums.com",),
    "Maharashtra": (
        "digitaluniversity.ac",
        "digitaluniversity.ac.in",
        "mkcl.org",
    ),
}


# Stage A — Bug 31 functional prefixes on platform-tenant subdomains.
# Indian Samarth / state-platform deployments often expose a single
# institution under multiple tenants discriminated by a *function* prefix —
# e.g. `lms-ccsuniversity.samarth.ac.in` (LMS) and `ccsuniversity.samarth.edu.in`
# (Student Portal) both belong to Chaudhary Charan Singh University.
# Strict membership in `host_belongs_to_org` rule (4) checks the literal
# tenant prefix against `exact_shortnames`, which would reject the LMS
# tenant. Stripping a known functional prefix before that check lets the
# strict gate accept both tenants without loosening the cross-university
# safety guarantee.
SAMARTH_FUNCTIONAL_PREFIXES: tuple[str, ...] = (
    "lms-",
    "elearn-", "elearning-",
    "exam-", "examination-",
    "result-", "results-",
    "fee-", "fees-",
    "lib-", "library-",
    "admission-", "admissions-", "adm-",
    "cdoe-", "cde-", "ide-", "dde-", "sol-", "idol-",
    "ug-", "pg-",
    "distance-", "online-",
    "portal-", "student-", "students-",
    "app-",
)


# Stage A — Bug 24 EXTERNAL_DOMAIN_BLOCKLIST. When walking outbound links
# from a primary domain's homepage (Bug 22 sibling-domain extraction),
# anchors pointing at hosts that match an entry here are skipped: they're
# never university portals.
#
# A host matches if it equals an entry exactly OR is a subdomain of one
# (`host == entry or host.endswith("." + entry)`). Therefore each entry
# must be a *specific* domain — never a TLD or eTLD like `.com`, `.in`,
# `.org`, `.co.in`, `ac.in`. Indian universities frequently host real
# portals on commercial TLDs (`hpushimla.in`, `bihar-ums.com`,
# `nsoucebdp.com`, `pcdpcal.com`, `mygyanvihar.com`, …); blocking those
# TLDs would drop those portals.
EXTERNAL_DOMAIN_BLOCKLIST: tuple[str, ...] = (
    # Social
    "facebook.com", "twitter.com", "x.com", "linkedin.com", "instagram.com",
    "youtube.com", "youtu.be", "tiktok.com", "pinterest.com", "whatsapp.com",
    "reddit.com", "snapchat.com",
    # Google / generic services
    "google.com", "googleusercontent.com", "gmail.com",
    "docs.google.com", "drive.google.com", "forms.google.com", "forms.gle",
    "sites.google.com",
    # Microsoft / Office / Outlook
    "microsoft.com", "office.com", "outlook.com", "onedrive.live.com",
    # Conferencing
    "zoom.us", "us02web.zoom.us", "us04web.zoom.us", "us05web.zoom.us",
    "teams.microsoft.com", "webex.com",
    # Other generic services
    "yahoo.com", "wikipedia.org", "wikimedia.org",
    "github.com", "gitlab.com",
    "cloudflare.com", "jquery.com", "gstatic.com", "googletagmanager.com",
    # Email / newsletters
    "mailchimp.com", "constantcontact.com", "sendgrid.com",
    # Government — block top-level gov / nic since these are official
    # external services (UGC, MoHRD, NPTEL, …) that universities link to
    # but never *are*. Specific state-government UMS platforms that DO
    # host real portals (`bihar-ums.com`, `digitaluniversity.ac.in`, …)
    # are not on these TLDs and thus are not affected.
    "gov.in", "nic.in",
    # NPTEL / SWAYAM are Govt-of-India MOOC hubs — universities link to
    # them but they're not the university's portal.
    "nptel.ac.in", "swayam.gov.in", "swayamprabha.gov.in",
    # Section 11 — additional external services that surface in DDG
    # results for many universities but are never the university's
    # own portal:
    "t.co",                  # Twitter URL shortener
    "wixsite.com",           # Wix-hosted promotional sites
    "irins.org",             # India Research Information System
    "samadhaan.ugc.ac.in",   # UGC grievance portal
    "ugc.ac.in",             # UGC general
    "aicte-india.org",       # AICTE
    "cert-in.org.in",        # CERT-In
    "digilocker.gov.in",     # DigiLocker
    "careers360.com",        # Careers360 review aggregator
)


# Section 11 — affiliated-college filter trigger. When the sibling-walk
# surfaces more than this many host candidates, an additional filter
# removes hosts whose domain name contains "college" / "school" /
# "institute" — at that count we're almost certainly looking at a
# parent-university (DU / Mumbai / VTU) whose homepage links to many
# affiliated college websites, none of which are this OrgID's portal.
SIBLING_COUNT_AFFILIATED_FILTER_THRESHOLD: int = 20
AFFILIATED_DOMAIN_TOKENS: tuple[str, ...] = (
    "college", "school", "institute",
)


def host_in_external_blocklist(host: str) -> bool:
    """True if `host` equals or is a subdomain of any
    `EXTERNAL_DOMAIN_BLOCKLIST` entry. Used by Bug 22 sibling-domain
    extraction.
    """
    if not host:
        return False
    h = host.lower()
    for entry in EXTERNAL_DOMAIN_BLOCKLIST:
        if h == entry or h.endswith("." + entry):
            return True
    return False


# Stage A — Bug 7 same-host login URL preference. When multiple login URLs
# exist on the same host, score each candidate's URL path: student-anchored
# paths get a strong boost; non-student-anchored paths (college admin / staff
# / faculty / etc.) get a heavy penalty so they're disqualified unless they
# are the only option for that host. Substring match (case-insensitive).
STUDENT_LOGIN_PATH_KEYWORDS: tuple[str, ...] = (
    "/student", "/studentlogin", "/student-login", "/student_login",
    "/learner",
)

NON_STUDENT_LOGIN_PATH_KEYWORDS: tuple[str, ...] = (
    "/college", "/college-login",
    "/admin",
    "/staff",
    "/faculty",
    "/employee",
    "/teacher",
    "/principal",
    "/vendor",
    "/recruitment",
    "/hr",
    "/alumni",
    "/parent",
)

# Bug 7 boost / penalty magnitudes. Penalty is large enough that a single
# non-student keyword almost always disqualifies a candidate when a peer
# with a student keyword exists on the same host.
STUDENT_LOGIN_PATH_BOOST: int = 5
NON_STUDENT_LOGIN_PATH_PENALTY: int = -10


# Stage A — Bug 8 subdomain probe. After main rule/Claude discovery, we
# probe these subdomains under every configured university domain for a
# login page. Catches SIS/SIM-style portals that DDG doesn't surface and
# that `run_path_probes` (which uses fixed templates like
# `student.{domain}`) misses by not having entries for sim/sis/erp.
#
# Bug 10 expansion: Indian-uni subdomain naming is wildly heterogeneous
# (feeportal vs fee, libportal vs lib, tnp vs placement, etc.). The list
# below covers every common pattern we've seen across the verified set.
SUBDOMAIN_PROBE_LIST: tuple[str, ...] = (
    # Generic student
    "student", "students", "studentportal", "studentlogin",
    "myaccount", "self-service", "selfservice",
    # Generic portal
    "portal", "myportal",
    # Information / ERP
    "sim", "sis", "erp", "mis", "ums",
    # Examination
    "exam", "exams", "examination", "examportal",
    "result", "results", "resultportal",
    "hallticket", "admitcard",
    "certificate", "certificates", "transcripts",
    # Library
    "lib", "library", "libportal",
    "elibrary", "digitallibrary",
    # Fee
    "fee", "fees", "feeportal", "payment", "online-payment",
    # Placement
    "placement", "placements", "tnp", "career",
    # LMS
    "lms", "moodle", "elearning", "elearn", "vle", "learning",
    # Hostel
    "hostel", "hostels", "hostelportal",
    # Distance learning (already in global allow-list)
    "sol", "ncweb", "idol", "cdoe", "cde", "ide", "dde", "udrc", "cdl", "soe",
)


# Stage A — Bug 7 same-host student-login path probes. After initial
# rule/Claude/subdomain discovery, for every unique host that produced a
# candidate we additionally probe these paths to surface a sibling student
# login URL. Catches CMS layouts where the discovered URL points at the
# college/admin login (e.g. SPPU SOL's `/College/CollegeLogin/CollegeLogin`)
# but the student equivalent (`/Login/Login/StudentLogin`) lives at a
# predictable peer path.
STUDENT_LOGIN_SAME_HOST_PROBES: tuple[str, ...] = (
    "/Login/Login/StudentLogin",
    "/login/login/studentlogin",
    "/Login/StudentLogin",
    "/login/studentlogin",
    "/student/login",
    "/Student/Login",
    "/StudentLogin",
    "/student-login",
    # Fix B — additional login-path variants observed in the corpus.
    # `/account/login` is Sathyabama's ERP path; `/portal/v2/default/login`
    # is the Knimbus tenant pattern; the rest are common .NET / Django /
    # Rails / generic CMS layouts.
    "/account/login",
    "/account/studentlogin",
    "/Account/Login",
    "/Account/StudentLogin",
    "/accounts/login",
    "/user/login",
    "/users/login",
    "/auth/login",
    "/auth/student",
    "/secure/login",
    "/portal/login",
    "/portal/v2/default/login",
    # Section 3 — React/SPA / Indian-uni custom paths. `/itxlogin` is
    # MIT ADT's student.mitapps.in entry point; the rest are common
    # React-app prefixes that show up across portals where the root
    # serves an SPA shell and the actual login lives at a sub-path.
    "/itxlogin",
    "/app/login",
    "/app/signin",
    "/app/student",
    "/web/login",
    "/ui/login",
)


# Stage A — Bug 9 login-link follow. When a validated candidate's URL is a
# bare host (path == "/"), scan the homepage anchors for these text patterns
# and follow the highest-scoring one to a more specific login URL. Substring
# match against anchor text and href, case-insensitive.
LOGIN_LINK_TEXT_PATTERNS: tuple[str, ...] = (
    "student login", "member login", "user login", "my account",
    "sign in", "signin", "log in", "login",
)


# Stage A — LMS/Moodle category detection (score-based, threshold ≥2).
# Host substrings that mark a subdomain as an LMS tenant. Substring (not
# segment-equal) match: "elearning.x.edu" matches "elearning"; "learning"
# matches inside "elearning" too — both are correct LMS signals.
LMS_HOST_TOKENS: tuple[str, ...] = (
    "moodle", "lms", "elearning", "learning", "vle", "lcms", "elearn",
)

# Hosts that suffix-match any of these are third-party LMS tenants
# (talentlms, classplus, blackboard, canvas, brightspace, etc.). Score 1.
LMS_THIRD_PARTY_HOSTS: tuple[str, ...] = (
    "cognibot.in", "talentlms.com", "classplusapp.com", "edmingle.com",
    "schoolyard.in", "blackboard.com", "canvaslms.com", "instructure.com",
    "brightspace.com",
)


# Stage D — sheet-writer category sort order. Portals are grouped into
# these buckets (post-remap) and rendered in this order; within a bucket
# they sort alphabetically by URL. Anything not listed here (post-remap)
# is bucketed as "Other" and appended last.
CATEGORY_ORDER: tuple[str, ...] = (
    "Student Portal",
    "LMS/Moodle",
    "Examination",
    "Library",
    "Fee",
)

# Stage D — remap from raw Stage A category labels to the canonical short
# names used in `CATEGORY_ORDER`. Applied for sort-grouping only; the
# stored category on the portal record is unchanged. Several Stage A
# subcategories ("Hall Ticket", "Admit Card", etc.) collapse into
# "Examination" so they all sit together in the rendered cell.
CATEGORY_REMAP_FOR_SORTING: dict[str, str] = {
    # Subcategories that all sort under Examination.
    "Hall Ticket": "Examination",
    "Admit Card": "Examination",
    "Result": "Examination",
    "Certificate": "Examination",
    "Transcript": "Examination",
    # Stage A's longer category names → short canonical form.
    "Examination Portal": "Examination",
    "Library Portal": "Library",
    "Fee Portal": "Fee",
    # Legacy alias — pre-rename runs may have stored "LMS" in state.db.
    "LMS": "LMS/Moodle",
}


@dataclass(frozen=True)
class Config:
    anthropic_api_key: str
    anthropic_model: str
    google_sheet_id: str
    universities_tab: str
    portals_tab: str
    google_credentials_path: Path
    google_token_path: Path
    state_db_path: Path
    log_level: str
    portal_confidence_threshold: int
    http_timeout_seconds: int
    user_agent: str
    # Stage A
    discovery_model: str
    discovery_claude_max_uses: int
    discovery_max_results_per_query: int
    discovery_ddg_sleep_seconds: float
    domain_overrides: dict[str, dict[str, Any]]
    # Stage A — JS-render fallback (Playwright)
    enable_js_rendering: bool
    js_rendering_suspicion_threshold: int
    js_rendering_timeout_seconds: int
    # Stage A — Claude fallback (off by default; needs API credits)
    enable_claude_fallback: bool
    # Stage C — T&C analyzer mode ("keyword" | "claude")
    tc_analyzer_mode: str


def _require(key: str) -> str:
    value = os.environ.get(key)
    if not value:
        raise RuntimeError(
            f"Missing required env var {key}. Copy .env.example to .env and fill it in."
        )
    return value


def _resolve(path_str: str) -> Path:
    p = Path(path_str)
    return p if p.is_absolute() else ROOT / p


def _load_domain_overrides(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as err:
        raise RuntimeError(f"{path} is not valid JSON: {err}") from err
    if not isinstance(data, dict):
        raise RuntimeError(f"{path} must be a JSON object at the top level")
    out: dict[str, dict[str, Any]] = {}
    for key, val in data.items():
        if not isinstance(val, dict):
            logger.warning("domain_overrides: entry %r is not an object; skipping", key)
            continue
        out[str(key)] = val
    return out


def load_config() -> Config:
    anthropic_api_key = os.getenv("ANTHROPIC_API_KEY", "")
    enable_claude_fallback = os.environ.get("ENABLE_CLAUDE_FALLBACK", "false").lower() in ("1", "true", "yes", "on")
    tc_analyzer_mode = os.environ.get("TC_ANALYZER_MODE", "keyword").lower()
    if (enable_claude_fallback or tc_analyzer_mode == "claude") and not anthropic_api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is required when ENABLE_CLAUDE_FALLBACK=True "
            "or TC_ANALYZER_MODE=claude. Add it to your .env file."
        )
    return Config(
        anthropic_api_key=anthropic_api_key,
        anthropic_model=os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-7"),
        google_sheet_id=_require("GOOGLE_SHEET_ID"),
        universities_tab=os.environ.get("UNIVERSITIES_TAB_NAME", "Universities"),
        portals_tab=os.environ.get("PORTALS_TAB_NAME", "Portals"),
        google_credentials_path=_resolve(
            os.environ.get("GOOGLE_CREDENTIALS_PATH", "credentials.json")
        ),
        google_token_path=_resolve(os.environ.get("GOOGLE_TOKEN_PATH", "token.json")),
        state_db_path=_resolve(os.environ.get("STATE_DB_PATH", "state.db")),
        log_level=os.environ.get("LOG_LEVEL", "INFO"),
        portal_confidence_threshold=int(os.environ.get("PORTAL_CONFIDENCE_THRESHOLD", "60")),
        http_timeout_seconds=int(os.environ.get("HTTP_TIMEOUT_SECONDS", str(HTTP_TIMEOUT_SECONDS))),
        # Browser-like default User-Agent. Many Indian-uni portals
        # (web.sol.du.ac.in, similar) return HTTP 403 to anything that
        # looks like a Python crawler ("python-requests/X" or our old
        # "reclaim-portal-agent/0.1"). Spoofing a current Chrome-on-
        # Mac string fixes the 403-class without per-URL overrides.
        # Override via .env if needed.
        user_agent=os.environ.get(
            "USER_AGENT",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36",
        ),
        discovery_model=os.environ.get("DISCOVERY_MODEL", "claude-sonnet-4-6"),
        discovery_claude_max_uses=int(os.environ.get("DISCOVERY_CLAUDE_MAX_USES", "5")),
        discovery_max_results_per_query=int(os.environ.get("DISCOVERY_MAX_RESULTS_PER_QUERY", "8")),
        discovery_ddg_sleep_seconds=float(os.environ.get("DISCOVERY_DDG_SLEEP_SECONDS", "0.6")),
        domain_overrides=_load_domain_overrides(DOMAIN_OVERRIDES_PATH),
        enable_js_rendering=os.environ.get("ENABLE_JS_RENDERING", "true").lower() in ("1", "true", "yes", "on"),
        js_rendering_suspicion_threshold=int(os.environ.get("JS_RENDERING_SUSPICION_THRESHOLD", "3")),
        js_rendering_timeout_seconds=int(os.environ.get("JS_RENDERING_TIMEOUT_SECONDS", str(JS_RENDERING_TIMEOUT_SECONDS))),
        enable_claude_fallback=enable_claude_fallback,
        tc_analyzer_mode=tc_analyzer_mode,
    )

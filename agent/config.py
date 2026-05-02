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
TOTAL_DISCOVERY_BUDGET_SECONDS: int = 90
TOTAL_TC_BUDGET_SECONDS: int = 60


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
    "knimbus.com": {"category": "Library", "validated": True},
    "cognibot.in": {"category": "LMS/Moodle", "validated": True},
    # Bihar state-government UMS — university subdomains like
    # `pu.bihar-ums.com/login` (Patna University). Hard verification still
    # applies (DNS, status, body) — adding it here exempts these hosts
    # from the off-domain filter so an organically-discovered URL gets
    # validated rather than dropped.
    "bihar-ums.com": {"category": "Student Portal", "validated": True},
}


# Stage C — curated path list for the university-level T&C fallback.
# Tried in order against the university root; first one passing the strict
# validation in `tc_finder._validate_university_tc_url` wins. Order encodes
# specificity (T&C-specific → privacy → disclaimer) plus CMS variants for
# DU/NIC-built sites and older Indian-uni "/disclaimer.html" patterns.
UNIVERSITY_TC_FALLBACK_PATHS: tuple[str, ...] = (
    # T&C-specific
    "/en/page/terms-condition",
    "/en/page/terms-conditions",
    "/terms-and-conditions",
    "/terms-of-use",
    "/terms",
    "/tos",
    # Privacy
    "/en/page/privacy-policy",
    "/privacy-policy",
    "/privacy",
    # Disclaimer
    "/en/page/disclaimer",
    "/disclaimer",
    # CMS-specific patterns (DU, NIC-built sites)
    "/index.php?page=disclaimer",
    "/index.php?page=privacy-policy",
    "/index.php?page=terms",
    # Older Indian uni patterns
    "/disclaimer.html",
    "/privacy.html",
    "/terms.html",
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
TC_PDF_REQUIRED_KEYWORDS: tuple[str, ...] = (
    "terms", "conditions", "privacy", "disclaimer",
    "agreement", "as is", "liability", "governance",
)
TC_PDF_KEYWORDS_NEEDED: int = 2


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
    return Config(
        anthropic_api_key=_require("ANTHROPIC_API_KEY"),
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
        user_agent=os.environ.get("USER_AGENT", "reclaim-portal-agent/0.1"),
        discovery_model=os.environ.get("DISCOVERY_MODEL", "claude-sonnet-4-6"),
        discovery_claude_max_uses=int(os.environ.get("DISCOVERY_CLAUDE_MAX_USES", "5")),
        discovery_max_results_per_query=int(os.environ.get("DISCOVERY_MAX_RESULTS_PER_QUERY", "8")),
        discovery_ddg_sleep_seconds=float(os.environ.get("DISCOVERY_DDG_SLEEP_SECONDS", "0.6")),
        domain_overrides=_load_domain_overrides(DOMAIN_OVERRIDES_PATH),
        enable_js_rendering=os.environ.get("ENABLE_JS_RENDERING", "true").lower() in ("1", "true", "yes", "on"),
        js_rendering_suspicion_threshold=int(os.environ.get("JS_RENDERING_SUSPICION_THRESHOLD", "3")),
        js_rendering_timeout_seconds=int(os.environ.get("JS_RENDERING_TIMEOUT_SECONDS", str(JS_RENDERING_TIMEOUT_SECONDS))),
        enable_claude_fallback=os.environ.get("ENABLE_CLAUDE_FALLBACK", "false").lower() in ("1", "true", "yes", "on"),
        tc_analyzer_mode=os.environ.get("TC_ANALYZER_MODE", "keyword").lower(),
    )

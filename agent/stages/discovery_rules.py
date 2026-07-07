"""Stage A Pass 1 — rule-based portal discovery, plus post-validation
consolidation (the three filters that collapse validated candidates down to
one row per category).

The stage runs in four distinct phases:

1. **Discovery** — `run_searches` runs the two neutral DuckDuckGo HTML queries
   (`<name> student login`, `<domain> student portal`), `run_path_probes`
   tries 16 common subdomain/path patterns on the university's OWN domain,
   and `run_subdomain_probes` probes the SUBDOMAIN_PROBE_LIST tokens against
   the configured domain(s). No platform-targeted upfront queries / probes
   — Samarth / DigitalUniversity URLs are accepted only when they surface
   organically from the neutral search.
2. **Category inference** (`infer_category`) — re-categorises each validated
   candidate using URL host+path signals. Shared-platform hosts map to the
   platform's declared category.
3. **Consolidation** (`consolidate_candidates`) — applied *after* HTTP /
   login-signal validation and category inference. It:
     * Filter 1 — dedups by base host (scheme + host), picking the
       strongest-scoring candidate within each host.
     * Filter 2 — drops college-specific subdomains. A URL passes if
       (a) it's a shared-platform portal whose subdomain matches one of
       this OrgID's label or acronym candidates, (b) it's under an
       `extra_allowed_root_domains` entry, or (c) it's under a strict
       configured domain with every subdomain label either in the global
       allow-list or the per-OrgID `extra_allowed_subdomains` list.
     * Filter 3 — collapses to one winner per category using a rule-based
       score favouring direct login URLs.
"""
from __future__ import annotations

import json
import logging
import re
import ssl
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit

import requests
import urllib3
from bs4 import BeautifulSoup

from ..config import (
    ADMIN_URL_PATH_TOKENS,
    AMBIGUOUS_SHORTNAMES,
    DUCKDUCKGO_TIMEOUT_SECONDS,
    ERP_APP_LOGIN_PATH_TEMPLATES,
    ERP_APP_LOGIN_SUBDOMAINS,
    EXPLICIT_NON_STUDENT_FIELD_SIGNALS,
    FOREIGN_ACADEMIC_TLDS,
    GEMINI_SEARCH_ENABLED,
    HASH_ROUTED_PLATFORM_ROOTS,
    KNOWN_ADMISSION_PLATFORMS,
    KNOWN_SHARED_PLATFORM_PATTERNS,
    LMS_HOST_TOKENS,
    LMS_THIRD_PARTY_HOSTS,
    LOGIN_LINK_TEXT_PATTERNS,
    MODERATE_ADMISSION_SIGNALS,
    MOODLE_LOGIN_COUNTER_SIGNALS,
    NON_STUDENT_LOGIN_PATH_KEYWORDS,
    NON_STUDENT_LOGIN_PATH_PENALTY,
    OPENROUTER_API_KEY,
    OPENROUTER_MODEL,
    SAMARTH_FUNCTIONAL_PREFIXES,
    SAMARTH_NONSTUDENT_TENANT_SUFFIXES,
    STATE_PLATFORM_HINTS,
    STRONG_ADMISSION_SIGNALS,
    STUDENT_CONTEXT_SIGNALS,
    STUDENT_CONTEXT_SIGNALS_NEEDED,
    STUDENT_IDENTITY_FIELD_SIGNALS,
    STUDENT_LOGIN_COUNTER_SIGNALS,
    STUDENT_LOGIN_PATH_BOOST,
    STUDENT_LOGIN_PATH_KEYWORDS,
    STUDENT_LOGIN_SAME_HOST_PROBES,
    SUBDOMAIN_PROBE_LIST,
    TITLE_ADMISSION_PHRASES,
    URL_ADMISSION_HOST_KEYWORDS,
    URL_ADMISSION_PATH_KEYWORDS,
    URL_ADMISSION_REGISTER_EXEMPT_TOKENS,
    host_in_external_blocklist,
    host_in_instance_blocklist,
)
from .. import regions
from requests.adapters import HTTPAdapter
from urllib3.util.ssl_ import create_urllib3_context

logger = logging.getLogger(__name__)


# --- HTTP session with tolerant SSL --------------------------------------
#
# Many Indian government / state university servers (e.g.
# sppuapp.digitaluniversity.ac) combine two TLS legacy behaviours that
# modern Python rejects by default:
#
# 1. **Unsafe legacy renegotiation** — Python 3.14 raises
#    SSLError(UNSAFE_LEGACY_RENEGOTIATION_DISABLED). Fixed by opting in to
#    `OP_LEGACY_SERVER_CONNECT` on the TLS context.
# 2. **Self-signed / untrusted CA chains** — raises
#    SSLCertVerificationError. We set `verify=False` on the session.
#
# Trade-offs: the session is intentionally more permissive than stdlib
# defaults. Acceptable because this agent only performs unauthenticated
# GETs against public pages; domain-validation (Filter 2) then rejects
# anything off-domain before a URL can reach Stage A output. No
# credentials are ever sent, so legacy-renegotiation MITM or a spoofed
# cert can't leak anything.
_LEGACY_SERVER_CONNECT_FLAG = getattr(ssl, "OP_LEGACY_SERVER_CONNECT", 0x4)


class _LegacyTolerantAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):  # type: ignore[override]
        ctx = create_urllib3_context()
        ctx.options |= _LEGACY_SERVER_CONNECT_FLAG
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        kwargs["ssl_context"] = ctx
        return super().init_poolmanager(*args, **kwargs)


# Realistic browser headers — some Indian gov sites 403 the default Python
# UA. Set on the session so every fetch shares them; per-request overrides
# (like a custom User-Agent) still win via headers= kwarg.
_DEFAULT_BROWSER_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-IN,en;q=0.9",
    # Skip "br" — `requests` only decodes brotli when the optional `brotli`
    # package is installed; advertising it without support gives back
    # garbled content from sites that prefer it.
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
}


def _make_http_session() -> requests.Session:
    session = requests.Session()
    # Connection pool sized for the parallel-fetch path (up to ~30 probes
    # in flight across distinct hosts during same-host-student probes).
    # max_retries=0 — retrying a 404 / DNS failure / TLS error doesn't
    # help portal discovery; skipping the retry roughly halves worst-case
    # latency on dead probes.
    https_adapter = _LegacyTolerantAdapter(
        pool_connections=40, pool_maxsize=40, max_retries=0,
    )
    http_adapter = HTTPAdapter(
        pool_connections=40, pool_maxsize=40, max_retries=0,
    )
    session.mount("https://", https_adapter)
    session.mount("http://", http_adapter)
    session.verify = False
    session.headers.update(_DEFAULT_BROWSER_HEADERS)
    return session


# Silence the noisy "Unverified HTTPS request is being made" warning — the
# looser verification is a deliberate design choice (see module docstring).
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

HTTP_SESSION: requests.Session = _make_http_session()


@dataclass(frozen=True)
class Candidate:
    url: str
    category: str
    discovery_source: str  # "rule" | "claude" | "platform-probe:{name}" | "link-follow"
    discovery_reasoning: str
    validation_notes: str = ""
    has_password_input: bool = False
    has_login_text: bool = False
    has_student_signal: bool = False
    js_rendered: bool = False
    # Bug 1 — explicit "this is a non-student-audience login" signal
    # derived from `classify_login_audience(body)`. Set on every accept
    # path that has body access; used by the ERP gate in `discovery.run`
    # to drop ONLY ERPs that openly identify as admin/staff (e.g. an
    # ASP.NET form whose control names are `AdminLogin$Password` or a
    # `<title>UNIVERSITY ADMIN LOGIN</title>`). Defaults False so
    # candidates without body access (rule-C bypass with empty static
    # HTML) aren't penalised.
    is_admin_audience: bool = False
    # Fix 2 — set when validation accepted via
    # `passes_login_signal_gate` rule-C (host on
    # `KNOWN_SHARED_PLATFORM_PATTERNS`). Causes the consolidate-time
    # membership re-check (Bug 30 strict) to skip this candidate.
    # NOTE: this LOOSENS cross-OrgID disambiguation for Samarth /
    # state-platform tenants — a foreign-tenant URL that surfaced via
    # search and passed rule-C at validation will no longer be filtered
    # at consolidation. The strict R3/R4 check at admission time
    # (sibling-walk filter, DDG-origin re-check) is the remaining
    # cross-uni guard. Pair this flag with curated `exact_shortnames`
    # in `domain_overrides.json` for OrgIDs that share platform space.
    rule_c_bypass: bool = False


# --- discovery inputs -----------------------------------------------------

# Two neutral query templates. Platform-targeted upfront queries (Samarth /
# DigitalUniversity / Knimbus / Cognibot) are intentionally NOT issued here:
# the agent must DISCOVER first, then accept platform URLs only if they
# surface organically. Pre-emptive platform queries (and subdomain guessing
# like `{shortname}.samarth.edu.in`) led to fabricated URLs being written
# for universities that have no such portal.
_SEARCH_TEMPLATE_NAME: str = "{name} student login"
_SEARCH_TEMPLATE_DOMAIN: str = "{domain} student portal"

# Bug 21 — narrow set of platform-targeted queries we *do* issue, because
# the platform is real, multi-tenant, and the bare-domain subdomain
# (`{shortname}.{platform_root}`) doesn't follow a guessable convention.
# Each entry is a `{name}`-templated DDG query string. Cheap (1 DDG call
# each) — kept conservative so it doesn't bring back the Samarth-style
# fabrication problem.
_PLATFORM_TARGETED_QUERIES: tuple[str, ...] = (
    "{name} bihar UMS login",
)

PATH_PROBES: tuple[tuple[str, str], ...] = (
    ("https://student.{domain}", "Student Portal"),
    ("https://students.{domain}", "Student Portal"),
    ("https://portal.{domain}", "Student Portal"),
    ("https://{domain}/student", "Student Portal"),
    ("https://{domain}/portal", "Student Portal"),
    ("https://lms.{domain}", "LMS/Moodle"),
    ("https://moodle.{domain}", "LMS/Moodle"),
    ("https://elearning.{domain}", "LMS/Moodle"),
    ("https://erp.{domain}", "ERP"),
    ("https://{domain}/erp", "ERP"),
    ("https://exam.{domain}", "Examination Portal"),
    ("https://results.{domain}", "Examination Portal"),
    ("https://fees.{domain}", "Fee Portal"),
    ("https://{domain}/fees", "Fee Portal"),
    ("https://library.{domain}", "Library Portal"),
    ("https://opac.{domain}", "Library Portal"),
)

DDG_HTML_ENDPOINT = "https://html.duckduckgo.com/html/"


# --- Shared platforms -----------------------------------------------------
#
# Roots for each platform are listed **longest-first** so that
# `platform_for_host` matches the most-specific root before the shorter one
# (e.g. `digitaluniversity.ac.in` before `digitaluniversity.ac`).
KNOWN_SHARED_PLATFORMS: tuple[dict, ...] = (
    {
        "name": "samarth",
        "roots": ("samarth.edu.in", "samarth.ac.in"),
        "category": "Student Portal",
        "description": "Govt of India centralized student lifecycle platform",
    },
    {
        "name": "digitaluniversity",
        "roots": ("digitaluniversity.ac.in", "digitaluniversity.ac"),
        "category": "Student Portal",
        "description": "Maharashtra state universities shared platform",
    },
)

# Known suffixes that universities append to their shortname/acronym when
# creating a tenant subdomain on a shared platform (e.g. SPPU → `sppuapp`,
# MJP Rohilkhand → `mjpruadm`). These drive the {acronym}{suffix} probe
# variants. Matching in Filter 2 uses plain startswith, which already
# subsumes these.
SHARED_PLATFORM_SUFFIXES: tuple[str, ...] = (
    "app", "apps", "portal", "admin", "adm", "online",
    "student", "students", "web",
    "ums", "sms", "erp", "academic", "academics",
)


# --- consolidation inputs -------------------------------------------------

_FUNCTIONAL_LABELS_BASE: frozenset[str] = frozenset({
    "www", "web",
    "student", "students", "mystudent", "my",
    "portal", "portals",
    "academic", "academics",
    "exam", "exams", "examination", "examinations",
    "result", "results",
    "fee", "fees", "feeadmin", "payment", "payments",
    "library", "lib", "libraries", "opac",
    "erp", "sap",
    "lms", "moodle", "elearning", "learning",
    "hostel", "hostels",
    "transport",
    "admission", "admissions",
    "alumni", "careers", "placement", "placements",
    "app", "apps", "api",
    "login", "auth", "sso",
    # Subdomain-probe tokens (Bug 8): SIS / SIM / self-service / hallticket /
    # certificate. Without these in the functional-label allow-list, the
    # 3-6-char-alpha rule in `_label_is_college_specific` would drop them
    # back out of Filter 2 even after we discovered them.
    "sim", "sis", "myaccount", "self-service", "selfservice",
    "hallticket", "hallticketnew", "certificate", "transcript", "transcripts",
    # Bug 10 — additional 3-6-char probe tokens. Anything ≥7 chars
    # auto-passes the college-specific heuristic; these short ones need
    # to be allow-listed explicitly.
    "mis", "ums", "vle", "tnp", "elearn", "career",
})

# Distance-learning arms — university-wide systems for enrolled distance
# students, not department-specific. Institution-specific acronyms belong
# in domain_overrides.json.
_DISTANCE_LEARNING_LABELS: frozenset[str] = frozenset({
    "sol", "ncweb", "idol", "cdoe", "cde", "ide", "dde", "udrc", "cdl", "soe",
})

ALLOWED_FUNCTIONAL_LABELS: frozenset[str] = (
    _FUNCTIONAL_LABELS_BASE | _DISTANCE_LEARNING_LABELS
)

COLLEGE_SPECIFIC_WORDS: tuple[str, ...] = (
    "college", "institute", "school", "faculty", "dept", "department",
)

CATEGORY_SUBDOMAIN_HINTS: dict[str, tuple[str, ...]] = {
    "Student Portal": ("student", "students", "portal", "my", "mystudent"),
    "LMS/Moodle": ("lms", "moodle", "elearning", "learning", "vle", "lcms", "elearn"),
    "Fee Portal": ("fee", "fees", "feeadmin", "payment", "payments"),
    "Examination Portal": ("exam", "exams", "examination", "result", "results"),
    "ERP": ("erp", "sap"),
    "Academic Portal": ("academic", "academics"),
    "Hostel Portal": ("hostel", "hostels"),
    "Library Portal": ("library", "lib", "opac", "libraries"),
    "Other": (),
}

LOGIN_PATH_TOKENS: tuple[str, ...] = ("login", "signin", "sign-in", "auth")

# For Filter 2's strong-signal override: the *URL path* must contain one of
# these tokens (stricter than the scoring-pass tokens above — e.g. "/sign/"
# is a concrete path segment like "csdocs.kuk.ac.in/sign/Login.aspx").
_STRONG_SIGNAL_LOGIN_PATH_TOKENS: tuple[str, ...] = (
    "/login", "/signin", "/sign/", "/auth",
)

# --- Student-link discovery on homepages / JS-rendered pages --------------

_STUDENT_LINK_STRONG_PHRASES: tuple[str, ...] = (
    "student login", "student portal", "student access",
)

_STUDENT_LINK_PLATFORM_TOKENS: tuple[str, ...] = ("umis", "ums", "portal")

_STUDENT_LINK_LOGIN_TOKENS: tuple[str, ...] = ("login", "signin")

_STUDENT_LINK_NEGATIVE_TOKENS_2: tuple[str, ...] = (
    "staff", "faculty", "teacher", "admin", "employee",
)

_STUDENT_LINK_NEGATIVE_TOKENS_3: tuple[str, ...] = (
    "alumni", "recruitment", "vendor",
)

_LINK_INELIGIBLE_PREFIXES: tuple[str, ...] = (
    "#", "mailto:", "tel:", "javascript:",
)

_ACRONYM_STOPWORDS: frozenset[str] = frozenset({
    "the", "of", "and", "for", "a", "an",
    # Honorific prefixes that precede an institution's name but are not
    # part of its acronym. "Dr. Balasaheb Sawant Konkan Krishi
    # Vidyapeeth" → BSKKV (not DBSKKV); the institution's own shared-
    # platform tenant keys off the honorific-free acronym.
    "dr", "prof", "shri", "sri", "smt", "late", "kum", "mr", "ms", "mrs",
})


# =================================================================== utils

def parse_domains(raw: str) -> list[str]:
    return [d.strip().lower().lstrip(".") for d in (raw or "").split(",") if d.strip()]


# Subdomain labels that are never student portals — skip them when they turn
# up in certificate-transparency enumeration (infra/mail/dev hosts).
_NON_PORTAL_CT_LABELS: frozenset[str] = frozenset({
    "mail", "webmail", "smtp", "imap", "pop", "pop3", "mx", "mx1", "mx2",
    "email", "ns", "ns1", "ns2", "ns3", "dns", "vpn", "ftp", "sftp",
    "cpanel", "whm", "webdisk", "autodiscover", "autoconfig", "cpcalendars",
    "cpcontacts", "test", "dev", "staging", "demo", "backup", "cdn",
    "static", "assets", "img", "media", "video", "stream", "git", "gitlab",
    "jenkins", "proxy", "gateway", "remote", "owa", "exchange", "voip",
})


def crt_sh_subdomains(domain: str, timeout: float = 15.0, cap: int = 40) -> list[str]:
    """Enumerate subdomains of `domain` from crt.sh certificate-transparency
    logs. Surfaces non-obvious portal hosts (e.g. ``cmsys.eng.rizvi.edu.in``)
    that the agent can neither guess from a functional-label list nor find via
    homepage links. Best-effort: any network / parse failure returns []."""
    domain = (domain or "").lower().strip().lstrip(".")
    if not domain:
        return []
    try:
        r = HTTP_SESSION.get(
            f"https://crt.sh/?q=%25.{domain}&output=json",
            timeout=timeout, verify=False,
        )
        if r.status_code != 200:
            return []
        data = r.json()
    except Exception:
        return []
    subs: set[str] = set()
    for row in data:
        for nv in str(row.get("name_value", "")).split("\n"):
            h = nv.strip().lower().lstrip("*.")
            if not h or h == domain or not h.endswith("." + domain):
                continue
            prefix = h[: -(len(domain) + 1)]
            if prefix.split(".")[0] in _NON_PORTAL_CT_LABELS:
                continue
            subs.add(h)
    return sorted(subs)[:cap]


# --- URL normalisation / session-ID stripping -----------------------------

_SESSION_ID_PATH_RE = re.compile(r";jsessionid=[^/?#]*", re.IGNORECASE)


_DEFAULT_PORTS: dict[str, int] = {"http": 80, "https": 443}

# Tracking / referral query parameters stripped during URL normalisation.
# These never identify a different resource — they're for analytics or
# attribution. Stripping makes dedup keys agree across discovery sources
# (homepage anchor, DDG, forum link, etc.) that surface the same login
# URL with different attribution tags.
#
# Conservative on potentially-meaningful keys: `from`, `source`, `via`
# CAN be returnto-style params on some sites (e.g. `/login?from=/dash`)
# but Indian-uni login URLs in our corpus don't use them that way; the
# upside (stable dedup) outweighs the rare loss of returnto context.
TRACKING_QUERY_PARAMS: frozenset[str] = frozenset({
    "ref", "referrer",
    "utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term",
    "fbclid", "gclid", "msclkid", "mc_cid", "mc_eid",
    "source", "from", "via",
})


def _strip_tracking_params(query: str) -> str:
    """Drop `TRACKING_QUERY_PARAMS` keys from the `?...` query string,
    preserving order and any non-tracking params. Empty result returns
    "". Case-insensitive on the key name.
    """
    if not query:
        return ""
    kept_pairs: list[str] = []
    for pair in query.split("&"):
        if not pair:
            continue
        key, _eq, _val = pair.partition("=")
        if key.lower() in TRACKING_QUERY_PARAMS:
            continue
        kept_pairs.append(pair)
    return "&".join(kept_pairs)


def _strip_default_port(scheme: str, netloc: str) -> str:
    """Drop ``:80`` from http URLs and ``:443`` from https URLs. Other
    explicit ports are preserved (e.g. ``:8443`` stays put). The hostname
    is also lowercased — netloc is the only canonical-form lever the URL
    spec gives us.
    """
    s = (scheme or "").lower()
    if not netloc:
        return netloc
    host_part = netloc.lower()
    if ":" in host_part:
        host, _, port = host_part.rpartition(":")
        if port.isdigit() and int(port) == _DEFAULT_PORTS.get(s, -1):
            return host
    return host_part


def normalize_url(url: str) -> str:
    """Bug B canonicalisation. Beyond `strip_session_ids`, also:
      * lowercase the host and drop the default port (``:80`` for http,
        ``:443`` for https) — `sist.knimbus.com:443/...` → `sist.knimbus.com/...`,
      * strip the fragment (``#/?signin=true`` → empty) — fragments are
        client-side state, never identify a different resource.
    Path and query are preserved as-is. Idempotent.

    `urlsplit` returns a 5-tuple `(scheme, netloc, path, query, fragment)`
    — there is no `params` field (that's `urlparse`'s 6-tuple). Passing
    6 values to `urlunsplit` raises `ValueError: too many values to
    unpack`.
    """
    if not url:
        return url
    cleaned = strip_session_ids(url)
    p = urlsplit(str(cleaned))
    netloc = _strip_default_port(p.scheme, p.netloc)
    # Strip tracking / referral params (`?ref=worldsitelink`,
    # `utm_*`, `fbclid`, `gclid`, …) so dedup keys agree across
    # discovery sources that decorate the URL with different
    # attribution tags.
    query = _strip_tracking_params(p.query or "")
    # Fragments are normally dropped (disposable client-side state). For
    # hash-routed SPA platforms (Core Campus etc.) the fragment IS the
    # login route, so preserve it for those hosts only.
    host = (netloc or "").lower().split(":")[0]
    keep_fragment = host and any(
        host == root or host.endswith("." + root)
        for root in HASH_ROUTED_PLATFORM_ROOTS
    )
    fragment = p.fragment if keep_fragment else ""
    return urlunsplit((p.scheme.lower(), netloc, p.path, query, fragment))


def canonicalize_url(url: str) -> str:
    """Bug B — full URL canonicalisation: strip session IDs / default
    port / fragment, then apply any `KNOWN_SHARED_PLATFORM_PATTERNS`
    canonical_path rewrite. The single helper called by validation
    accept-paths so dedup keys, stored URLs, and sheet output all agree
    on the canonical form.
    """
    return canonical_url_for_known_platform(normalize_url(url))


def canonical_url_for_known_platform(url: str) -> str:
    """Bug B — when `url`'s host is on a `KNOWN_SHARED_PLATFORM_PATTERNS`
    entry whose pattern has a ``canonical_path`` field, replace the URL's
    path/query with the canonical form. Used for platforms whose
    unauthenticated-redirect target differs from their bookmarkable
    login URL (e.g. knimbus.com tenants redirect
    ``/portal/v2/default/login`` → ``/portal/v2/default/landingPage#/?signin=true``).
    Returns the URL unchanged when no pattern matches or no
    ``canonical_path`` is set.
    """
    if not url:
        return url
    p = urlsplit(str(url))
    host = (p.netloc or "").lower().split(":")[0]
    if not host:
        return url
    for pattern, meta in KNOWN_SHARED_PLATFORM_PATTERNS.items():
        if not (host == pattern or host.endswith("." + pattern)):
            continue
        canonical_path = meta.get("canonical_path") if isinstance(meta, dict) else None
        if not canonical_path:
            return url
        # Preserve scheme + netloc; drop query and fragment along with
        # the original path. The platform pattern guarantees the
        # canonical path is the right login surface for every tenant.
        # `urlunsplit` is 5-tuple (scheme, netloc, path, query, fragment).
        netloc = _strip_default_port(p.scheme, p.netloc)
        return urlunsplit((p.scheme.lower(), netloc, canonical_path, "", ""))
    return url


def strip_session_ids(url: str) -> str:
    """Remove `;jsessionid=...` from path and `jsessionid=...` from query.

    Session IDs are per-session, expire quickly, and should never be stored
    or used as dedup keys. Applied during URL normalisation so both dedup
    and sheet output agree.
    """
    if not url:
        return url
    p = urlsplit(str(url))
    path = _SESSION_ID_PATH_RE.sub("", p.path or "")
    query = p.query or ""
    if query:
        pairs = [
            pair for pair in query.split("&")
            if pair and not pair.lower().startswith("jsessionid=")
        ]
        query = "&".join(pairs)
    return urlunsplit((p.scheme, p.netloc, path, query, p.fragment))


# --- JS-shell suspicion scoring -------------------------------------------

_SPA_ROOT_MARKERS: tuple[re.Pattern, ...] = (
    re.compile(r'\bid\s*=\s*["\']root["\']', re.IGNORECASE),
    re.compile(r'\bid\s*=\s*["\']app["\']', re.IGNORECASE),
    re.compile(r'\bdata-reactroot\b', re.IGNORECASE),
    re.compile(r'\bng-app\b', re.IGNORECASE),
    re.compile(r'\bdata-vue-root\b', re.IGNORECASE),
)
_NOSCRIPT_RE = re.compile(r"<noscript[^>]*>(.*?)</noscript>", re.IGNORECASE | re.DOTALL)
_SCRIPT_SRC_RE = re.compile(r"<script[^>]*\bsrc\s*=", re.IGNORECASE)


_LOGIN_PATH_TOKENS_FOR_SUSPICION: tuple[str, ...] = ("/login", "/signin", "/auth")

# Section 1 — host subdomain labels that indicate "this host serves a
# student-facing portal". When the leftmost subdomain matches one of
# these AND the URL is at the root path AND the static body is small
# (<2000 bytes), the page is overwhelmingly likely to be a React/Vue
# SPA shell whose login form arrives only after JS hydration. Single
# strong signal pushes Playwright unconditionally.
_LOGIN_SUBDOMAIN_INDICATORS: frozenset[str] = frozenset({
    "student", "students", "studentportal",
    "erp", "ums", "sis", "sim", "mis",
    # Indian-uni MIS / SIS / SMS variants and platform-shaped labels.
    # Kept in sync with `_LOGIN_SUBDOMAIN_LABELS_SPA` in discovery.py.
    "sms", "spoc", "sap", "eportal", "myportal",
    "portal",
    "lms", "elearn", "elearning", "moodle",
    "exam", "exams", "examportal",
    "result", "results",
    "fee", "fees", "feeportal",
    "library", "lib",
    "placement", "tnp",
    "signin", "signon", "auth",
    # Self-service / student-services subdomain labels (JNTUH OSS
    # at `studentservices.jntuh.ac.in` and similar).
    "studentservices", "services", "selfservice", "self-service",
})

# Path shapes considered "the root" for login-subdomain detection.
# Kept in sync with `_LOGIN_SUBDOMAIN_ROOT_PATHS` in discovery.py.
_ROOT_PATH_SHAPES_FOR_SUSPICION: frozenset[str] = frozenset({
    "", "/", "/index.html", "/index.htm", "/index.php", "/home",
    # ASP.NET-built university homepages (e.g. older `.NET` portals
    # whose apex returns a redirect to `/default.aspx`).
    "/default.aspx",
})


def js_shell_suspicion_score(url: str, body: str) -> int:
    """Heuristic score for whether a static page is actually a JS shell.

    Scoring (must reach `JS_RENDERING_SUSPICION_THRESHOLD` = 3 to escalate
    to Playwright):

      Strong (+2):
        * "JavaScript disabled" exact phrase in body.
        * "Please enable JavaScript" (or close variant) in body.

      Strong+ (+3 — single signal hits threshold alone):
        * Bug A — body length < 2000 chars AND URL path contains
          ``/login`` / ``/signin`` / ``/auth``. A tiny page at a
          login-ish URL is almost always a JS-rendered SPA shell
          (e.g. `erp.sathyabama.ac.in/account/login` — 1170-char body,
          no static `<form>`, login form arrives only after hydration).
          +3 so the SPA-marker `<div id="root">` regex doesn't have to
          additionally match for Playwright to fire.

      Medium (+1):
        * A `<noscript>` block whose content mentions "enable" or "javascript".
        * Body has fewer than 500 chars but ≥3 `<script src=...>` tags
          (classic SPA shell — entire app delivered via JS bundles).
        * Root SPA mount markers present (`#root`, `#app`, `[data-reactroot]`,
          `[ng-app]`, `[data-vue-root]`).

    A page therefore needs at least one strong + one medium indicator
    (or two strong, or one tiny-body-at-login) to trigger Playwright.
    Avoids spinning up Chromium for every slightly-empty page.
    """
    if not body:
        return 0
    score = 0
    lower = body.lower()

    # Strong signals.
    if "javascript disabled" in lower:
        score += 2
    if (
        "please enable javascript" in lower
        or "enable javascript in your browser" in lower
        or "this site requires javascript" in lower
    ):
        score += 2

    # Strong+ (Bug A) — tiny body at a login-shaped URL. Bumped to +3
    # so the single signal alone clears the threshold (= 3). Otherwise
    # SPA shells whose mount markers don't match the regex set
    # (e.g. `<div id="erp-app">` instead of `<div id="app">`) score
    # only +2 here and never escalate to Playwright.
    parsed = urlsplit(url) if url else None
    path_lower = ((parsed.path or "") if parsed else "").lower()
    if len(body) < 2000:
        if any(tok in path_lower for tok in _LOGIN_PATH_TOKENS_FOR_SUSPICION):
            score += 3

    # Strong+ (Section 1) — login-shaped subdomain at root path with a
    # tiny static body. `student.mitapps.in/` returns a 44-byte React
    # shell — none of the static-content signals fire (no <noscript>,
    # no SPA mount marker visible in 44 bytes, body too small for
    # script-tag heuristic). The leftmost subdomain label tells us
    # what the host IS (student / erp / portal / …) regardless of
    # body content; +3 single-signal escalation pushes Playwright,
    # which then hydrates the React app and either sees the form
    # directly or the `_try_click_login_button` path follows the
    # login button to the real URL (e.g. /itxlogin).
    if parsed is not None and len(body) < 2000:
        host = (parsed.netloc or "").lower().split(":")[0]
        leftmost = host.split(".", 1)[0] if "." in host else host
        if (
            leftmost in _LOGIN_SUBDOMAIN_INDICATORS
            and (path_lower.rstrip("/") or path_lower) in _ROOT_PATH_SHAPES_FOR_SUSPICION
        ):
            score += 3

    # Medium: <noscript> block that explicitly mentions enable / javascript.
    ns_match = _NOSCRIPT_RE.search(body)
    if ns_match:
        ns_content = ns_match.group(1).lower()
        if "enable" in ns_content or "javascript" in ns_content:
            score += 1

    # Medium: tiny body with multiple <script src> tags (classic SPA shell).
    if len(body) < 500 and len(_SCRIPT_SRC_RE.findall(body)) >= 3:
        score += 1

    # Medium: SPA root mount markers.
    if any(p.search(body) for p in _SPA_ROOT_MARKERS):
        score += 1

    return score


def extract_shortname_candidates(
    domains: list[str],
    extra_root_domains: list[str] | None = None,
) -> set[str]:
    """Leftmost label of each **root** configured domain (subdomains of
    another configured domain are excluded)."""
    all_d = [
        d.strip().lower().lstrip(".")
        for d in list(domains) + list(extra_root_domains or [])
        if d
    ]
    roots: list[str] = []
    for d in all_d:
        if any(d != o and d.endswith("." + o) for o in all_d):
            continue
        roots.append(d)
    out: set[str] = set()
    for d in roots:
        label = d.split(".")[0]
        if len(label) >= 2:
            out.add(label)
    return out


def compute_acronym(name: str) -> str | None:
    """Return the uppercase acronym of `name`, or None if shorter than 3 chars.

    Skips stopwords ("the", "of", "and", "for", "a", "an"). Uses the first
    alphabetic character of each remaining whitespace-separated word.
    """
    if not name:
        return None
    # Strip surrounding punctuation before the stopword test so "Dr." and
    # "&" don't slip past the honorific/stopword filter.
    words = [
        w for w in name.split()
        if w.strip(".,&()").lower() not in _ACRONYM_STOPWORDS
    ]
    letters: list[str] = []
    for word in words:
        for ch in word:
            if ch.isalpha():
                letters.append(ch.upper())
                break
    acronym = "".join(letters)
    return acronym if len(acronym) >= 3 else None


def platform_for_host(host: str) -> tuple[dict, str] | None:
    """If `host` is a subdomain of one of `KNOWN_SHARED_PLATFORMS`, return
    `(platform, subdomain_prefix)`. The bare root is not considered a
    tenant host and returns None.
    """
    for platform in KNOWN_SHARED_PLATFORMS:
        for root in platform["roots"]:
            if host == root:
                return None
            if host.endswith("." + root):
                prefix = host[: -(len(root) + 1)]
                return platform, prefix
    return None


def is_shared_platform_for_university(
    url_host: str,
    *,
    label_candidates: set[str] | frozenset[str],
    acronym_candidates: set[str] | frozenset[str],
) -> bool:
    """True if `url_host` is a shared-platform subdomain belonging to this
    university (by label or acronym match, per the rules in this module's
    docstring)."""
    match = platform_for_host(url_host)
    if match is None:
        return False
    _platform, subdomain = match
    sd = subdomain.lower()
    for label in label_candidates:
        lab = label.lower()
        if len(lab) < 2:
            continue
        if sd == lab or sd.startswith(lab):
            return True
    for ac in acronym_candidates:
        acl = ac.lower()
        if len(acl) < 3:
            continue
        if sd == acl or sd.startswith(acl):
            return True
    return False


def all_platform_roots() -> list[str]:
    """Flat list of every platform root (used to extend effective_domains)."""
    out: list[str] = []
    for p in KNOWN_SHARED_PLATFORMS:
        for r in p["roots"]:
            if r not in out:
                out.append(r)
    return out


def _base_host(url: str) -> str:
    p = urlsplit(url)
    return f"{p.scheme.lower()}://{p.netloc.lower().split(':')[0]}"


def _host_of(url: str) -> str:
    return urlsplit(url).netloc.lower().split(":")[0]


def _subdomain_labels(host: str, domains: list[str]) -> tuple[list[str], str | None]:
    for d in domains:
        if host == d:
            return [], d
        if host.endswith("." + d):
            prefix = host[: -(len(d) + 1)]
            return prefix.split("."), d
    return [], None


def _host_under_any(host: str, roots: list[str]) -> bool:
    for r in roots:
        if host == r or host.endswith("." + r):
            return True
    return False


# ========================================================= category inference

def infer_category(
    url: str,
    *,
    fallback: str = "Other",
    page_body: str | None = None,
) -> str:
    parsed = urlsplit(url)
    host = parsed.netloc.lower()
    path = (parsed.path or "").lower()

    # Shared-platform hosts → platform's declared category (usually Student Portal).
    for platform in KNOWN_SHARED_PLATFORMS:
        for root in platform["roots"]:
            if host.endswith("." + root):
                return platform["category"]

    def host_has(*tokens: str) -> bool:
        return any(tok in host for tok in tokens)

    def path_has(*segments: str) -> bool:
        return any(seg in path for seg in segments)

    # LMS/Moodle: multi-signal score, threshold ≥2. Runs BEFORE the other
    # buckets because a "learning.x.edu" host should classify as LMS even
    # if the path also has /exam etc.
    if score_lms_signals(host=host, path=path, page_body=page_body) >= 2:
        return "LMS/Moodle"

    # Admission URLs are filtered at validation and shouldn't reach here.
    if (host_has("exam", "result", "hallticket", "certificate", "transcript")
            or path_has("/exam", "/result", "/hall-ticket", "/transcript",
                        "/certificate", "/admit-card", "/admitcard")):
        return "Examination Portal"
    if host_has("fee", "feeadmin", "payment") or path_has("/fee", "/payment"):
        return "Fee Portal"
    if host_has("library", "lib", "opac", "duls") or path_has("/library", "/opac"):
        return "Library Portal"
    if host_has("erp", "sap") or path_has("/erp"):
        return "ERP"
    if host_has("hostel"):
        return "Hostel Portal"
    if host_has("academic", "grade", "records") or path_has("/academic", "/grade"):
        return "Academic Portal"

    return fallback


# ----------------------------------------------------- LMS/Moodle scoring

def score_lms_signals(
    *,
    host: str,
    path: str,
    page_body: str | None = None,
) -> int:
    """Score-based LMS/Moodle detection. ≥2 confirms; 0/1 falls through to
    other classifiers. Independent signals stack (so an LMS host on a
    third-party LMS provider scores 3, etc.).

    Strong (score 2):
      * Path is exactly `/login/index.php` (Moodle's stock login URL) or
        contains `/moodle` anywhere.
      * Host substring matches any of `LMS_HOST_TOKENS`
        (moodle/lms/elearning/learning/vle/lcms/elearn).

    Medium (score 1):
      * Host suffix-matches any of `LMS_THIRD_PARTY_HOSTS` (talentlms,
        canvaslms, blackboard, etc.).
      * `page_body` contains "moodle" / "powered by moodle" /
        moodle CSS class prefix / `<meta name="generator">…Moodle…` /
        the Moodle "Cookies must be enabled in your browser" boilerplate.
        Each independent body-content signal scores 1; only counted when
        a body is supplied (URL-only callers skip this section).
    """
    score = 0
    # Strong: path patterns
    if path == "/login/index.php" or path.endswith("/login/index.php"):
        score += 2
    if "/moodle" in path:
        score += 2
    # Strong: host subdomain tokens
    if any(tok in host for tok in LMS_HOST_TOKENS):
        score += 2
    # Medium: third-party LMS provider
    for h in LMS_THIRD_PARTY_HOSTS:
        if host == h or host.endswith("." + h):
            score += 1
            break
    # Medium: body-content signals
    if page_body:
        body_lower = page_body.lower()
        if "moodle" in body_lower:
            score += 1
        if "powered by moodle" in body_lower:
            score += 1
        if 'class="moodle-' in body_lower or 'class="mod-' in body_lower:
            score += 1
        if 'name="generator"' in body_lower and "moodle" in body_lower:
            score += 1
        if "cookies must be enabled in your browser" in body_lower:
            score += 1
    return score


# ============================================================ DDG searches

def run_searches(
    university_name: str,
    *,
    domains: list[str],
    max_results_per_query: int,
    http_timeout: float,
    user_agent: str,
    max_workers: int = 4,
) -> list[Candidate]:
    """Run the two neutral discovery queries in parallel.

    Issues one name-based query (`<name> student login`) and one domain-based
    query (`<domain> student portal`) per entry in `domains`. Most OrgIDs
    pass a single domain — multi-domain rows (or OrgIDs with an
    `extra_effective_domains` override) get a domain query each so we
    discover portals living on a non-primary university domain.

    Platform-targeted queries are intentionally absent — see the module
    docstring on `_SEARCH_TEMPLATE_NAME`. If a Samarth / DigitalUniversity /
    Knimbus / Cognibot URL is the right answer for this university, the
    name- or domain-based search will surface it.
    """
    queries: list[tuple[str, str]] = []
    queries.append((_SEARCH_TEMPLATE_NAME.format(name=university_name), "Student Portal"))
    seen_domains: set[str] = set()
    for d in domains:
        if not d or d in seen_domains:
            continue
        seen_domains.add(d)
        queries.append(
            (_SEARCH_TEMPLATE_DOMAIN.format(domain=d), "Student Portal")
        )
    # Bug 21 — narrow platform-targeted queries (Bihar UMS). Always issued;
    # they're cheap and only return relevant results when applicable.
    for tmpl in _PLATFORM_TARGETED_QUERIES:
        queries.append((tmpl.format(name=university_name), "Student Portal"))

    def _run(query: str, category: str) -> list[Candidate]:
        try:
            urls = _ddg_html_search(
                query,
                http_timeout=http_timeout,
                user_agent=user_agent,
                max_results=max_results_per_query,
            )
        except Exception as err:
            logger.warning("DDG search failed for %r: %s", query, err)
            return []
        # Bug 1 — log every DDG query at INFO with raw URL count so
        # zero-result OrgIDs are debuggable without enabling DEBUG.
        logger.info("DDG query: %r → %d urls", query, len(urls))
        return [
            Candidate(
                url=url,
                category=category,
                discovery_source="rule",
                discovery_reasoning=f"search: {query}",
            )
            for url in urls
        ]

    out: list[Candidate] = []
    if not queries:
        return out
    with ThreadPoolExecutor(max_workers=min(max_workers, len(queries))) as exe:
        futures = [exe.submit(_run, q, c) for q, c in queries]
        for f in as_completed(futures):
            out.extend(f.result())

    # Bug 1 — broader fallback when the standard queries returned
    # nothing. Run two extra queries:
    #   1. Name-only "<name> student portal login" (no extra
    #      domain pinning).
    #   2. `site:<domain> login` for each owned domain — limits
    #      results to the domain's own pages, useful when the
    #      university name is ambiguous (multiple MITs / IIITs).
    # Each fallback query is logged the same way so operators can
    # see what was actually sent.
    if not out:
        fallback_queries: list[tuple[str, str]] = []
        if university_name:
            fallback_queries.append((
                f"{university_name} student portal login",
                "Student Portal",
            ))
        for d in domains:
            if not d:
                continue
            fallback_queries.append((f"site:{d} login", "Student Portal"))
        if fallback_queries:
            logger.info(
                "DDG search returned 0 candidates; trying %d broader fallback queries",
                len(fallback_queries),
            )
            with ThreadPoolExecutor(
                max_workers=min(max_workers, len(fallback_queries))
            ) as exe:
                futures = [exe.submit(_run, q, c) for q, c in fallback_queries]
                for f in as_completed(futures):
                    out.extend(f.result())

    # Google fallback when DDG (primary + broader) returns 0. DDG misses
    # many smaller Indian universities that Google indexes, e.g. St.
    # Xavier's Ranchi (`www.sxcran.org/Student/login`). Best-effort: the
    # `googlesearch-python` dep is optional and rate-limited, so we run a
    # single representative query and swallow any errors.
    if not out and university_name:
        google_query = f"{university_name} student portal login"
        logger.info(
            "DDG returned 0 candidates after fallback; trying Google search: %r",
            google_query,
        )
        google_urls = _google_search(google_query, max_results=max_results_per_query)
        logger.info("Google query: %r → %d urls", google_query, len(google_urls))
        for url in google_urls:
            out.append(
                Candidate(
                    url=url,
                    category="Student Portal",
                    discovery_source="rule",
                    discovery_reasoning=f"google search: {google_query}",
                )
            )
    return out


def host_is_known_shared_platform(host: str) -> bool:
    """True if `host` matches any KNOWN_SHARED_PLATFORM_PATTERNS pattern
    (samarth, digitaluniversity, knimbus, cognibot, myloft, …). Used to
    exempt third-party platform hosts from the off-domain pre-filter
    reject so the short-circuit can fire."""
    if not host:
        return False
    for pattern in KNOWN_SHARED_PLATFORM_PATTERNS:
        if host == pattern or host.endswith("." + pattern):
            return True
    return False


def is_foreign_academic_host(host: str) -> bool:
    """True if `host` is on a foreign country / academic TLD. Every
    institution in this project is Indian, so such a host is a same-brand
    campus abroad, not the target portal (e.g. `moodle.amity.ac.uk`
    matching the Indian 'amity' shortname). Generic gTLDs are not flagged."""
    if not host:
        return False
    h = host.lower().rstrip(".")
    return any(h == t.lstrip(".") or h.endswith(t) for t in FOREIGN_ACADEMIC_TLDS)


def is_nonstudent_platform_tenant(host: str) -> bool:
    """True if `host` is a Samarth / state-platform tenant whose label
    marks it a RECRUITMENT or ADMISSION portal — never an enrolled-student
    login. Catches `mgahvrec.samarth.edu.in` (recruitment),
    `<inst>admission.samarth.edu.in`, and the bare functional tenants
    `recruitment.samarth.edu.in` / `admissions.samarth.edu.in`.

    Policy veto (user-requested): applied unconditionally at consolidation,
    independent of whether a student peer tenant exists.
    """
    if not host:
        return False
    h = host.lower().lstrip(".")
    roots: list[str] = list(_SAMARTH_PLATFORM_ROOTS)
    for st_hosts in STATE_PLATFORM_HINTS.values():
        roots.extend(p.lower().lstrip(".") for p in st_hosts if p)
    for root in roots:
        if not root or not h.endswith("." + root):
            continue
        # Tenant label = leftmost label of the prefix before the platform root.
        prefix = h[: -(len(root) + 1)].split(".")[-1]
        for suf in SAMARTH_NONSTUDENT_TENANT_SUFFIXES:
            # Exact functional tenant (`recruitment.samarth`) or an
            # `<acronym><suffix>` tenant (`mgahvrec`, `csjmuadmission`).
            if prefix == suf or (prefix.endswith(suf) and len(prefix) > len(suf)):
                return True
    return False


# Samarth platform roots — used by the strict membership rule (Bug 30).
# Treated as ambiguous tenant hosts: any tenant subdomain must strictly
# match this OrgID's `exact_shortnames` to avoid cross-university leakage
# (e.g. pup.samarth.ac.in [Punjabi U.] leaking into Patna's row).
_SAMARTH_PLATFORM_ROOTS: tuple[str, ...] = ("samarth.edu.in", "samarth.ac.in")


_HOMEPAGE_PATH_SHAPES: frozenset[str] = frozenset({
    "", "/index.html", "/index.htm", "/index.php", "/home",
    "/default.aspx", "/default.htm", "/default.html",
})


def _is_university_homepage(url: str, primary_domain: str) -> bool:
    """Section 9 — true iff `url` resolves to the bare university
    homepage of `primary_domain` (or its `www.` variant). The homepage
    itself is never a student portal — when rule-B fired and link-
    follow couldn't upgrade to a real login URL, the homepage URL
    sticks around and would otherwise leak into the final result.
    Used by `consolidate_candidates` to drop those leftover homepages.

    A URL counts as "homepage" when the host equals `primary_domain`
    or `www.<primary_domain>` AND the path is one of the canonical
    "root" shapes (empty, `/index.html`, `/home`, etc.) — slash and
    case insensitive.
    """
    if not primary_domain:
        return False
    p = urlsplit(url or "")
    host = (p.netloc or "").lower().split(":")[0]
    primary_n = primary_domain.lower().lstrip(".")
    if host != primary_n and host != f"www.{primary_n}":
        return False
    path = (p.path or "").rstrip("/").lower()
    return path in _HOMEPAGE_PATH_SHAPES


def _host_needs_strict_tenant_check(host: str, state: str | None) -> bool:
    """Section 4 — true iff `host` is on a Samarth or state-platform
    root, where cross-OrgID disambiguation matters. Used by
    `consolidate_candidates` to decide whether a `rule_c_bypass`
    candidate should still run through the strict R3/R4 tenant check.

    Returns True when host is on:
      * any of `_SAMARTH_PLATFORM_ROOTS` — every Samarth tenant looks
        like a known platform but belongs to ONE specific OrgID; we
        must verify the prefix matches `exact_shortnames` /
        `auto_shortnames`.
      * any entry of `STATE_PLATFORM_HINTS.values()` — same dynamic
        for `bihar-ums.com`, `digitaluniversity.ac`, etc.

    Returns False when host is on a "neutral" multi-tenant platform
    (knimbus.com / cognibot.in / myloft.xyz) that doesn't share
    wildcard semantics — these are safe to admit via rule-C alone.
    `state` is unused here (kept for forward-compat with possible
    state-aware refinements).
    """
    del state  # currently unused; reserved for future per-state logic
    if not host:
        return False
    h = host.lower().lstrip(".").split(":")[0]
    for root in _SAMARTH_PLATFORM_ROOTS:
        if h == root or h.endswith("." + root):
            return True
    for st_hosts in STATE_PLATFORM_HINTS.values():
        for plat in st_hosts:
            plat_n = plat.lower().lstrip(".")
            if not plat_n:
                continue
            if h == plat_n or h.endswith("." + plat_n):
                return True
    return False


def host_belongs_to_org(
    host: str,
    *,
    primary: str,
    extra_effective_domains: list[str],
    state: str | None,
    exact_shortnames: list[str],
    portal_anchored_hosts: set[str] | frozenset[str],
    domains: list[str] | tuple[str, ...] = (),
    auto_shortnames: list[str] | tuple[str, ...] | set[str] | frozenset[str] = (),
    acronym: str = "",
) -> tuple[bool, str]:
    """Bug 30 — strict per-OrgID host membership rule.

    A candidate host belongs to OrgID X iff at least one of these holds
    (in order; first hit wins, foreign-state reject runs first):

    (R0) Bug 43 — host is on a *foreign* state-platform root (any entry
        of `STATE_PLATFORM_HINTS[other_state]`) → REJECT. Stops a
        Tamil-Nadu uni from accepting `nou.bihar-ums.com`.
    (1) Host == primary, any SheerID-listed `domains` entry, or any
        `extra_effective_domain` for X.
    (2) Host is a subdomain of any of those (Bug 41 — previously only
        primary + extras were checked; multi-domain SheerID rows like
        Sathyabama (`sathyabamauniversity.ac.in` primary +
        `sathyabama.ac.in` secondary) would otherwise reject
        `feeportal.sathyabama.ac.in`).
    (3) Host is on a state-platform domain (STATE_PLATFORM_HINTS[X.state])
        AND the institutional subdomain prefix is in X's `exact_shortnames`
        (after stripping a known functional prefix — see Bug 31; falls
        back to `auto_shortnames` when no `exact_shortnames` is set —
        see Bug 1).
    (4) Host is on samarth.edu.in / samarth.ac.in AND the tenant subdomain
        prefix is in X's `exact_shortnames`
        (after stripping a known functional prefix — see Bug 31; falls
        back to `auto_shortnames` when no `exact_shortnames` is set —
        see Bug 1).
    (5) Host was reached by following a portal-pattern outbound anchor on
        the verified primary homepage (Bug 22 portal-anchored sibling).
    (6) Shortname-in-domain. Any shortname in
        `exact_shortnames ∪ auto_shortnames` (≥4 chars) appears at the
        start of the host's eTLD+1 leftmost label OR as a `.<name>.`
        label inside the full host. Catches abbreviated sibling
        domains like `ccsuforms.in` (`ccsu`) and `jnvuiums.in` (`jnvu`)
        even when no override exists, because `auto_shortnames` is the
        leftmost-label set extracted from configured domains.
    (7) Bug 41 — host matches `KNOWN_SHARED_PLATFORM_PATTERNS` (knimbus,
        cognibot, …, EXCLUDING state-platforms which are gated by
        rule R0). Cross-state contamination is already handled by R0.

    Rules (3) and (4) are GATES: when the host is on one of these
    ambiguous platform roots, the strict shortname match is required and
    rules (1)/(2) do NOT override. This is what disambiguates two
    universities sharing a state-UMS platform (e.g. Patna vs Patliputra
    on bihar-ums.com) when one of them happens to list the platform root
    in `extra_effective_domains`.

    Returns `(ok, reason)`. The caller is expected to log a WARNING on
    rejection so cross-contamination is observable.
    """
    if not host:
        return False, "empty host"
    h = host.lower().lstrip(".")
    primary_n = (primary or "").lower().lstrip(".")
    owned: list[str] = []
    if primary_n:
        owned.append(primary_n)
    # Bug 41 — every SheerID-listed domain (not just `domains[0]`) goes
    # into the owned set. Without this, a multi-domain SheerID row's
    # secondary-domain subdomains fall through to "shortname/state
    # mismatch" and the whole university returns zero portals.
    for d in domains or ():
        d_n = (d or "").lower().lstrip(".")
        if d_n and d_n not in owned:
            owned.append(d_n)
    for d in extra_effective_domains or []:
        d_n = (d or "").lower().lstrip(".")
        if d_n and d_n not in owned:
            owned.append(d_n)
    exact_lower = {s.lower() for s in (exact_shortnames or []) if s}

    # ---- R0: Bug 43 foreign-state-platform reject ------------------
    # Build the set of state-platform hosts that DO belong to this
    # OrgID's state (so we don't reject our own platform).
    own_state_doms: tuple[str, ...] = (
        STATE_PLATFORM_HINTS.get(state, ()) if state else ()
    )
    own_state_set = {d.lower().lstrip(".") for d in own_state_doms}
    for st_hosts in STATE_PLATFORM_HINTS.values():
        for plat in st_hosts:
            plat_n = plat.lower().lstrip(".")
            if not plat_n or plat_n in own_state_set:
                continue
            if h == plat_n or h.endswith("." + plat_n):
                return False, (
                    f"foreign state-platform host {plat_n!r}: OrgID's "
                    f"state {state!r} doesn't include this platform"
                )

    # ---- R3 + R4: own-state + samarth tenant gates ----------------
    # When host is on a state-platform or samarth root, the strict
    # shortname check is authoritative — we don't fall through to
    # (1)/(2). This keeps a state-platform listed in
    # `extra_effective_domains` (e.g. bihar-ums.com for Patna) from
    # trivially admitting every tenant subdomain.
    platform_roots = tuple(d.lower().lstrip(".") for d in own_state_doms) + _SAMARTH_PLATFORM_ROOTS

    for plat in platform_roots:
        if not plat:
            continue
        if h == plat:
            return False, f"bare platform root {plat} (no tenant)"
        if h.endswith("." + plat):
            prefix = h[: -(len(plat) + 1)]
            # The institutional/tenant label is typically a single label;
            # for multi-label prefixes (e.g. `student.pu.bihar-ums.com`)
            # the rightmost label of the prefix is the institution. We
            # check both the full prefix and the rightmost label so single-
            # and multi-label tenants both work. Bug 31 — also try
            # stripping a known functional prefix (`lms-`, `exam-`, …)
            # from the full prefix so `lms-ccsuniversity` matches an
            # `exact_shortname` of `ccsuniversity`.
            tokens: set[str] = {prefix}
            if "." in prefix:
                tokens.add(prefix.split(".")[-1])
            for fp in SAMARTH_FUNCTIONAL_PREFIXES:
                if prefix.startswith(fp) and len(prefix) > len(fp):
                    tokens.add(prefix[len(fp):])
            # Bug 1 — when the OrgID has no curated `exact_shortnames`,
            # fall back to `auto_shortnames` (the leftmost-label set
            # extracted from configured domains). VNSGU's
            # `vnsgu.samarth.edu.in` has tenant prefix "vnsgu", which
            # matches auto_shortname "vnsgu" derived from `vnsgu.ac.in`.
            # OrgIDs that DO set `exact_shortnames` keep the strict
            # cross-uni disambiguator (Patna `{"pu"}` still rejects
            # `pup.samarth.ac.in`). Source label included in the
            # accept reason so logs show which set authorised the
            # tenant.
            check_set: set[str] = set(exact_lower)
            check_source = "exact_shortnames"
            if not check_set:
                check_set = {
                    (s or "").lower().strip()
                    for s in (auto_shortnames or [])
                    if s
                }
                check_source = "auto_shortnames"
            # Acronym fallback — most Samarth/state-platform tenants are
            # named by the institution ACRONYM (`mgahv` for Mahatma Gandhi
            # Antarrashtriya Hindi Vishwavidyalaya), which is neither a
            # domain label nor an exact_shortname. Fold it in (≥4 chars,
            # non-ambiguous) so acronym-named tenants pass membership
            # without a per-OrgID override. The ≥4 floor + AMBIGUOUS_SHORTNAMES
            # exclusion mirror R6 so short/ambiguous acronyms (du, iit, mit)
            # can't admit unrelated tenants.
            ac = (acronym or "").lower().strip()
            if ac and len(ac) >= 4 and ac not in AMBIGUOUS_SHORTNAMES:
                if not check_set:
                    check_source = "acronym"
                elif ac not in check_set:
                    check_source = check_source + "+acronym"
                check_set = set(check_set) | {ac}
            if not check_set:
                return False, (
                    f"platform {plat} tenant '{prefix}': no exact_shortnames or "
                    f"auto_shortnames available for this OrgID"
                )
            if any(t in check_set for t in tokens):
                return True, (
                    f"platform {plat} tenant '{prefix}' ∈ {check_source} "
                    f"{sorted(check_set)}"
                )
            # Fix 1 — asymmetric substring fallback. When the tenant
            # prefix EXTENDS one of this OrgID's shortnames (e.g.
            # `bujhansiadm.samarth.edu.in` for an OrgID whose auto-
            # shortname is `bujhansi`), accept. Length floor 4 mirrors
            # R6 so that 2-3-char auto-shortnames (`du`, `iit`) can't
            # admit unrelated tenants like `dauniv` / `iitkgp`. Only
            # the `shortname ∈ prefix` direction is checked — the
            # reverse (`prefix ∈ shortname`) would let a short tenant
            # like `pup.samarth.ac.in` match a longer shortname.
            for s in check_set:
                if len(s) >= 4 and s in prefix:
                    return True, (
                        f"platform {plat} tenant '{prefix}' substring-match "
                        f"shortname '{s}' ∈ {check_source} {sorted(check_set)}"
                    )
            return False, (
                f"platform {plat} tenant '{prefix}' ∉ {check_source} "
                f"{sorted(check_set)}"
            )

    # ---- R1 + R2: owned domain equality / subdomain ---------------
    for d in owned:
        if h == d:
            return True, f"owned domain {d}"
        if h.endswith("." + d):
            return True, f"subdomain of owned {d}"

    # ---- R5: portal-anchored sibling host (Bug 22) -----------------
    # The host appeared on the primary homepage as a strict portal-
    # pattern anchor — already a verified university link.
    if portal_anchored_hosts and h in portal_anchored_hosts:
        return True, "portal-anchored sibling on primary homepage"

    # ---- R6: shortname-in-domain (sibling acceptance) --------------
    # Indian universities frequently register secondary domains using a
    # 3-5 char abbreviation of the institutional name (e.g. `ccsu` for
    # Chaudhary Charan Singh University → `ccsuforms.in`; `jnvu` for
    # Jai Narain Vyas University → `jnvuiums.in`). When ANY of the
    # OrgID's shortnames — operator-curated `exact_shortnames` OR
    # auto-derived from configured-domain leftmost labels — appears at
    # the start of the host's eTLD+1 leftmost label OR as a label
    # somewhere in the full host, accept.
    #
    # Length floor of 4 avoids 3-char acronyms (e.g. "pup", "iit")
    # admitting unrelated domains by accident. Operator-curated
    # `exact_shortnames` shorter than 4 still drive the strict R3/R4
    # platform-tenant gates above; only R6 (the broad sibling rule)
    # enforces the length floor.
    #
    # Fix 3 — auto-derived shortnames in `AMBIGUOUS_SHORTNAMES`
    # (mit / iiit / nit / bit / …) are excluded from the R6 set.
    # These are common Indian-uni acronyms shared across many
    # institutions; matching them would admit other-institution
    # domains. Operator-curated `exact_shortnames` are NEVER filtered
    # by AMBIGUOUS_SHORTNAMES — when an operator explicitly lists
    # "mit" they're saying "I want this match for THIS OrgID".
    sibling_shortnames: set[str] = set(exact_lower)
    if auto_shortnames:
        sibling_shortnames.update(
            (s or "").lower().strip()
            for s in auto_shortnames
            if s and (s or "").lower().strip() not in AMBIGUOUS_SHORTNAMES
        )
    # The institution acronym is also a valid sibling-domain abbreviation
    # (e.g. `mgahv` → `mgahv.in`). Same ≥4-char / non-ambiguous guard as
    # the auto-shortnames above; the R6 loop enforces the length floor.
    _ac6 = (acronym or "").lower().strip()
    if _ac6 and _ac6 not in AMBIGUOUS_SHORTNAMES:
        sibling_shortnames.add(_ac6)
    if sibling_shortnames:
        base = _registrable_base(h)
        base_label = base.split(".", 1)[0] if base else ""
        for s in sibling_shortnames:
            if len(s) < 4:
                continue
            if base_label.startswith(s):
                return True, (
                    f"shortname-in-domain: '{s}' prefix of base label "
                    f"'{base_label}' (host '{h}')"
                )
            if ("." + s + ".") in h:
                return True, (
                    f"shortname-in-domain: '.{s}.' substring in host '{h}'"
                )

    # ---- R7: Bug 41 — known shared platform ------------------------
    # `KNOWN_SHARED_PLATFORM_PATTERNS` covers verified multi-tenant
    # platforms (knimbus library, cognibot LMS, samarth, …). State-
    # platforms (bihar-ums) are already filtered by R0; samarth tenants
    # are already gated by R4 (shortname required). Anything left here
    # is a "stable platform identity" — rule-C in
    # `passes_login_signal_gate` will further validate the URL itself.
    if host_is_known_shared_platform(h):
        return True, "known shared platform host"

    return False, "shortname/state mismatch"


# Multi-part TLD suffixes for `_registrable_base`. Mirrors `_MULTIPART_TLDS`
# in tc_finder.py. Anything not listed falls back to last-2-parts.
_REGISTRABLE_MULTIPART_TLDS: frozenset[str] = frozenset({
    "ac.in", "co.in", "edu.in", "gov.in", "org.in", "net.in", "nic.in",
    "ac.uk", "co.uk", "gov.uk", "org.uk",
    "ac.za", "co.za",
})


def _registrable_base(host: str) -> str:
    """Best-effort eTLD+1 (or eTLD+2 for known multi-part suffixes).
    Used by `host_belongs_to_org` rule (6) to extract the leftmost
    registrable label for shortname-prefix matching."""
    parts = host.lower().lstrip(".").split(".")
    if len(parts) <= 2:
        return ".".join(parts)
    last2 = ".".join(parts[-2:])
    if last2 in _REGISTRABLE_MULTIPART_TLDS and len(parts) >= 3:
        return ".".join(parts[-3:])
    return last2


# ============================================================ admission detection
#
# Comprehensive admission-portal detection — replaces the older
# `("admission", "apply")` substring check in `discovery._is_admission_url`.
# See `agent/config.py` for the full rationale; this function is the
# single decision surface for "is this URL an applicant-facing admission
# portal?" used by both the URL-only pre-fetch filter (sibling-walk and
# pre-validation) and the post-fetch content gate (validate_candidate).
#
# The four layers run in order of cost; the first hit wins:
#
#   1. URL substring (host or path) — `html=None` skips layers 2-3 and
#      runs only this + layer 4. Cheap; called pre-fetch.
#   2. Page text content scoring (strong / moderate / counter signals).
#   3. <title> / <h1> phrase match.
#   4. Known admission-platform host blocklist.
#
# Layers 1-3 honour an exception for `/register` and `/registration`
# paths that also carry a counter token like "student" / "login" /
# "signin" / "existing" — those are typically existing-student login
# entry points labeled as a "registration portal".


def _matched_url_admission_token(parsed: Any) -> str:
    """Layer 1 helper. Returns the matching token (path or host) when
    the URL looks like an admission endpoint, or "" otherwise. The
    `/register`-style exception is applied here.
    """
    host = (parsed.netloc or "").lower().split(":")[0]
    path = (parsed.path or "").lower()
    register_tokens_in_path = {"/register", "/registration"}
    has_register = any(t in path for t in register_tokens_in_path)
    has_exempt = any(t in path for t in URL_ADMISSION_REGISTER_EXEMPT_TOKENS)
    for kw in URL_ADMISSION_PATH_KEYWORDS:
        if kw in path:
            # `/register` / `/registration` skipped if path has
            # student/login/signin/existing — likely an enrolled-
            # student entry, not new-applicant signup.
            if kw in register_tokens_in_path and has_exempt:
                continue
            return f"path={kw!r}"
    # `/register` already handled in the loop above. For host tokens we
    # don't apply the same exception — a host literally named
    # "admission" is never an enrolled-student portal regardless of
    # whatever path it serves.
    del has_register
    for kw in URL_ADMISSION_HOST_KEYWORDS:
        if kw in host:
            return f"host={kw!r}"
    return ""


def _matched_known_admission_platform(host: str) -> str:
    """Layer 4 helper. Returns the matching platform domain or ""."""
    if not host:
        return ""
    h = host.lower().lstrip(".").split(":")[0]
    for entry in KNOWN_ADMISSION_PLATFORMS:
        if h == entry or h.endswith("." + entry):
            return entry
    return ""


def _matched_admission_title_phrase(title_h1_text: str) -> str:
    """Layer 3 helper. Returns the matching phrase or "". Honours the
    `login` + `existing` exception (probable enrolled-student login)."""
    if not title_h1_text:
        return ""
    s = title_h1_text.lower()
    for phrase in TITLE_ADMISSION_PHRASES:
        if phrase in s:
            if "login" in s and "existing" in s:
                return ""
            return phrase
    return ""


def url_is_admin_path(url: str) -> bool:
    """Layer-1 admin / backend URL classifier. Returns True iff the
    URL path contains any token from `ADMIN_URL_PATH_TOKENS` (
    `/admin/`, `/wp-admin`, `/administrator`, `/cpanel`, …) —
    catches CMS / Django / WordPress / Joomla admin backends. Run
    pre-fetch in `_pre_validation_filter` so admin URLs never burn
    a validation slot. Substring match on the lowercased path; no
    fuzziness.

    Distinct from `is_admission_portal` (which targets *applicant*
    onboarding) — admin = staff/management backend, admission =
    new-student registration.
    """
    if not url:
        return False
    try:
        path = (urlsplit(url).path or "").lower()
    except Exception:
        return False
    return any(tok in path for tok in ADMIN_URL_PATH_TOKENS)


def is_admission_portal(url: str, html: str | None) -> tuple[bool, str]:
    """Decide whether (url, html) is an applicant-facing admission portal.

    Returns ``(is_admission, reason)``. Callers should log the reason on
    rejection — `discovery._maybe_reject_admission` is the canonical
    log site.

    Pass ``html=None`` for a URL-only check (Layer 1 + Layer 4 only).
    With HTML, Layers 2 and 3 run as well.
    """
    parsed = urlsplit(url or "")
    host = (parsed.netloc or "").lower().split(":")[0]

    # Layer 1 — URL substring match.
    tok = _matched_url_admission_token(parsed)
    if tok:
        return True, f"URL admission token {tok}"

    # Layer 4 — known admission-platform host blocklist.
    plat = _matched_known_admission_platform(host)
    if plat:
        return True, f"known admission platform host {plat!r}"

    # Layer 2 + Layer 3 require HTML.
    if not html:
        return False, ""

    # Moodle login-page bypass. Stock Moodle login pages render UI
    # elements ("Forgotten your username or password?", "Create new
    # account", "Lost password") and use form action paths like
    # `/login/index.php` — the "Create new account" text in
    # particular previously mis-flagged real Moodle student logins
    # as admission portals via Layer 2's strong-signal match. Any
    # marker in the raw HTML overrides admission detection and
    # accepts the page as a student login. Match against raw
    # `html` (not get_text()) so URL fragments in `<form action=…>`
    # attributes participate.
    html_lower = html.lower()
    moodle_marker = next(
        (m for m in MOODLE_LOGIN_COUNTER_SIGNALS if m in html_lower),
        None,
    )
    if moodle_marker is not None:
        return False, ""

    soup = BeautifulSoup(html, "html.parser")

    # Layer 3 — title + H1 phrase match (cheap; no full-body parse).
    title_text = (soup.title.string or "") if soup.title else ""
    h1_texts = [h.get_text(" ", strip=True) for h in soup.find_all("h1")[:5]]
    title_h1 = (title_text + " | " + " | ".join(h1_texts))
    phrase = _matched_admission_title_phrase(title_h1)
    if phrase:
        return True, f"title/h1 admission phrase {phrase!r}"

    # Layer 2 — full visible-text scoring.
    text = soup.get_text(" ", strip=True).lower()
    if not text:
        return False, ""

    matched_strong: list[str] = [s for s in STRONG_ADMISSION_SIGNALS if s in text]
    if matched_strong:
        return True, f"strong admission signal {matched_strong[0]!r}"

    matched_moderate: list[str] = [s for s in MODERATE_ADMISSION_SIGNALS if s in text]
    matched_counter: list[str] = [s for s in STUDENT_LOGIN_COUNTER_SIGNALS if s in text]

    if len(matched_moderate) >= 2 and not matched_counter:
        sample = matched_moderate[:3]
        return True, (
            f"moderate admission signals ×{len(matched_moderate)} "
            f"(no counter-signals): {sample}"
        )

    if len(matched_moderate) >= 4:
        sample = matched_moderate[:3]
        return True, (
            f"high admission signal count ×{len(matched_moderate)} "
            f"(despite ×{len(matched_counter)} counter-signals): {sample}"
        )

    return False, ""


def _ddg_html_search(
    query: str,
    *,
    http_timeout: float,
    user_agent: str,
    max_results: int,
) -> list[str]:
    # DDG queries get a longer timeout than the per-host validation timeout
    # because html.duckduckgo.com is occasionally slow under load.
    timeout = max(http_timeout, DUCKDUCKGO_TIMEOUT_SECONDS)
    resp = HTTP_SESSION.post(
        DDG_HTML_ENDPOINT,
        data={"q": query},
        headers={"User-Agent": user_agent},
        timeout=timeout,
    )
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    urls: list[str] = []
    for anchor in soup.select("a.result__a"):
        href = anchor.get("href") or ""
        real = _unwrap_ddg_link(href)
        if real and real not in urls:
            urls.append(real)
        if len(urls) >= max_results:
            break
    return urls


def _unwrap_ddg_link(href: str) -> str | None:
    if not href:
        return None
    normalised = href if "://" in href else "https:" + href
    parsed = urlsplit(normalised)
    if "duckduckgo.com" in parsed.netloc and parsed.path.startswith("/l/"):
        qs = parse_qs(parsed.query)
        target = qs.get("uddg", [""])[0]
        return target or None
    if parsed.scheme in ("http", "https") and parsed.netloc:
        return normalised
    return None


def gemini_search(
    orgid: str,
    university_name: str,
    primary_domain: str,
    shortname: str,
    *,
    http_timeout: float = 30.0,
) -> list[str]:
    """Primary discovery search — Gemini Pro via OpenRouter.

    Returns the raw URL strings Gemini surfaces. The caller is
    responsible for wrapping these into `Candidate` objects and routing
    them through the standard membership / pre-filter / validation /
    consolidation pipeline (no shortcut — Gemini is treated like any
    other search engine, just with higher recall on smaller Indian
    universities that DDG under-indexes).

    Disabled / no API key / network failure → returns []. The caller
    should fall back to DDG when this happens.

    The prompt deliberately uses the FULL `university_name` (not the
    `shortname`) so that ambiguous shortnames like MIT / NIT / IIIT
    auto-disambiguate from the long form ("MIT ADT University Pune"
    vs "MIT University Shillong"). `shortname` is accepted in the
    signature for future use but is not interpolated into the prompt.
    """
    if not GEMINI_SEARCH_ENABLED or not OPENROUTER_API_KEY:
        logger.debug(
            "[%s] Gemini search disabled or no API key", orgid,
        )
        return []

    prompt = (
        f"Find the student LOGIN portal URLs for {university_name} "
        f"in India (official domain: {primary_domain}). "
        f"I need URLs where enrolled students can actually LOG IN — "
        f"pages with a username/password form. "
        f"Include: student ERP login, LMS/Moodle login, exam portal "
        f"login, fee payment login, library login. "
        f"Do NOT include: homepages, news pages, PDF documents, "
        f"admission/application forms, staff/admin login, or any page "
        f"without a login form. Do NOT include URLs from other "
        f"universities. "
        f"Return ONLY a JSON array of login page URLs, nothing else, "
        f"no explanation, no markdown. "
        f'Example: ["https://erp.xyz.ac.in/login", '
        f'"https://lms.xyz.ac.in/student"]'
    )

    try:
        response = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/reclaimprotocol",
            },
            json={
                "model": OPENROUTER_MODEL,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=http_timeout,
        )
        data = response.json()
    except Exception as err:
        logger.warning("[%s] Gemini search failed: %s", orgid, err)
        return []

    if isinstance(data, dict) and "error" in data:
        logger.warning("[%s] OpenRouter error: %s", orgid, data["error"])
        return []
    try:
        text = data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError, AttributeError):
        logger.warning(
            "[%s] Gemini response missing choices/content: %r",
            orgid, data,
        )
        return []

    logger.info("[%s] Gemini raw response: %s", orgid, text[:300])

    # Strip markdown fences (```json ... ```) and pull out the JSON
    # array. Gemini occasionally wraps the array in prose despite the
    # "no explanation" instruction; the regex handles that.
    text = re.sub(r"```json|```", "", text).strip()
    match = re.search(r"\[.*?\]", text, re.DOTALL)
    if not match:
        logger.warning("[%s] Gemini response had no JSON array", orgid)
        return []
    try:
        urls = json.loads(match.group())
    except json.JSONDecodeError as err:
        logger.warning(
            "[%s] Gemini JSON parse failed: %s (raw=%r)",
            orgid, err, match.group()[:200],
        )
        return []
    if not isinstance(urls, list):
        return []
    valid = [
        u for u in urls
        if isinstance(u, str)
        and u.startswith(("http://", "https://"))
        and len(u) < 500
    ]
    logger.info("[%s] Gemini search: %d valid URLs", orgid, len(valid))
    return valid


def gemini_search_broad(
    orgid: str,
    university_name: str,
    primary_domain: str,
    *,
    http_timeout: float = 30.0,
) -> list[str]:
    """Cascade retry-2 — Gemini search with a broader prompt.

    Used only by `discovery._retry_gemini_broad` when the main
    pipeline returned 0 portals. Asks for the *student-facing
    system homepage* rather than a login URL, which recovers
    universities whose actual login surface is one click in from a
    portal homepage that DDG / sibling-walk / probes missed.

    Same OpenRouter call shape as `gemini_search`; only the prompt
    differs. Disabled / no API key / network failure → returns [].
    """
    if not GEMINI_SEARCH_ENABLED or not OPENROUTER_API_KEY:
        return []

    prompt = (
        f"What is the official student portal or ERP system "
        f"website for {university_name} in India "
        f"(domain: {primary_domain})? "
        f"I just need the main URL of whatever system students "
        f"use to access their academic information — grades, "
        f"attendance, fees, exam results. "
        f"Return ONLY a JSON array of URLs, nothing else, no "
        f"explanation, no markdown. "
        f'Example: ["https://erp.xyz.ac.in", '
        f'"https://student.xyz.ac.in"]'
    )

    try:
        response = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/reclaimprotocol",
            },
            json={
                "model": OPENROUTER_MODEL,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=http_timeout,
        )
        data = response.json()
    except Exception as err:
        logger.warning(
            "[%s] Gemini broad search failed: %s", orgid, err,
        )
        return []

    if isinstance(data, dict) and "error" in data:
        logger.warning(
            "[%s] OpenRouter error (broad): %s", orgid, data["error"],
        )
        return []
    try:
        text = data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError, AttributeError):
        logger.warning(
            "[%s] Gemini broad response missing choices/content: %r",
            orgid, data,
        )
        return []

    text = re.sub(r"```json|```", "", text).strip()
    match = re.search(r"\[.*?\]", text, re.DOTALL)
    if not match:
        logger.warning(
            "[%s] Gemini broad response had no JSON array", orgid,
        )
        return []
    try:
        urls = json.loads(match.group())
    except json.JSONDecodeError as err:
        logger.warning(
            "[%s] Gemini broad JSON parse failed: %s (raw=%r)",
            orgid, err, match.group()[:200],
        )
        return []
    if not isinstance(urls, list):
        return []
    valid = [
        u for u in urls
        if isinstance(u, str)
        and u.startswith(("http://", "https://"))
        and len(u) < 500
    ]
    logger.info(
        "[%s] Gemini broad search: %d valid URLs", orgid, len(valid),
    )
    return valid


def gemini_subdomain_search(
    orgid: str,
    university_name: str,
    primary_domain: str,
    *,
    http_timeout: float = 30.0,
) -> list[str]:
    """Phase 3 Gemini expansion — ask OpenRouter Gemini for the
    subdomains and related platform domains the university uses for
    student-facing services. Returns clean hostnames (NOT full URLs).

    Triggered by the orchestrator when the homepage-anchor sibling
    walk yielded few hosts (or when the search phase produced 0
    candidates). Higher recall than the anchor walk for universities
    whose student services live on a *separate* platform domain
    (e.g. Patliputra: ppup.ac.in homepage doesn't link to
    ppuponline.in, but Gemini knows they belong to the same
    institution).

    The prompt asks for HOSTNAMES, not URLs — different signal from
    `gemini_search` which requests full login URLs. Both can fire
    in the same run.

    Disabled / no API key / network failure → returns []. Caller
    must treat the return as advisory.
    """
    if not GEMINI_SEARCH_ENABLED or not OPENROUTER_API_KEY:
        return []

    prompt = (
        f"What hostnames does {university_name} in India "
        f"(official domain: {primary_domain}) use for enrolled-"
        f"student services like ERP, LMS, exam portal, fee "
        f"payment, library? "
        f"Include both subdomains of {primary_domain} AND any "
        f"separate platform domains the university uses (custom "
        f"ERP domains, third-party platforms like knimbus.com / "
        f"samarth.ac.in / edumarshal.com). "
        f"Do NOT include admission portals, government portals, "
        f"news/marketing subdomains, or hostnames belonging to "
        f"other universities. "
        f"Return ONLY a JSON array of hostnames (no https://, no "
        f"paths), nothing else, no explanation, no markdown. "
        f'Example: ["erp.xyz.ac.in", "lms.xyz.ac.in", '
        f'"xyzuniv.knimbus.com"]'
    )

    try:
        response = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/reclaimprotocol",
            },
            json={
                "model": OPENROUTER_MODEL,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=http_timeout,
        )
        data = response.json()
    except Exception as err:
        logger.warning(
            "[%s] Gemini subdomain search failed: %s", orgid, err,
        )
        return []

    if isinstance(data, dict) and "error" in data:
        logger.warning(
            "[%s] OpenRouter subdomain error: %s",
            orgid, data["error"],
        )
        return []
    try:
        text = data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError, AttributeError):
        logger.warning(
            "[%s] Gemini subdomain response missing choices/content: %r",
            orgid, data,
        )
        return []

    logger.info("[%s] Gemini subdomain raw: %s", orgid, text[:200])

    text = re.sub(r"```json|```", "", text).strip()
    match = re.search(r"\[.*?\]", text, re.DOTALL)
    if not match:
        logger.warning(
            "[%s] Gemini subdomain response had no JSON array",
            orgid,
        )
        return []
    try:
        hosts = json.loads(match.group())
    except json.JSONDecodeError as err:
        logger.warning(
            "[%s] Gemini subdomain JSON parse failed: %s (raw=%r)",
            orgid, err, match.group()[:200],
        )
        return []
    if not isinstance(hosts, list):
        return []

    valid: list[str] = []
    for h in hosts:
        if not isinstance(h, str):
            continue
        cleaned = (
            h.strip()
            .lower()
            .replace("https://", "")
            .replace("http://", "")
            .rstrip("/")
        )
        # Defensive: strip path/query/fragment in case Gemini
        # returned full URLs despite the "hostnames only" prompt.
        for sep in ("/", "?", "#"):
            if sep in cleaned:
                cleaned = cleaned.split(sep, 1)[0]
        if not (3 < len(cleaned) < 200):
            continue
        if "." not in cleaned:
            continue
        if cleaned in valid:
            continue
        valid.append(cleaned)

    logger.info(
        "[%s] Gemini subdomain search: %d hosts found: %s",
        orgid, len(valid), valid,
    )
    return valid


def _google_search(query: str, *, max_results: int) -> list[str]:
    """Best-effort Google search fallback. Lazy-imports
    `googlesearch-python` so the agent runs without the dependency
    installed; callers must treat any return value as advisory and
    swallow exceptions.

    Used only when DDG (primary + broader fallback) returns zero
    results, which happens for many smaller Indian universities whose
    portals Google indexes but DDG does not (e.g. St. Xavier's Ranchi,
    `www.sxcran.org/Student/login`).
    """
    try:
        from googlesearch import search as _gsearch  # type: ignore
    except Exception as err:
        logger.info(
            "Google search fallback skipped (googlesearch-python not "
            "available): %s",
            err,
        )
        return []
    urls: list[str] = []
    try:
        for url in _gsearch(query, num_results=max_results, lang="en"):
            if isinstance(url, str) and url.startswith(("http://", "https://")):
                if url not in urls:
                    urls.append(url)
            if len(urls) >= max_results:
                break
    except Exception as err:
        logger.warning("Google search failed for %r: %s", query, err)
        return []
    return urls


# =========================================================== path probes

_PROBE_MAX_WORKERS: int = 30


def _parallel_probe(
    items: list[tuple[str, str, str, str]],
    *,
    http_timeout: float,
    user_agent: str,
    max_workers: int = _PROBE_MAX_WORKERS,
) -> list[Candidate]:
    """Probe a list of `(url, category, source, reasoning)` in parallel.

    Items whose URL is HEAD-reachable become Candidates (status 200-399).
    """
    if not items:
        return []
    out: list[Candidate] = []

    def _check(item: tuple[str, str, str, str]) -> Candidate | None:
        url, category, source, reasoning = item
        if _probe_reachable(url, http_timeout=http_timeout, user_agent=user_agent):
            return Candidate(
                url=url,
                category=category,
                discovery_source=source,
                discovery_reasoning=reasoning,
            )
        return None

    with ThreadPoolExecutor(max_workers=min(max_workers, len(items))) as exe:
        futures = [exe.submit(_check, it) for it in items]
        for f in as_completed(futures):
            r = f.result()
            if r is not None:
                out.append(r)
    return out


def run_path_probes(
    domains: list[str],
    *,
    http_timeout: float,
    user_agent: str,
) -> list[Candidate]:
    """Probe `PATH_PROBES` against every domain in `domains` in parallel.

    Most OrgIDs pass a single domain. Multi-domain rows (or OrgIDs with an
    `extra_effective_domains` override) get path probes fanned out across
    each domain — necessary when a university's student portal lives on a
    secondary owned domain (e.g. HPU Shimla → primary `hpuniv.ac.in`,
    extra `hpushimla.in`).
    """
    items: list[tuple[str, str, str, str]] = []
    seen: set[str] = set()
    for domain in domains:
        if not domain or domain in seen:
            continue
        seen.add(domain)
        for template, category in PATH_PROBES:
            url = template.format(domain=domain)
            items.append((url, category, "rule", f"path probe: {template}"))
    return _parallel_probe(items, http_timeout=http_timeout, user_agent=user_agent)


def run_subdomain_probes(
    primary_domain: str,
    *,
    http_timeout: float,
    user_agent: str,
    extra_domains: list[str] | None = None,
) -> list[Candidate]:
    """Bug 8 — probe the configured `SUBDOMAIN_PROBE_LIST` against every
    configured university domain (primary plus any extras) in parallel.

    Probing extras is cheap (HEAD checks parallelised across the same
    session pool) and necessary for SheerID rows that list a sub-domain
    as the primary (e.g. SPPU → primary `pun.unipune.ac.in`, root
    `unipune.ac.in`). Without extras, the rooted subdomains
    (`hallticket.unipune.ac.in`, `lib.unipune.ac.in`, …) never get probed.
    """
    targets: list[str] = []
    if primary_domain:
        targets.append(primary_domain)
    for d in extra_domains or []:
        if d and d != primary_domain and d not in targets:
            targets.append(d)
    if not targets:
        return []
    items: list[tuple[str, str, str, str]] = []
    for domain in targets:
        for sub in SUBDOMAIN_PROBE_LIST:
            url = f"https://{sub}.{domain}/"
            items.append(
                (url, "Student Portal", "subdomain-probe", f"subdomain probe: {sub}.{domain}")
            )
        # ASP.NET ERP app-path probes: the login lives at a path that
        # mirrors the subdomain label (`iums.{domain}/iums/Login.aspx`),
        # not at the subdomain root, so probe those explicitly.
        for sub in ERP_APP_LOGIN_SUBDOMAINS:
            for tmpl in ERP_APP_LOGIN_PATH_TEMPLATES:
                url = f"https://{sub}.{domain}{tmpl.format(label=sub)}"
                items.append(
                    (url, "Student Portal", "subdomain-probe",
                     f"ERP app-path probe: {sub}.{domain}{tmpl.format(label=sub)}")
                )
    return _parallel_probe(items, http_timeout=http_timeout, user_agent=user_agent)


def run_same_host_student_probes(
    seed_hosts: set[str],
    *,
    http_timeout: float,
    user_agent: str,
) -> list[Candidate]:
    """Bug 7 — for every host that already has a candidate, probe the
    `STUDENT_LOGIN_SAME_HOST_PROBES` paths in parallel."""
    items: list[tuple[str, str, str, str]] = []
    for host in sorted(seed_hosts):
        if not host:
            continue
        for path in STUDENT_LOGIN_SAME_HOST_PROBES:
            url = f"https://{host}{path}"
            items.append(
                (url, "Student Portal", "same-host-student-probe", f"same-host probe: {host}{path}")
            )
    return _parallel_probe(items, http_timeout=http_timeout, user_agent=user_agent)


def _probe_reachable(url: str, *, http_timeout: float, user_agent: str) -> bool:
    """HEAD-then-tiny-GET reachability check.

    The HEAD-then-tiny-GET fallback covers the large class of Indian gov
    sites that 405 / 403 HEAD requests but happily serve GET. Both calls
    use the shared `HTTP_SESSION` so connections are kept alive across the
    parallel probe fanout.
    """
    headers = {"User-Agent": user_agent}
    try:
        resp = HTTP_SESSION.head(url, headers=headers, timeout=http_timeout, allow_redirects=True)
        if resp.status_code >= 400:
            resp = HTTP_SESSION.get(
                url,
                headers={**headers, "Range": "bytes=0-1024"},
                timeout=http_timeout,
                allow_redirects=True,
                stream=True,
            )
            resp.close()
        return 200 <= resp.status_code < 400
    except requests.RequestException as err:
        logger.debug("path probe %s failed: %s", url, err)
        return False


# ============================================== Bug 22 sibling-domain extraction

# Strict portal-anchor patterns. Anchor visible text (after stripping
# whitespace + trailing punctuation, lower-cased) must FULL-match one of
# these. Substring matches like "click here to login" are deliberately
# excluded — those are button labels in nav menus, not portal-link
# indicators.
_SIBLING_PORTAL_ANCHOR_PATTERNS: frozenset[str] = frozenset({
    "student portal", "students portal",
    "student login", "students login",
    "examination portal", "exam portal", "exams portal",
    "online exam portal", "online examination portal",
    "admit card", "hall ticket",
    "result portal", "results portal",
    "lms", "lms portal", "learning portal", "moodle", "moodle portal",
    "online learning",
    "fee portal", "fee payment portal", "online fee",
    "library portal", "elibrary",
    "transcript portal", "certificate portal",
})

# `portal` alone is allowed only when the destination href path contains
# `/login` or `/portal` — covers homepages whose hero CTA is just "Portal".
_SIBLING_PORTAL_ANCHOR_PATTERN_PORTAL: str = "portal"

# Anti-keywords that disqualify an otherwise-matching anchor when its
# parent context (li/div/p/nav/section ancestor) contains them. Catches
# e.g. an "Examination Portal" anchor that lives inside an
# "Alumni Resources" sidebar — not the right portal.
_SIBLING_PORTAL_ANTI_KEYWORDS: tuple[str, ...] = (
    "alumni", "faculty only", "staff only", "recruitment",
    "vendor", "tender", "rti", "employee", "admin",
)

# Trailing characters stripped from anchor text before pattern matching.
_ANCHOR_TEXT_TRAILING_PUNCT: str = ".,;:!?-_>»→•·"


def _normalize_anchor_text(text: str) -> str:
    if not text:
        return ""
    t = text.strip().lower()
    while t and t[-1] in _ANCHOR_TEXT_TRAILING_PUNCT:
        t = t[:-1]
    return t.strip()


def _anchor_text_is_strict_portal(text: str, *, href_path: str) -> bool:
    norm = _normalize_anchor_text(text)
    if not norm:
        return False
    if norm in _SIBLING_PORTAL_ANCHOR_PATTERNS:
        return True
    if norm == _SIBLING_PORTAL_ANCHOR_PATTERN_PORTAL:
        path_l = href_path.lower()
        if "/login" in path_l or "/portal" in path_l:
            return True
    return False


def _parent_context_has_anti_keyword(anchor) -> str | None:
    """Walk up to the nearest list/section ancestor and check its full
    text for any anti-keyword. Returns the matching keyword or None."""
    parent = anchor.find_parent(["li", "div", "p", "nav", "section", "aside"])
    if parent is None:
        return None
    parent_text = parent.get_text(" ", strip=True).lower()
    if not parent_text:
        return None
    for kw in _SIBLING_PORTAL_ANTI_KEYWORDS:
        if kw in parent_text:
            return kw
    return None


@dataclass(frozen=True)
class SiblingDomainResult:
    # URLs whose anchor text strictly matched a portal pattern. These get
    # added to `rule_candidates` directly with a custom discovery_source.
    portal_anchors: tuple[tuple[str, str], ...]
    # Hosts that survive the blocklist + are different from primary,
    # regardless of anchor-text match. Used to (a) probe SUBDOMAIN_PROBE_LIST
    # and (b) expand `effective_domains` so URLs on these hosts pass the
    # off-domain validation filter.
    sibling_hosts: frozenset[str]


def extract_sibling_domains_from_homepage(
    html: str,
    *,
    base_url: str,
    primary_host: str,
) -> SiblingDomainResult:
    """Bug 22 — walk every `<a>` in the primary domain's homepage HTML and
    classify outbound links.

    For each anchor whose href resolves to an external host (different from
    `primary_host`, not www.{primary}, not in `EXTERNAL_DOMAIN_BLOCKLIST`):

      * If anchor text strictly matches `_SIBLING_PORTAL_ANCHOR_PATTERNS`
        AND no anti-keyword is in the parent context → record as a
        `portal_anchor` (URL becomes a direct candidate downstream).
      * In all cases, record the host in `sibling_hosts` — even if the
        anchor text doesn't match a portal pattern, the host is a real
        external link from the university and worth probing for
        sub-portals (Bug 23) and trusting through off-domain validation.

    The broader `sibling_hosts` set is needed for universities like NSOU
    whose homepage links to a hub site (`www.nsouict.ac.in`, anchor text
    "ICT Services") rather than directly to a labelled "Student Portal".
    """
    if not html:
        return SiblingDomainResult(portal_anchors=tuple(), sibling_hosts=frozenset())

    primary_host_norm = primary_host.lower().lstrip(".")
    primary_www = "www." + primary_host_norm

    soup = BeautifulSoup(html, "html.parser")
    seen_anchor_keys: set[tuple[str, str]] = set()
    portal_anchors: list[tuple[str, str]] = []
    sibling_hosts: set[str] = set()

    from urllib.parse import urljoin

    for anchor in soup.find_all("a", href=True):
        href = (anchor.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        text = anchor.get_text(" ", strip=True)
        try:
            abs_url = urljoin(base_url, href)
        except Exception:
            continue
        p = urlsplit(abs_url)
        if p.scheme not in ("http", "https"):
            continue
        host = p.netloc.lower().split(":")[0]
        if not host:
            continue
        if host == primary_host_norm or host == primary_www:
            continue
        if host_in_external_blocklist(host):
            continue
        # Subdomains of the primary host aren't "siblings" — they're owned.
        if host.endswith("." + primary_host_norm):
            continue

        sibling_hosts.add(host)

        if not text:
            continue
        if _anchor_text_is_strict_portal(text, href_path=p.path or ""):
            anti = _parent_context_has_anti_keyword(anchor)
            if anti is not None:
                continue
            key = (abs_url, _normalize_anchor_text(text))
            if key in seen_anchor_keys:
                continue
            seen_anchor_keys.add(key)
            portal_anchors.append((abs_url, text))

    return SiblingDomainResult(
        portal_anchors=tuple(portal_anchors),
        sibling_hosts=frozenset(sibling_hosts),
    )


# Multi-label TLDs we treat as effective TLDs when computing a host's
# registrable root. Anything else falls back to "last two labels".
_EFFECTIVE_TLD_SUFFIXES: tuple[str, ...] = (
    ".ac.in", ".co.in", ".edu.in", ".gov.in", ".net.in", ".org.in",
    ".ac.uk", ".co.uk", ".gov.uk",
    ".com.au", ".edu.au", ".gov.au",
)


def registrable_root(host: str) -> str:
    """Return the registrable root domain of `host`. Examples:
        lms.nsouict.ac.in       → nsouict.ac.in
        www.nsouict.ac.in       → nsouict.ac.in
        nsoucebdp.com           → nsoucebdp.com
        sub.example.com         → example.com
    Falls back to `host` if it has fewer than 2 labels.
    """
    h = (host or "").lower().lstrip(".")
    if not h:
        return h
    for suf in _EFFECTIVE_TLD_SUFFIXES:
        if h == suf.lstrip("."):
            return h
        if h.endswith(suf):
            head = h[: -len(suf)]
            labels = head.split(".")
            if labels and labels[-1]:
                return labels[-1] + suf
            return h
    labels = h.split(".")
    if len(labels) >= 2:
        return ".".join(labels[-2:])
    return h


def fetch_homepage_for_sibling_walk(
    primary_domain: str,
    *,
    http_timeout: float,
    user_agent: str,
) -> tuple[str, str] | None:
    """Fetch `https://{primary_domain}/` and return (final_url, body_text),
    or None if the fetch failed. Used as the input to
    `extract_sibling_domains_from_homepage`. We tolerate redirects (the
    final URL is what we feed to anchor-resolving urljoin).
    """
    url = f"https://{primary_domain}/"
    try:
        resp = HTTP_SESSION.get(
            url,
            headers={"User-Agent": user_agent},
            timeout=http_timeout,
            allow_redirects=True,
        )
    except requests.RequestException as err:
        logger.warning(
            "sibling-domain walk: failed to fetch %s — %s: %s",
            url, type(err).__name__, err,
        )
        return None
    if resp.status_code >= 400:
        logger.warning(
            "sibling-domain walk: %s returned http %d", url, resp.status_code,
        )
        return None
    return resp.url, resp.text or ""


# =========================================================== consolidation

def _label_is_college_specific(label: str) -> bool:
    lower = label.lower()
    if any(word in lower for word in COLLEGE_SPECIFIC_WORDS):
        return True
    if 3 <= len(lower) <= 6 and any(ch.isalpha() for ch in lower):
        return True
    return False


def is_homepage_url(url: str) -> bool:
    """A homepage is a URL whose path is empty or just '/'. Used to decide
    whether to invoke link-follow on a validated candidate."""
    p = urlsplit(url or "")
    path = (p.path or "").rstrip("/")
    return path == ""


# Body-content regexes used by `has_strict_login_form` (Bug 19) and shared
# with `discovery._validate_one`. Kept here so the gate logic can be tested
# end-to-end without importing from the orchestrator module.
PASSWORD_INPUT_RE = re.compile(
    r"""(?:
        <input\b[^>]*\btype\s*=\s*["']?password["']?
      | <input\b[^>]*\b(?:name|id)\s*=\s*["']password["']
      | \bformcontrolname\s*=\s*["']password["']
    )""",
    re.IGNORECASE | re.VERBOSE,
)

_USERNAME_OR_EMAIL_INPUT_RE = re.compile(
    r"""<input\b[^>]*\btype\s*=\s*["']?(?:text|email)["']?""",
    re.IGNORECASE,
)

_FORM_TAG_RE = re.compile(r"<form\b", re.IGNORECASE)

# Visible anchor texts that count as a strict "Login" anchor.
# Full-match (case-insensitive, surrounding whitespace stripped). "Login"
# alone IS allowed because the strict path-token check below independently
# requires the destination to be /login-shaped — together those filter out
# the "click somewhere that says login" false positives that motivated the
# tightening (Bug 19).
_STRICT_LOGIN_ANCHOR_TEXTS: frozenset[str] = frozenset({
    "login", "log in", "log-in",
    "sign in", "sign-in", "signin",
    "student login", "user login", "member login",
    # Fix 1 — landing-page CTA wording. Some universities (JNVU's
    # `jnvuiums.in`, several Samarth tenants) hide the login behind a
    # generic "View Detail" / "Click here" / "Access Portal" button on
    # a card, not a literal "Login" anchor. The destination still has
    # to pass the path-token check (`/login`, `login.aspx`, …) below,
    # so the broader text set can't admit non-login link-follows.
    "view detail", "view details",
    "click here",
    "go to portal", "go to login", "go to login portal",
    "access portal", "access login",
    "open portal", "open login",
    "proceed to login", "continue to login",
})

# Path tokens the destination URL must contain (case-insensitive substring).
# Fix 1 — added `loginpage` (matches `/Loginpage.aspx`, `/loginpage_col.aspx`)
# and `/account/login` / `/portal/login` patterns observed in the JNVU
# and CCSU corpora.
_STRICT_LOGIN_PATH_TOKENS: tuple[str, ...] = (
    "/login", "login.aspx", "loginpage", "/signin",
    "/account/login", "/portal/login",
)

# Bug 20 — non-student / student audience keywords matched against the
# page <title> + first few <h1>/<h2>/<h3> tags. Substring match,
# case-insensitive. Order doesn't matter; first hit decides.
NON_STUDENT_AUDIENCE_KEYWORDS_IN_TITLE: tuple[str, ...] = (
    "staff login", "staff portal", "staff sign",
    "employee login", "employee portal", "employee sign",
    "faculty login", "faculty portal", "faculty sign",
    "teacher login", "teacher portal",
    "admin login", "admin portal", "admin sign", "administrator",
    "principal login",
    "vendor login", "vendor portal",
    "recruitment", "recruiter login",
    "hr login", "human resources login",
    "alumni login", "alumni portal",
    # Grievance / complaint redressal portals — complaint-filing systems,
    # not enrolled-student academic-data logins.
    "grievance", "grievance portal", "grievance redressal",
    "complaint portal", "online complaint",
    "back office", "backoffice",
    "internal portal", "intranet",
    # Fix 3 — exam/staff role pages observed on JNVU's IUMS
    # (`erp.jnvuiums.in/Dispatch/ExaminerLoginPage.aspx`,
    # `LoginAff.aspx`, `ExamFormLogin_Practical.aspx`). These are
    # staff/admin pages, never enrolled-student logins.
    "examiner login", "examiner portal",
    "practical examiner",
    "affiliation",
    "college affiliation",
    "dispatch",
    "college portal login",
    # Admin / staff "panel" / "dashboard" variants. Triggered the
    # GLC Mumbai miss where the page title was
    # "WELCOME TO GLC ADMIN PANEL" — neither "admin login" nor
    # "admin portal" matched (substring match, no fuzziness), so
    # rule-A accepted. "panel" / "dashboard" / "control panel" /
    # "management panel" cover the common phrasings administration
    # pages use on Indian-uni / .NET-built sites. Substring match
    # — case insensitive — so "Admin Panel" / "ADMIN PANEL" /
    # "Welcome to Admin Login" all hit.
    "admin panel",
    "admin dashboard",
    "administration panel",
    "administration login",
    "administration portal",
    "control panel",
    "management panel",
    "management login",
    "staff panel",
    "staff dashboard",
    "faculty panel",
    "faculty dashboard",
    "welcome to admin",
    "welcome to the admin",
    "dashboard - admin",
    "dashboard – admin",
)

STUDENT_AUDIENCE_KEYWORDS: tuple[str, ...] = (
    "student login", "student portal", "students login",
    "student sign", "learner login", "candidate login",
)


def has_strict_login_form(html: str, *, final_url: str) -> tuple[bool, str]:
    """Bug 19 — strict rule A. Body must look like a real, same-host login
    form, not just contain a password input by accident.

    Requires:
      * `<input type="password">` (or `name=password` / Angular
        `formcontrolname="password"` equivalent) — `PASSWORD_INPUT_RE`
      * a `<form>` tag
      * a username/email input (`<input type="text">` or `type="email">`)
      * the first `<form action=...>` resolves to the same host as
        `final_url` (relative actions like `""` / `"./"` / `"/path"`
        implicitly satisfy this — `urljoin` keeps them on-host)

    Returns (ok, reason).
    """
    if not html:
        return False, "no body"
    if not PASSWORD_INPUT_RE.search(html):
        return False, "no password input"
    if not _FORM_TAG_RE.search(html):
        return False, "no <form> tag"
    if not _USERNAME_OR_EMAIL_INPUT_RE.search(html):
        return False, "no username/email input"

    soup = BeautifulSoup(html, "html.parser")
    final_host = urlsplit(final_url).netloc.lower().split(":")[0]

    # First <form> with a password input wins. (Some pages have a search
    # form before the login form; keying off the password-bearing form
    # avoids false off-host rejections from a search/newsletter form.)
    target_form = None
    for f in soup.find_all("form"):
        if f.find(
            "input",
            attrs={"type": re.compile(r"^password$", re.IGNORECASE)},
        ):
            target_form = f
            break
    if target_form is None:
        target_form = soup.find("form")

    if target_form is not None:
        action = (target_form.get("action") or "").strip()
        if action:
            try:
                action_url = urljoin(final_url, action)
            except Exception:
                return False, f"un-resolvable form action: {action!r}"
            action_host = urlsplit(action_url).netloc.lower().split(":")[0]
            if action_host and final_host and action_host != final_host:
                return False, f"form action off-host: {action_host}"
    return True, "strict-login-form"


def find_strict_login_anchor_target(
    html: str, *, base_url: str,
) -> tuple[str, str] | None:
    """Bug 19 / Fix 1 — strict rule B candidate. Find the first `<a>` whose
    href resolves to a URL with a ``_STRICT_LOGIN_PATH_TOKENS`` path
    token AND either:

      * the visible text full-matches one of ``_STRICT_LOGIN_ANCHOR_TEXTS``
        (the original Bug 19 strict rule), OR
      * the href path itself contains a ``_STRICT_LOGIN_PATH_TOKENS``
        token (Fix 1) — anchor text becomes irrelevant when the
        destination URL is unambiguously login-shaped. This is what
        catches "View Detail" / "Click here" / icon-only buttons on
        cards that link to ``/Loginpage.aspx``.

    Returns (absolute_url, anchor_text) for the first hit, or None.
    The caller must still fetch the destination and run rule A on it
    before accepting; this function only reports that the page points
    at something login-shaped.
    """
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")
    for anchor in soup.find_all("a", href=True):
        href = (anchor.get("href") or "").strip()
        if not href:
            continue
        text = anchor.get_text(strip=True) or ""
        try:
            abs_url = urljoin(base_url, href)
        except Exception:
            continue
        path = (urlsplit(abs_url).path or "").lower()
        path_is_login_shaped = any(tok in path for tok in _STRICT_LOGIN_PATH_TOKENS)
        if not path_is_login_shaped:
            continue
        # Path is login-shaped. Accept the anchor if either:
        #   (a) the visible text matches a strict login phrase, or
        #   (b) the path itself is unambiguously login-shaped — admit
        #       any anchor pointing at it regardless of text.
        text_lower = text.lower()
        text_matches = text_lower in _STRICT_LOGIN_ANCHOR_TEXTS
        if not text_matches:
            # Fall through (b): path is login-shaped enough on its own.
            # Use a synthetic text label when the anchor was empty
            # (icon-only buttons) so the caller has something to log.
            return abs_url, text or "<no-text>"
        return abs_url, text
    return None


# Audience tokens matched against ASP.NET WebForms / Razor server-control
# names baked into form/input `name` / `id` attributes. These leak the
# audience even when the visible page chrome is generic ("LOGIN PANEL")
# — Patna University's `pup.ac.in/Login.aspx` has an
# `<input name="AdminLogin$UserName">` despite a title of just
# `Login::Patna University::`. Tokens are matched per-label (split on
# `_`, `$`, `-`, camelCase boundaries) so `AdminLogin` matches `admin`
# but `Administration` doesn't accidentally match `admin` either —
# only as a complete word/label segment.
_NON_STUDENT_FORM_NAME_TOKENS: frozenset[str] = frozenset({
    "admin", "administrator", "administration",
    "staff", "employee", "faculty", "teacher", "principal",
    "vendor", "recruiter", "recruitment",
    "hr", "humanresources",
    "alumni",
})

_STUDENT_FORM_NAME_TOKENS: frozenset[str] = frozenset({
    "student", "students", "learner", "candidate",
})

# Split on word boundaries (underscore, dollar, dash) and camelCase.
_LABEL_SPLIT_RE = re.compile(r"[_$\-]|(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def _label_tokens(s: str) -> set[str]:
    if not s:
        return set()
    return {tok.lower() for tok in _LABEL_SPLIT_RE.split(s) if tok}


def classify_login_audience(html: str) -> str:
    """Bug 20 — inspect a login page's `<title>` + first few `<h1>`/`<h2>`/
    `<h3>` tags AND the `name`/`id` attributes of `<form>` and `<input>`
    tags for audience signals.

    Returns one of:
      * `"non_student"` — title or heading contains a non-student keyword
        (`staff login` / `admin portal` / `faculty` / `employee` / `vendor`
        / `recruitment` / `alumni` / `intranet` / etc.), OR a form/input
        `name`/`id` label tokenises to one of `_NON_STUDENT_FORM_NAME_TOKENS`
        (e.g. `AdminLogin$UserName` → `admin` token) → caller should
        REJECT.
      * `"student"` — explicit student-positive phrase (`student login`,
        `learner login`, `candidate login`, …) in title/h1, OR form/input
        name token in `_STUDENT_FORM_NAME_TOKENS` (e.g.
        `StudentLogin$UserName`) → caller may KEEP.
      * `"ambiguous"` — neither set matched. Caller falls back to URL-path
        heuristics; cap confidence and accept tentatively.
    """
    if not html:
        return "ambiguous"
    soup = BeautifulSoup(html, "html.parser")

    parts: list[str] = []
    title = soup.title.string if (soup.title and soup.title.string) else ""
    if title:
        parts.append(title)
    for tag_name in ("h1", "h2", "h3"):
        for h in soup.find_all(tag_name)[:3]:
            t = h.get_text(strip=True)
            if t:
                parts.append(t)
    combined = " ".join(parts).lower()

    if combined:
        for kw in NON_STUDENT_AUDIENCE_KEYWORDS_IN_TITLE:
            if kw in combined:
                return "non_student"

    # Server-control name signal: tokenise every <form>/<input> name+id
    # attribute and look for non-student / student tokens. This catches
    # ASP.NET WebForms pages whose visible chrome is generic but whose
    # control names declare the audience (`AdminLogin$Password`, etc.).
    name_tokens: set[str] = set()
    for tag in soup.find_all(["form", "input"]):
        for attr in ("name", "id"):
            val = tag.get(attr)
            if val:
                name_tokens.update(_label_tokens(str(val)))
    if name_tokens & _NON_STUDENT_FORM_NAME_TOKENS:
        return "non_student"

    if combined:
        for kw in STUDENT_AUDIENCE_KEYWORDS:
            if kw in combined:
                return "student"
    if name_tokens & _STUDENT_FORM_NAME_TOKENS:
        return "student"
    return "ambiguous"


# ============================================================ login-form audience
#
# Bug 40 — `classify_login_audience` (above) inspects the page chrome
# (title / h1-h3 / control-name tokens) for staff/admin/employee
# signals. That catches "Staff Login" / `AdminLogin$Password` style
# pages but misses exam-form / fee-challan / admission-application
# pages whose chrome is generic ("Login - University X") and whose
# only tell is the *form fields themselves*.
#
# `classify_login_form_audience` reads the form. The decision rules:
#
#   1. Find the *primary identifier field* — first <input> whose type
#      is not password/hidden/submit/button/reset/checkbox/radio/file/
#      image. Collect its placeholder, aria-label, name, id, title;
#      its `<label for="id">` text; and its parent's text (truncated
#      to 100 chars, the user-spec window).
#   2. If that primary-field text matches any
#      EXPLICIT_NON_STUDENT_FIELD_SIGNAL ("from no" / "challan no" /
#      "application no" / …) → return "non_student". This is decisive
#      regardless of what other fields are on the page.
#   3. Scan ALL <label> texts + every input's identifying attributes
#      for a STUDENT_IDENTITY_FIELD_SIGNAL ("enrollment number" /
#      "roll number" / "username" / "user id" / etc.). One hit →
#      "student".
#   4. Fall back to the body text — count STUDENT_CONTEXT_SIGNALS
#      ("student" / "semester" / "academic" / "department" / …).
#      ≥ STUDENT_CONTEXT_SIGNALS_NEEDED hits → "student".
#   5. Otherwise → "non_student" with reason
#      "no student identity field or context".

_NON_INPUT_TYPES_FOR_PRIMARY: frozenset[str] = frozenset({
    "password", "hidden", "submit", "button", "reset",
    "image", "file", "checkbox", "radio",
})

_PRIMARY_FIELD_NEARBY_CHARS: int = 100


def _primary_identifier_field_text(soup: BeautifulSoup) -> str:
    """Return the lowercased text used to identify the primary
    (non-password) input field: its identifying attributes plus its
    matched `<label for=...>` text plus a short window of its parent's
    text. Empty string when no eligible input exists."""
    primary = None
    for inp in soup.find_all("input"):
        t = (inp.get("type") or "text").strip().lower()
        if t in _NON_INPUT_TYPES_FOR_PRIMARY:
            continue
        primary = inp
        break
    if primary is None:
        return ""

    bits: list[str] = []
    for attr in ("placeholder", "aria-label", "name", "id", "title"):
        v = primary.get(attr)
        if v:
            bits.append(str(v))
    pid = primary.get("id")
    if pid:
        for lbl in soup.find_all("label", attrs={"for": pid}):
            t = lbl.get_text(" ", strip=True)
            if t:
                bits.append(t)
    # Parent-text window: most templates wrap "<label>Foo</label><input>"
    # or "<td>Foo</td><td><input></td>"; the parent's full text contains
    # the label even when no `for=` attribute links them. Truncate to
    # the user-spec ~100 char window so we don't pick up unrelated
    # neighbours.
    if primary.parent is not None:
        ptxt = primary.parent.get_text(" ", strip=True)
        if ptxt:
            bits.append(ptxt[:_PRIMARY_FIELD_NEARBY_CHARS])

    return " ".join(bits).lower()


def classify_login_form_audience(html: str) -> tuple[str, str]:
    """Bug 40 — examine the login form's *fields* for student-identity vs
    non-student-identity signals. Distinct from `classify_login_audience`
    which inspects page chrome.

    Returns ``(verdict, reason)``:

      * ``"non_student"`` — primary identifier field is "From No." /
        "Challan No." / "Application No." / etc. (an
        EXPLICIT_NON_STUDENT_FIELD_SIGNAL). Caller should REJECT.
      * ``"student"`` — any field label / placeholder matches a
        STUDENT_IDENTITY_FIELD_SIGNAL, or ≥2 STUDENT_CONTEXT_SIGNALS
        appear in the body. Caller may KEEP.
      * ``"non_student"`` (with reason "no student identity field or
        context") — neither student-identity field nor enough body
        context. Caller should REJECT.
      * ``"ambiguous"`` — body empty / no parseable HTML. Caller
        should fall back to other signals.
    """
    if not html:
        return "ambiguous", "no body"
    soup = BeautifulSoup(html, "html.parser")

    primary_text = _primary_identifier_field_text(soup)

    # Step 3 — primary-field non-student check (decisive).
    if primary_text:
        for sig in EXPLICIT_NON_STUDENT_FIELD_SIGNALS:
            if sig in primary_text:
                return "non_student", f"primary identifier field {sig!r}"

    # Step 4 — student-identity field anywhere on the form.
    label_bits: list[str] = []
    for lbl in soup.find_all("label"):
        t = lbl.get_text(" ", strip=True)
        if t:
            label_bits.append(t)
    for inp in soup.find_all("input"):
        for attr in ("placeholder", "aria-label", "name", "id", "title"):
            v = inp.get(attr)
            if v:
                label_bits.append(str(v))
    label_blob = " ".join(label_bits).lower()
    for sig in STUDENT_IDENTITY_FIELD_SIGNALS:
        if sig in label_blob:
            return "student", f"student-identity field signal {sig!r}"

    # Step 5 — fall back to body text scan.
    body_text = soup.get_text(" ", strip=True).lower()
    if not body_text:
        return "ambiguous", "empty body text"
    matched_ctx = [c for c in STUDENT_CONTEXT_SIGNALS if c in body_text]
    if len(matched_ctx) >= STUDENT_CONTEXT_SIGNALS_NEEDED:
        return "student", (
            f"student context signals ×{len(matched_ctx)}: "
            f"{matched_ctx[:3]}"
        )

    return "non_student", "no student identity field or context"


# Strong login-PATH tokens (any position) + portal/student host-label tokens
# (leftmost label only, to avoid matching them mid-string). Multilingual.
_LOGIN_PATH_TOKENS_LOOSE: tuple[str, ...] = (
    "login", "signin", "sign-in", "logon", "/sso", "/auth", "acesso",
    "entrar", "ingresar", "acceso", "autenticacao", "autogestion",
)
_PORTAL_HOST_LABELS: tuple[str, ...] = (
    "portal", "portais", "campus", "campusvirtual", "aulavirtual", "moodle",
    "autogestion", "guarani", "siu", "alumno", "alumnos", "aluno",
    "estudiante", "estudiantes", "student", "students", "sso", "id", "auth",
)


def _url_is_login_shaped(url: str) -> bool:
    """True if the URL names a login/portal surface (path token anywhere, or a
    portal-ish leftmost host label). Used by rule-E's loosened acceptance."""
    parts = urlsplit(url)
    host = parts.netloc.lower().split(":")[0]
    path = parts.path.lower()
    if any(t in path for t in _LOGIN_PATH_TOKENS_LOOSE):
        return True
    leftmost = host.split(".", 1)[0] if "." in host else host
    return any(leftmost == lbl or leftmost.startswith(lbl) for lbl in _PORTAL_HOST_LABELS)


def passes_login_signal_gate(
    *,
    final_url: str,
    html: str,
) -> tuple[bool, str]:
    """Bug 19 — strict A/B/C validation gate. A candidate URL is kept only
    if at least one of:

      A) `has_strict_login_form(html, final_url=...)` — real, same-host
         login form on the page itself.

      B) `is_homepage_url(final_url)` AND
         `find_strict_login_anchor_target(html, base_url=...)` — a strict
         (full-match anchor text + login-pathy href) login redirect lives
         on this page. Tentative keep — `_resolve_homepage_to_login_url`
         must validate the destination via rule A; if it can't, the
         candidate is dropped at the post-link-follow filter (REJECTED).

      C) `host_is_known_shared_platform(host)` — host is on
         `KNOWN_SHARED_PLATFORM_PATTERNS`. The hard verification gate
         (DNS, status, body length, …) still applies; this rule only
         exempts the URL from rule A's strict body-content requirement,
         since shared platforms render via JS / multi-step flows that
         don't always have a static password input.

    Returns (ok, reason). Failure reason is the canonical
    `"no login form, no login redirect, not on known platform"`.
    """
    host = urlsplit(final_url).netloc.lower().split(":")[0]
    if host_is_known_shared_platform(host):
        return True, "rule-C: known shared platform"
    a_ok, a_reason = has_strict_login_form(html, final_url=final_url)
    if a_ok:
        return True, "rule-A: strict login form"
    if is_homepage_url(final_url):
        if find_strict_login_anchor_target(html, base_url=final_url) is not None:
            return True, "rule-B: strict login anchor (link-follow will upgrade)"
    # Rule D — known *regional* platform (e.g. SIU-Guaraní, Moodle). Like
    # rule C, these render login via JS / multi-step flows with no static
    # form; the host/path signature is specific enough to accept directly.
    region_plat = regions.url_is_region_login_surface(final_url)
    if region_plat is not None:
        return True, f"rule-D: region platform ({region_plat[0]})"
    # Rule E — login/portal-shaped URL (loosened, geography-agnostic). When a
    # URL's path or leftmost host label clearly names a login/portal surface,
    # accept even without a static form (SPA logins render via JS). The caller
    # still enforces a body-length floor, audience checks, and off-domain
    # rejection, and results get human review in Training — so this widens
    # recall for global universities without opening the floodgates.
    if _url_is_login_shaped(final_url):
        return True, "rule-E: login-shaped url"
    return False, "no login form, no login redirect, not on known platform"


def score_student_anchor(href: str, text: str, parent_text: str = "") -> int:
    """Score an <a> element for student-portal-link likelihood.

    Additive (positive and negative signals all sum):
      * +3  exact phrase: "student login" / "student portal" / "student access"
      * +2  "student" anywhere in href or text
      * +2  "umis" / "ums" / "portal" in href/text AND parent context mentions student
      * +1  "login" / "signin"
      * -2  "staff" / "faculty" / "teacher" / "admin" / "employee"
      * -3  "alumni" / "recruitment" / "vendor"
    """
    href_l = (href or "").lower()
    text_l = (text or "").strip().lower()
    combined = href_l + " " + text_l
    parent_l = (parent_text or "").lower()
    score = 0
    if any(p in combined for p in _STUDENT_LINK_STRONG_PHRASES):
        score += 3
    if "student" in combined:
        score += 2
    if any(t in combined for t in _STUDENT_LINK_PLATFORM_TOKENS) and "student" in parent_l:
        score += 2
    if any(t in combined for t in _STUDENT_LINK_LOGIN_TOKENS):
        score += 1
    if any(t in combined for t in _STUDENT_LINK_NEGATIVE_TOKENS_2):
        score -= 2
    if any(t in combined for t in _STUDENT_LINK_NEGATIVE_TOKENS_3):
        score -= 3
    return score


def extract_top_student_links(
    html: str,
    *,
    base_url: str,
    max_n: int = 3,
    min_score: int = 2,
) -> list[tuple[str, int, str]]:
    """Return [(absolute_url, score, anchor_text), ...] sorted by score desc.

    Skips fragments, mailto:, tel:, javascript: hrefs. Resolves relative URLs
    against `base_url`. Deduplicates by absolute URL. Caps at `max_n`.
    """
    if not html:
        return []
    from urllib.parse import urljoin
    soup = BeautifulSoup(html, "html.parser")
    scored: list[tuple[str, int, str]] = []
    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href", "")
        if not href or any(href.startswith(prefix) for prefix in _LINK_INELIGIBLE_PREFIXES):
            continue
        text = anchor.get_text(strip=True) or ""
        parent = anchor.find_parent(["li", "div", "p", "section", "nav"])
        parent_text = parent.get_text(strip=True) if parent else ""
        score = score_student_anchor(href, text, parent_text)
        if score < min_score:
            continue
        try:
            abs_url = urljoin(base_url, href)
        except Exception:
            continue
        scored.append((abs_url, score, text))
    scored.sort(key=lambda x: -x[1])
    seen: set[str] = set()
    out: list[tuple[str, int, str]] = []
    for url, s, t in scored:
        if url in seen:
            continue
        seen.add(url)
        out.append((url, s, t))
        if len(out) >= max_n:
            break
    return out


def extract_login_links(
    html: str,
    *,
    base_url: str,
    max_n: int = 5,
    min_score: int = 2,
) -> list[tuple[str, int, str]]:
    """Bug 9 — broader anchor scan than `extract_top_student_links`. Returns
    [(absolute_url, score, anchor_text), ...] sorted by score descending,
    where score combines:

      * `score_student_anchor` (existing — strong-phrase, student tokens,
        platform tokens, negative tokens for staff/alumni etc.)
      * `score_login_path_specificity` of the *destination* URL path
        (Bug 7 boosts for /student paths, penalties for /college, /admin,
        /staff, /faculty, etc.)
      * +2 if anchor text or href matches a `LOGIN_LINK_TEXT_PATTERN`
        (login / sign in / user login / member login / my account / etc.)

    Used to upgrade a bare-host candidate (e.g. `lib.unipune.ac.in/`) to
    its specific login URL (`lib.unipune.ac.in/user/login`)."""
    if not html:
        return []
    from urllib.parse import urljoin
    soup = BeautifulSoup(html, "html.parser")
    scored: list[tuple[str, int, str]] = []
    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href", "")
        if not href or any(href.startswith(prefix) for prefix in _LINK_INELIGIBLE_PREFIXES):
            continue
        text = anchor.get_text(strip=True) or ""
        parent = anchor.find_parent(["li", "div", "p", "section", "nav"])
        parent_text = parent.get_text(strip=True) if parent else ""
        try:
            abs_url = urljoin(base_url, href)
        except Exception:
            continue

        combined_lower = (href + " " + text).lower()
        login_text_hit = any(p in combined_lower for p in LOGIN_LINK_TEXT_PATTERNS)
        if not login_text_hit:
            continue

        score = score_student_anchor(href, text, parent_text)
        score += score_login_path_specificity(abs_url)
        score += 2  # baseline boost for matching a login-shaped anchor
        if score < min_score:
            # Negative-scored anchors (path matched a non-student keyword)
            # never replace a parent — bug surfaced when `hallticket.unipune.ac.in/`
            # got upgraded to `hallticketnew.unipune.ac.in/College/...` via
            # an anchor whose total score was -7.
            continue
        scored.append((abs_url, score, text))

    scored.sort(key=lambda x: -x[1])
    seen: set[str] = set()
    out: list[tuple[str, int, str]] = []
    for url, s, t in scored:
        if url in seen:
            continue
        seen.add(url)
        out.append((url, s, t))
        if len(out) >= max_n:
            break
    return out


def _strong_signal_override(c: Candidate, *, primary_domain: str) -> bool:
    """Option-1 override: bypass Filter 2's college-specific heuristic when we
    have strong evidence this is a real student-login portal on the
    university's own domain tree.

    Conditions (ALL must hold):
      - `has_password_input` = True (a real <input type="password"> was
        detected, either in static HTML or after JS rendering).
      - Host is a strict subdomain of the primary configured domain.
      - URL path contains one of `/login`, `/signin`, `/sign/`, `/auth`.
    """
    if not c.has_password_input:
        return False
    if not primary_domain:
        return False
    host = _host_of(c.url)
    if not host.endswith("." + primary_domain):
        return False
    path = (urlsplit(c.url).path or "").lower()
    return any(tok in path for tok in _STRONG_SIGNAL_LOGIN_PATH_TOKENS)


def _innermost_subdomain_label(host: str, primary_domain: str) -> str:
    """Label closest to `primary_domain` (for logging the subdomain that
    would have been flagged by Filter 2)."""
    labels, _ = _subdomain_labels(host, [primary_domain])
    return labels[-1] if labels else host


def _host_passes_filter_2(
    host: str,
    *,
    allowed_domains: list[str],
    extra_allowed_root_domains: list[str],
    extra_allowed_labels: set[str],
    shortname_candidates: set[str] | frozenset[str] = frozenset(),
    acronym_candidates: set[str] | frozenset[str] = frozenset(),
) -> tuple[bool, str]:
    # (A) Shared-platform portal matching this OrgID's labels or acronyms.
    # Tighter than rule (A') below — used when the OrgID has a clean
    # acronym/shortname that pins the tenant prefix to this university
    # (e.g. SPPU → `sppu.digitaluniversity.ac`).
    if is_shared_platform_for_university(
        host,
        label_candidates=shortname_candidates,
        acronym_candidates=acronym_candidates,
    ):
        return True, ""

    # (A') Bug 25 — host on KNOWN_SHARED_PLATFORM_PATTERNS without a
    # label/acronym match. Validation accepts these via rule C
    # (`host_is_known_shared_platform`), so consolidation must mirror that
    # or every rule-C candidate gets dropped here. The cross-university
    # filter that rule (A) implements is intentionally relaxed: short
    # acronyms (e.g. "PU" for Punjabi University, 2 chars and below the
    # `compute_acronym` ≥3-char minimum) leave rule (A) unable to match
    # the legitimate tenant prefix — the cost of filtering cross-university
    # noise here is greater than the benefit.
    if host_is_known_shared_platform(host):
        return True, ""

    # (B) Under a permissive extra_allowed_root_domain — no label check
    if _host_under_any(host, extra_allowed_root_domains):
        return True, ""

    # (C) Under a strict configured domain — apply label filter
    labels, matched = _subdomain_labels(host, allowed_domains)
    if matched is None:
        return False, f"off-domain: {host}"
    if not labels:
        return True, ""
    offenders: list[str] = []
    for lab in labels:
        low = lab.lower()
        if low in ALLOWED_FUNCTIONAL_LABELS or low in extra_allowed_labels:
            continue
        if _label_is_college_specific(low):
            offenders.append(low)
    if offenders:
        return False, f"college-specific subdomain: {','.join(offenders)}"
    return True, ""


def score_login_path_specificity(url: str) -> int:
    """Bug 7 — score a URL's path for who-the-login-is-for.

    +STUDENT_LOGIN_PATH_BOOST per `STUDENT_LOGIN_PATH_KEYWORDS` hit
    +NON_STUDENT_LOGIN_PATH_PENALTY per `NON_STUDENT_LOGIN_PATH_KEYWORDS` hit
    Sum is returned; a single non-student keyword (penalty -10) typically
    disqualifies a candidate against a peer with a student keyword (+5)
    on the same host. Path comparison is case-insensitive."""
    path = (urlsplit(url).path or "").lower()
    score = 0
    for kw in STUDENT_LOGIN_PATH_KEYWORDS:
        if kw in path:
            score += STUDENT_LOGIN_PATH_BOOST
    for kw in NON_STUDENT_LOGIN_PATH_KEYWORDS:
        if kw in path:
            score += NON_STUDENT_LOGIN_PATH_PENALTY
    return score


def score_candidate(c: Candidate, primary_domain: str) -> int:
    score = 0
    parsed = urlsplit(c.url)
    path = (parsed.path or "").lower()
    host = parsed.netloc.lower().split(":")[0]

    if any(tok in path for tok in LOGIN_PATH_TOKENS):
        score += 3

    hints = CATEGORY_SUBDOMAIN_HINTS.get(c.category, ())
    if hints and primary_domain:
        labels, _ = _subdomain_labels(host, [primary_domain])
        if any(label.lower() in hints for label in labels):
            score += 2

    if c.has_password_input:
        score += 2

    if primary_domain and (host == primary_domain or host == "www." + primary_domain):
        score += 1

    segments = [s for s in path.split("/") if s]
    if len(segments) <= 2:
        score += 1

    # Bug 7 — student vs non-student login path specificity. Folded into the
    # consolidation score so `_pick_better` naturally prefers the student
    # variant when two candidates share the same (host, category) key.
    score += score_login_path_specificity(c.url)

    return score


def _pick_better(
    a: tuple[Candidate, int], b: tuple[Candidate, int]
) -> tuple[tuple[Candidate, int], tuple[Candidate, int]]:
    ac, as_ = a
    bc, bs = b
    if as_ != bs:
        return (a, b) if as_ > bs else (b, a)
    a_path = urlsplit(ac.url).path.rstrip("/")
    b_path = urlsplit(bc.url).path.rstrip("/")
    if len(a_path) != len(b_path):
        return (a, b) if len(a_path) < len(b_path) else (b, a)
    return a, b


def consolidate_candidates(
    candidates: list[Candidate],
    *,
    allowed_domains: list[str],
    extra_allowed_subdomains: list[str] | None = None,
    extra_allowed_root_domains: list[str] | None = None,
    shortname_candidates: set[str] | frozenset[str] | None = None,
    acronym_candidates: set[str] | frozenset[str] | None = None,
    primary_domain: str | None = None,
    extra_effective_domains: list[str] | None = None,
    state: str | None = None,
    exact_shortnames: list[str] | None = None,
    portal_anchored_hosts: set[str] | frozenset[str] | None = None,
    orgid: str,
) -> list[tuple[Candidate, int]]:
    extra_labels = {lbl.lower() for lbl in (extra_allowed_subdomains or [])}
    extra_roots = [r.lower().lstrip(".") for r in (extra_allowed_root_domains or [])]
    shortnames = frozenset(s.lower() for s in (shortname_candidates or set()))
    acronyms = frozenset(a.lower() for a in (acronym_candidates or set()))

    if extra_labels or extra_roots:
        logger.info(
            "[%s] applied override for orgid=%s: extra subdomains %s, extra root domains %s",
            orgid, orgid, sorted(extra_labels), extra_roots,
        )

    primary = (
        primary_domain
        if primary_domain is not None
        else (allowed_domains[0] if allowed_domains else "")
    )
    extra_eff_doms = list(extra_effective_domains or [])
    exact_short = list(exact_shortnames or [])
    pa_hosts = portal_anchored_hosts or frozenset()

    # --- Bug 30 strict membership re-check ---
    # Belt-and-suspenders with the sibling-walk filter in discovery.py.
    # Cross-contamination can otherwise reach this point via Pass-2
    # (Claude fallback) candidates, link-follow upgrades that switched
    # host, or any future code path that adds candidates after the
    # sibling-walk filter ran.
    membership_filtered: list[Candidate] = []
    for c in candidates:
        # Fix 2 — rule-C bypass: candidates accepted via known-shared-
        # platform rule at validation time skip the consolidate
        # membership re-check entirely. The strict R3/R4 check still
        # ran at admission time (sibling-walk filter, DDG-origin
        # re-check), so a foreign-tenant URL that doesn't match the
        # OrgID's shortnames was already rejected before reaching
        # validation. This bypass primarily helps OrgIDs whose
        # explicit Samarth tenant is known but the auto-derived
        # shortname / exact_shortname disambiguation is borderline.
        #
        # Section 4 refinement — rule-C bypass is SCOPED to platforms
        # that don't need cross-OrgID disambiguation. Samarth /
        # state-platform tenants (bihar-ums, digitaluniversity, …)
        # share a wildcard root across many institutions, so they
        # always need the strict R3/R4 tenant check at consolidate.
        # Without this gate, foreign-tenant URLs that pass rule-C at
        # validation (because the host IS on a known platform) leak
        # into the wrong OrgID's row (`dauniv.samarth.edu.in` →
        # SOL DU). Knimbus / Cognibot / MyLoft tenants don't share
        # wildcard roots that way, so they keep the bypass.
        host = _host_of(c.url)
        # Policy veto (user-requested) — applied to the FINAL URL so it
        # catches admin pages reached via a validation-time redirect that
        # the pre-validation filter (which saw the original probe URL)
        # missed:
        #   (a) WordPress / CMS admin backends (`wp-login.php`, `/wp-admin`,
        #       `/administrator`, …) — never a student login.
        #   (b) Samarth / state-platform recruitment & admission tenants
        #       (`mgahvrec.samarth.edu.in`, `<inst>admission.samarth.edu.in`).
        if url_is_admin_path(c.url):
            logger.info(
                "[%s] consolidate: drop %s — CMS/admin backend login (policy)",
                orgid, c.url,
            )
            continue
        # Final-stage instance-blocklist veto. The pre-validation filter
        # rejects blocklisted hosts (e.g. *.samarth.ac.in — the Samarth
        # employee/faculty/admin portal, never a student login), but the
        # js-render KEEP path can re-admit the same host without re-checking.
        # Re-apply here so a blocklisted host can never reach the sheet.
        if host_in_instance_blocklist(host):
            logger.info(
                "[%s] consolidate: drop %s — instance blocklist (e.g. samarth.ac.in "
                "employee portal)", orgid, c.url,
            )
            continue
        if is_nonstudent_platform_tenant(host):
            logger.info(
                "[%s] consolidate: drop %s — recruitment/admission platform "
                "tenant (policy)", orgid, c.url,
            )
            continue
        # Foreign-TLD veto. All institutions here are Indian, so a host on a
        # foreign academic/country TLD (e.g. moodle.amity.ac.uk) is a same-
        # brand overseas campus, not the target portal — drop it even though
        # the 'amity' shortname matched during membership.
        if is_foreign_academic_host(host):
            logger.info(
                "[%s] consolidate: drop %s — foreign academic TLD (Indian "
                "institution expected)", orgid, c.url,
            )
            continue
        # Section 9 — drop bare-homepage candidates that survived
        # rule-B but link-follow couldn't upgrade. The homepage URL
        # itself is never a student portal; if it weren't supposed to
        # be a candidate, link-follow would have replaced its URL
        # with the real login destination by now.
        if (
            _is_university_homepage(c.url, primary or "")
            and not c.has_password_input
        ):
            logger.info(
                "[%s] consolidate: drop %s — university homepage with "
                "no direct login form (rule-B link-follow didn't upgrade)",
                orgid, c.url,
            )
            continue
        if c.rule_c_bypass and not _host_needs_strict_tenant_check(host, state):
            membership_filtered.append(c)
            continue
        ok, reason = host_belongs_to_org(
            host,
            primary=primary,
            domains=allowed_domains,
            extra_effective_domains=extra_eff_doms,
            state=state,
            exact_shortnames=exact_short,
            portal_anchored_hosts=pa_hosts,
            auto_shortnames=shortnames,
            acronym=max(acronyms, key=len, default=""),
        )
        if not ok:
            # Mirror the discovery.py log-level split: WARNING is
            # reserved for real cross-state contamination (state
            # explicitly known + mismatched). When this OrgID has no
            # state set, every state-platform host trips R0 and we
            # don't want to spam the console with WARNINGs for noise
            # we can't fix without a state override.
            if reason.startswith("foreign state-platform") and not state:
                logger.debug(
                    "[%s] membership skip %s: %s (state unknown)",
                    orgid, host, reason,
                )
            else:
                logger.warning(
                    "[%s] membership REJECTED %s: %s", orgid, host, reason,
                )
            continue
        membership_filtered.append(c)

    # --- Filter 2: university-wide only (with strong-signal override) ---
    filtered_2: list[Candidate] = []
    for c in membership_filtered:
        host = _host_of(c.url)
        if _strong_signal_override(c, primary_domain=primary):
            label = _innermost_subdomain_label(host, primary)
            logger.info(
                "[%s] filter2-override: %s not in allow-list but has "
                "password-input + login-path on primary domain — keeping %s",
                orgid, label, c.url,
            )
            filtered_2.append(c)
            continue
        ok, reason = _host_passes_filter_2(
            host,
            allowed_domains=allowed_domains,
            extra_allowed_root_domains=extra_roots,
            extra_allowed_labels=extra_labels,
            shortname_candidates=shortnames,
            acronym_candidates=acronyms,
        )
        if not ok:
            logger.info("[%s] validate DROP %s — %s", orgid, c.url, reason)
            continue
        filtered_2.append(c)

    scored = [(c, score_candidate(c, primary)) for c in filtered_2]

    # --- Filter 1: one winner per (host, category) ---
    # Key is (scheme+host, category) so that sub-paths on the same host that
    # ended up in *different* categories (e.g. kuk.ac.in/hostels → Hostel
    # Portal vs kuk.ac.in/main-library → Library Portal) both survive.
    by_host_category: dict[tuple[str, str], tuple[Candidate, int]] = {}
    for c, s in scored:
        key = (_base_host(c.url), c.category)
        current = by_host_category.get(key)
        if current is None:
            by_host_category[key] = (c, s)
            continue
        winner, loser = _pick_better((c, s), current)
        logger.info(
            "[%s] consolidate DROP %s — host-dedup loser to %s",
            orgid, loser[0].url, winner[0].url,
        )
        by_host_category[key] = winner

    # Former Filter 3 ("one winner per category overall") is intentionally
    # removed: two candidates on *different* hosts in the same category
    # (e.g. iums.kuk.ac.in vs csdocs.kuk.ac.in, both Student Portal) both
    # survive here. The reviewer can choose between them downstream.
    return list(by_host_category.values())

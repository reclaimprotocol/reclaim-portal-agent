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
    DUCKDUCKGO_TIMEOUT_SECONDS,
    KNOWN_SHARED_PLATFORM_PATTERNS,
    LMS_HOST_TOKENS,
    LMS_THIRD_PARTY_HOSTS,
    LOGIN_LINK_TEXT_PATTERNS,
    NON_STUDENT_LOGIN_PATH_KEYWORDS,
    NON_STUDENT_LOGIN_PATH_PENALTY,
    STATE_PLATFORM_HINTS,
    STUDENT_LOGIN_PATH_BOOST,
    STUDENT_LOGIN_PATH_KEYWORDS,
    STUDENT_LOGIN_SAME_HOST_PROBES,
    SUBDOMAIN_PROBE_LIST,
    host_in_external_blocklist,
)
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
})


# =================================================================== utils

def parse_domains(raw: str) -> list[str]:
    return [d.strip().lower().lstrip(".") for d in (raw or "").split(",") if d.strip()]


# --- URL normalisation / session-ID stripping -----------------------------

_SESSION_ID_PATH_RE = re.compile(r";jsessionid=[^/?#]*", re.IGNORECASE)


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


def js_shell_suspicion_score(url: str, body: str) -> int:
    """Heuristic score for whether a static page is actually a JS shell.

    Scoring (must reach `JS_RENDERING_SUSPICION_THRESHOLD` = 3 to escalate
    to Playwright):

      Strong (+2):
        * "JavaScript disabled" exact phrase in body.
        * "Please enable JavaScript" (or close variant) in body.

      Medium (+1):
        * A `<noscript>` block whose content mentions "enable" or "javascript".
        * Body has fewer than 500 chars but ≥3 `<script src=...>` tags
          (classic SPA shell — entire app delivered via JS bundles).
        * Root SPA mount markers present (`#root`, `#app`, `[data-reactroot]`,
          `[ng-app]`, `[data-vue-root]`).

    A page therefore needs at least one strong + one medium indicator
    (or two strong) to trigger Playwright. Avoids spinning up Chromium
    for every slightly-empty page.
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
    words = [w for w in name.split() if w.lower() not in _ACRONYM_STOPWORDS]
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


# Samarth platform roots — used by the strict membership rule (Bug 30).
# Treated as ambiguous tenant hosts: any tenant subdomain must strictly
# match this OrgID's `exact_shortnames` to avoid cross-university leakage
# (e.g. pup.samarth.ac.in [Punjabi U.] leaking into Patna's row).
_SAMARTH_PLATFORM_ROOTS: tuple[str, ...] = ("samarth.edu.in", "samarth.ac.in")


def host_belongs_to_org(
    host: str,
    *,
    primary: str,
    extra_effective_domains: list[str],
    state: str | None,
    exact_shortnames: list[str],
    portal_anchored_hosts: set[str] | frozenset[str],
) -> tuple[bool, str]:
    """Bug 30 — strict per-OrgID host membership rule.

    A candidate host belongs to OrgID X iff at least one of these holds:

    (1) Host == primary or any `extra_effective_domain` for X
    (2) Host is a subdomain of any `extra_effective_domain` for X
    (3) Host is on a state-platform domain (STATE_PLATFORM_HINTS[X.state])
        AND the institutional subdomain prefix is in X's `exact_shortnames`
    (4) Host is on samarth.edu.in / samarth.ac.in AND the tenant subdomain
        prefix is in X's `exact_shortnames`
    (5) Host was reached by following a portal-pattern outbound anchor on
        the verified primary homepage (Bug 22 portal-anchored sibling)

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
    for d in extra_effective_domains or []:
        d_n = (d or "").lower().lstrip(".")
        if d_n and d_n not in owned:
            owned.append(d_n)
    exact_lower = {s.lower() for s in (exact_shortnames or []) if s}

    # Rules (3) + (4): platform-tenant gates. When host is on a
    # state-platform or samarth root, the strict shortname check is
    # authoritative — we don't fall through to (1)/(2). This keeps a
    # state-platform listed in `extra_effective_domains` (e.g. bihar-ums.com
    # for Patna) from trivially admitting every tenant subdomain.
    state_doms: tuple[str, ...] = (
        STATE_PLATFORM_HINTS.get(state, ()) if state else ()
    )
    platform_roots = tuple(d.lower().lstrip(".") for d in state_doms) + _SAMARTH_PLATFORM_ROOTS

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
            # and multi-label tenants both work.
            tokens: set[str] = {prefix}
            if "." in prefix:
                tokens.add(prefix.split(".")[-1])
            if not exact_lower:
                return False, (
                    f"platform {plat} tenant '{prefix}': no exact_shortnames "
                    f"configured for this OrgID"
                )
            if any(t in exact_lower for t in tokens):
                return True, (
                    f"platform {plat} tenant '{prefix}' ∈ exact_shortnames "
                    f"{sorted(exact_lower)}"
                )
            return False, (
                f"platform {plat} tenant '{prefix}' ∉ exact_shortnames "
                f"{sorted(exact_lower)}"
            )

    # Rules (1) + (2): owned domain equality / subdomain.
    for d in owned:
        if h == d:
            return True, f"owned domain {d}"
        if h.endswith("." + d):
            return True, f"subdomain of owned {d}"

    # Rule (5): portal-anchored sibling host (Bug 22). The host appeared
    # on the primary homepage as a strict portal-pattern anchor — already
    # a verified university link.
    if portal_anchored_hosts and h in portal_anchored_hosts:
        return True, "portal-anchored sibling on primary homepage"

    return False, "shortname/state mismatch"


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
})

# Path tokens the destination URL must contain (case-insensitive substring).
_STRICT_LOGIN_PATH_TOKENS: tuple[str, ...] = (
    "/login", "login.aspx", "/signin",
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
    "back office", "backoffice",
    "internal portal", "intranet",
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
    """Bug 19 — strict rule B candidate. Find the first `<a>` whose visible
    text full-matches one of `_STRICT_LOGIN_ANCHOR_TEXTS` and whose href
    resolves to a URL with a `_STRICT_LOGIN_PATH_TOKENS` path token.

    Returns (absolute_url, anchor_text) for the first hit, or None.

    The caller must still fetch the destination and run rule A on it
    before accepting — this function only reports that the page DOES point
    at something login-shaped.
    """
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")
    for anchor in soup.find_all("a", href=True):
        href = (anchor.get("href") or "").strip()
        text = anchor.get_text(strip=True)
        if not href or not text:
            continue
        if text.lower() not in _STRICT_LOGIN_ANCHOR_TEXTS:
            continue
        try:
            abs_url = urljoin(base_url, href)
        except Exception:
            continue
        path = (urlsplit(abs_url).path or "").lower()
        if not any(tok in path for tok in _STRICT_LOGIN_PATH_TOKENS):
            continue
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
        host = _host_of(c.url)
        ok, reason = host_belongs_to_org(
            host,
            primary=primary,
            extra_effective_domains=extra_eff_doms,
            state=state,
            exact_shortnames=exact_short,
            portal_anchored_hosts=pa_hosts,
        )
        if not ok:
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

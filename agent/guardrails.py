"""Genie-V3 · Layer 3 — Post-Inference HTTP Hook.

The model returns URLs it believes are student portals. This module is the
cheap, factual check that runs immediately after: does that address actually
answer? A confidently-worded portal URL that 404s is worse than no answer,
because it ships to a customer as verified fact.

WHAT COUNTS AS ALIVE — AND WHY 401/403/429 DO
---------------------------------------------
A WAF challenging us proves a server is there and responding; it says nothing
about whether the portal exists. V2 treated those as dead and produced a run of
false "unreachable — remove" verdicts on portals that were perfectly healthy,
which is why they are explicitly TRUE here.

    2xx / 3xx          -> True   (answering, possibly after redirects)
    401 / 403 / 429    -> True   (WAF/auth wall: the server is alive)
    405                -> retry with GET; many university stacks reject HEAD
    404 / 5xx / other  -> False
    timeout / network  -> False

TWO THINGS THIS DOES BEYOND A BARE HEAD REQUEST
-----------------------------------------------
Both exist because without them the check reports live portals as dead:

  * TLS verification is off. University TLS is frequently misconfigured
    (expired, self-signed, wrong SAN) and aiohttp aborts on that before it ever
    sees a status code.
  * Requests are routed through the residential proxy by ccTLD, the same rule
    the crawler uses. Verifying a geo-blocked Brazilian portal from an Indian
    exit is how you turn a working portal into a dead one.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import re
import ssl
import unicodedata
import weakref
from typing import Any, Iterable, Sequence
from urllib.parse import (parse_qsl, quote, urlencode, urljoin, urlsplit,
                          urlunsplit)

import aiohttp
from bs4 import BeautifulSoup

from agent import proxy as _proxy
from agent.admission import is_admission_portal, is_admission_url
from agent.graph_matcher import registrable_root
from agent.languages import LOGIN_WORD_RE as _NATIVE_LOGIN_WORD_RE
from agent.languages import STUDENT_AUDIENCE_RE as _NATIVE_STUDENT_AUDIENCE_RE
from agent.regions import student_platform_fingerprint

logger = logging.getLogger("genie.guardrails")

#: Status codes that prove the host is answering even though it refuses us.
WAF_ALIVE_CODES = frozenset({401, 403, 429})
#: Server said "not that verb" — retry with GET before believing it.
METHOD_NOT_ALLOWED = 405

USER_AGENT = os.getenv(
    "GENIE_UA",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
)

# A portal result must lead a student to a browser login surface, not a PDF,
# a network appliance, or an informational page that merely happens to answer
# HTTP.  Keep this check here (rather than in the model prompt) because it is
# an objective property of the final response.
_DOCUMENT_PATH_RE = re.compile(
    r"\.(?:pdf|docx?|xlsx?|pptx?|od[stp]|rtf)(?:$|[?#])", re.I)
_NON_PORTAL_UTILITY_HOST_RE = re.compile(
    r"(?:^|[.-])(?:directory|staff-directory|employee-directory)(?:[.-]|$)", re.I)
_PSEUDO_WEB_HOSTS = frozenset({
    "javascript", "mailto", "tel", "data", "file", "sms", "void",
})
# Pages in these route families may contain a site-wide Login link, but they
# are documentation/content *about* a portal rather than a portal entry point.
# Reject them before rendering so a header/footer control cannot promote them
# into the final spreadsheet.
_NON_PORTAL_CONTENT_PATH_RE = re.compile(
    r"(?:^|/)(?:faqs?|help|guides?|manuals?|docs?|documentation|news|"
    r"articles?|blogs?)(?:/|$)|"
    r"/admin/tool/(?:policy|dataprivacy)(?:/|$)|"
    r"/(?:course/view|enrol/index|mod/(?:page|book)/view)\.php(?:/|$)|"
    r"/(?:courses?|classes?)/\d+(?:/|$)|"
    r"/portal/site/!(?:error|gateway)(?:/|$)|"
    r"/(?:error|errors?)/(?:[^/?#]+/)*[^/?#]*(?:login|auth)[^/?#]*(?:/|$)|"
    r"/online_guide(?:/|$)|"
    r"/services?/portal/(?:faq|student|member)(?:/|$)|"
    r"/(?:okomariguide|mfatype(?:-en)?)\.html(?:/|$)|"
    r"/(?:first|1st)[_-]?login(?:\.[a-z0-9]+)?(?:/|$)|"
    r"(?:^|/)(?:privacy|privacy-policy|policy|policies|terms|terms-of-use)"
    r"(?:\.[a-z0-9]+)?(?:/|$)",
    re.I,
)
# Login screens that are real but never a student portal: CMS and hosting
# control panels. A WordPress or Plesk login has a perfect username/password
# form, so the form check alone cannot reject it.
_NON_PORTAL_LOGIN_PATH_RE = re.compile(
    r"(?:^|/)(?:wp-login\.php|wp-admin|login_up\.php|administrator)(?:/|$)|"
    r"(?:^|/)(?:nextcloud|owncloud|roundcube|zimbra|webmail|owa)(?:/|$)",
    re.I,
)
# Hosts whose first label names a service that is not a student academic
# portal: mail, file storage, careers and complaints. Libraries are NOT listed;
# the audience check deliberately accepts library systems as student services.
_NON_PORTAL_HOST_LABELS = frozenset({
    "webmail", "mail", "email", "owa", "roundcube", "zimbra",
    "nextcloud", "owncloud",
    "career", "careers", "placement", "placements", "jobs",
    "recruit", "recruitment", "alumni", "alum",
    "grievance", "grievances", "complaint", "complaints", "redressal",
})
# A path that names another audience outright (`/staff/login`,
# `recruiter_login.php`, `/faculty-portal`). The word must stand alone or be
# joined to a login word, so `/transparent/` and `/shr/` do not match.
_NON_STUDENT_PATH_RE = re.compile(
    r"(?:^|[/_.-])(?:staff|faculty|employees?|teachers?|payroll|alumni|parents?|"
    r"vendors?|recruiters?|recruitment|employers?|grievances?|placements?|hr)"
    r"(?:[-_]?(?:login|signin|sign-in|portal|panel|area))?"
    r"(?:\.[a-z0-9]{2,5})?(?:[/_.-]|$)|"
    r"(?:^|[/_.-])admin[-_]?(?:login|panel|portal)(?:\.[a-z0-9]{2,5})?(?:[/_.-]|$)|"
    # A path that ends at `/admin` or `/admin/login` is a back-office console.
    # Deeper `/admin/...` routes are left to the content rules above.
    r"(?:^|/)admin(?:/(?:login|signin)(?:\.[a-z0-9]{2,5})?)?/?$",
    re.I,
)
_STUDENT_PATH_WORD_RE = re.compile(
    r"student|learner|alun[oa]|estudiante|alumn[oa]s?(?![a-z])|mahasiswa|"
    r"ogrenci|etudiant", re.I)
#: Click-tracking query keys. They identify the visitor's ad click, never the
#: page, so two URLs differing only in these are the same portal.
_TRACKING_PARAMS = frozenset({
    "_gl", "_ga", "fbclid", "gclid", "gbraid", "wbraid", "msclkid", "dclid",
    "mc_cid", "mc_eid", "yclid", "igshid",
})
_TRACKING_PARAM_PREFIXES = ("utm_", "gad_")
_JSESSIONID_RE = re.compile(r";jsessionid=[^/?#;]*", re.I)
_INDEX_DOCUMENT_RE = re.compile(
    r"/(?:index|default)\.(?:php|html?|aspx?|jsp|do)$", re.I)
_LOGIN_WORD_RE = re.compile(
    r"\b(?:log\s*in|login|sign\s*in|signin|authenticate|authori[sz]e|"
    r"student\s+access|access\s+account)\b|"
    r"(?:ログイン|サインイン|認証|ログオン)", re.I)
_IDENTIFIER_RE = re.compile(
    r"(?:user(?:name)?|student|email|login|roll|enrol|enroll|registration|"
    r"account|candidate|id(?:entifier)?|number)", re.I)
_STUDENT_AUDIENCE_RE = re.compile(
    r"\b(?:student|students|learner|learners|scholar|scholars|roll\s*(?:no|number)|"
    r"enrol(?:ment|ment\s+number)?|registration\s*(?:no|number)?|academic|"
    r"course(?:work)?|class(?:room)?|campus|library|catalog(?:ue)?|opac|borrower)\b|"
    r"(?:学生|学習管理システム|学習|授業|履修|教育|講義|科目|図書館)", re.I)
_NON_STUDENT_AUDIENCE_RE = re.compile(
    r"\b(?:admission(?:s)?|applicant(?:s)?|application\s+portal|prospective\s+student|"
    r"staff|faculty|employee(?:s)?|human\s+resources|payroll|administrator|admin\s+login|"
    r"alumni|parent(?:s)?|vendor|partner)\b", re.I)
# The last line names student-information-system vendors whose tenant login
# screens are a bare "Username / Password". Only `samarth.edu.in` is listed:
# `samarth.ac.in` is Samarth's employee portal, and "Samarth" alone is a common
# institution name. Regional systems (SIU Guaraní, ...) are not repeated here;
# they come from the region packs via `student_platform_fingerprint`.
_ACADEMIC_PLATFORM_RE = re.compile(
    r"(?:moodle|canvas(?:lms)?|blackboard|brightspace|d2l|chamilo|sakailms|"
    r"(?:^|[./_-])lms(?:[./_-]|$)|koha|ezproxy|library\s+catalog|opac|"
    r"\betlab\b|samarth\.edu\.in|mastersoft\s*erp)", re.I)
_OFFICIAL_STUDENT_SERVICE_URL_RE = re.compile(
    r"(?:^|[./_-])(?:student|campus|portal|lms|moodle|canvas|library|"
    r"my)(?:[./_-]|$)", re.I)
#: Prefix of the rejection note for an admission portal. Callers match on it to
#: tell "this is the wrong kind of login" from "no login was found".
ADMISSION_NOTE = "admission portal"
_MAX_LOGIN_PAGE_BYTES = 512 * 1024
_VOLATILE_AUTH_PARAMS = frozenset({
    "sessid", "session", "sessionid", "samlrequest", "samlresponse",
    "relaystate", "signature", "sigalg", "state", "nonce", "code",
})


def _has_login_word(text: str) -> bool:
    """Whether ``text`` carries sign-in wording in English or the page's language."""
    if not text:
        return False
    if _LOGIN_WORD_RE.search(text):
        return True
    return bool(_NATIVE_LOGIN_WORD_RE.search(unicodedata.normalize("NFC", text)))


def _is_volatile_auth_param(key: str) -> bool:
    """Whether a query key belongs to a one-time authentication transaction."""
    lower = (key or "").lower()
    return lower in _VOLATILE_AUTH_PARAMS or lower.startswith("openid.")
_render_sems: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, asyncio.Semaphore
] = weakref.WeakKeyDictionary()


def _get_render_sem() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    semaphore = _render_sems.get(loop)
    if semaphore is None:
        semaphore = asyncio.Semaphore(max(
            1, int(os.getenv("GENIE_VERIFY_BROWSER_CONCURRENCY", "2"))))
        _render_sems[loop] = semaphore
    return semaphore

#: Use the platform trust store and hostname validation for every URL probe.
#: A portal with an expired, mismatched, or otherwise untrusted certificate
#: must not pass verification and be written as a usable portal.
_SSL_CONTEXT = ssl.create_default_context()


def _as_url(url_path: str) -> str:
    # Search/model output occasionally includes a Markdown code delimiter as a
    # literal URL suffix (or its encoded form, %60).  It is never part of a
    # portal address and previously produced broken rows such as login.php%60.
    raw = (url_path or "").strip().strip("`\"'")
    raw = re.sub(r"(?:%60|`)+$", "", raw, flags=re.I)
    if not raw:
        raise ValueError("verify_portal_endpoint() needs a URL")
    if "://" not in raw:
        raw = "https://" + raw.lstrip("/")
    return raw


def _host_is_ip_literal(url: str) -> bool:
    """Whether ``url`` has an IPv4/IPv6 literal hostname.

    Student portals are public web applications with DNS names.  A literal IP
    can be an appliance, an internal panel, or a model hallucination; it also
    should never be fetched by this public discovery workflow.
    """
    try:
        host = urlsplit(_as_url(url)).hostname
        if not host:
            return False
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _normalised_host(url: str) -> str:
    """Hostname comparison that treats the www alias as the same site."""
    host = (urlsplit(_as_url(url)).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def portal_url_allowed(url: str) -> tuple[bool, str]:
    """Cheap, no-network rejection for non-web portal candidates."""
    try:
        normalised = _as_url(url)
        parsed = urlsplit(normalised)
    except ValueError:
        return False, "bad url"
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return False, "not an http(s) URL"
    # `https://support@example.edu` is an e-mail address someone typed as a
    # URL. urlsplit reads `support` as the username and `example.edu` as the
    # host, so without this the address would be verified as the homepage.
    if parsed.username is not None or "@" in parsed.netloc:
        return False, "e-mail address or credentials in URL"
    host = parsed.hostname.lower().rstrip(".")
    # Search/model output can turn a non-navigation href such as
    # `javascript:void(0)` into `https://javascript:void(0);`.  urlsplit sees
    # that as an HTTP URL whose hostname is literally "javascript", so scheme
    # validation alone is insufficient. Public portal outputs must be DNS
    # hostnames, never browser pseudo-schemes or single-label placeholders.
    if host in _PSEUDO_WEB_HOSTS or "." not in host:
        return False, "not a public DNS hostname"
    try:
        _ = parsed.port
    except ValueError:
        return False, "invalid network location"
    if _host_is_ip_literal(normalised):
        return False, "literal IP host"
    if _NON_PORTAL_UTILITY_HOST_RE.search(parsed.hostname or ""):
        return False, "non-portal directory utility host"
    if _DOCUMENT_PATH_RE.search(parsed.path or ""):
        return False, "document URL"
    if _NON_PORTAL_CONTENT_PATH_RE.search(parsed.path or ""):
        return False, "informational/content route"
    labels = host.removeprefix("www.").split(".")
    if len(labels) > 2 and labels[0] in _NON_PORTAL_HOST_LABELS:
        return False, "non-portal service host (mail/storage/careers/complaints)"
    if _NON_PORTAL_LOGIN_PATH_RE.search(parsed.path or ""):
        return False, "CMS, hosting-panel, mail or file-storage login"
    # `/student-staff/login` is a shared sign-in, which stays valid; only a
    # path that names another audience and no student one is rejected.
    if (_NON_STUDENT_PATH_RE.search(parsed.path or "")
            and not _STUDENT_PATH_WORD_RE.search(parsed.path or "")):
        return False, "non-student audience path"
    return True, "ok"


def canonical_portal_url(url: str) -> str:
    """Return a reusable output URL, removing presentation-only variants."""
    cleaned = _as_url(url)
    parsed = urlsplit(cleaned)
    query = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        lower = key.lower()
        if (lower in _TRACKING_PARAMS or lower in {"lang", "changelangcd"}
                or lower.startswith(_TRACKING_PARAM_PREFIXES)):
            continue
        query.append((key, value))
    # A servlet session id in the path belongs to one visit, not to the portal.
    path = _JSESSIONID_RE.sub("", parsed.path or "")
    return urlunsplit((parsed.scheme, parsed.netloc, path,
                       urlencode(query, doseq=True), ""))


def portal_dedupe_key(url: str) -> str:
    """Key that is equal for two addresses of the same portal page.

    Used only to compare, never to publish. It ignores what does not change the
    page a student lands on: http vs https, a leading ``www.``, a trailing
    slash, letter case in the path, and an explicit index document
    (``/login/index.php`` and ``/login/``).
    """
    parsed = urlsplit(canonical_portal_url(url))
    host = (parsed.hostname or "").lower().rstrip(".").removeprefix("www.")
    try:
        port = parsed.port
    except ValueError:
        port = None
    if port and port not in (80, 443):
        host = f"{host}:{port}"
    path = _INDEX_DOCUMENT_RE.sub("", (parsed.path or "/").rstrip("/"))
    path = path.rstrip("/").lower() or "/"
    return f"{host}{path}" + (f"?{parsed.query}" if parsed.query else "")


#: Evidence note for a sign-in page that asks for the username on its own.
IDENTIFIER_FIRST_NOTE = "username-first sign-in step (password asked on the next screen)"
# The account field of an identity provider's first screen. Deliberately
# narrower than `_IDENTIFIER_RE`: a bare "id" or "number" field also describes
# a search box or a tracking form.
_SSO_IDENTIFIER_FIELD_RE = re.compile(
    r"user(?:name|id|principalname)?|login(?:fmt|id|name)?|e-?mail|identifier|"
    r"account|j_username|netid|matric|usuario|utilisateur|kullanici", re.I)
# Single-field forms that are not a sign-in: search, newsletter sign-up,
# password recovery and account creation.
_NOT_SIGN_IN_FORM_RE = re.compile(
    r"search|buscar|pesquis|recherch|newsletter|subscri|suscri|inscrev|"
    r"forgot|forgotten|reset|recover|recuperar|esquec|olvid|oubli[eé]|"
    r"create\s+(?:an?\s+)?account|sign\s*up|register|registr|cadastr", re.I)
# Products whose sign-in really is split across two screens.
_SSO_PRODUCT_RE = re.compile(
    r"/adfs/ls|microsoftonline|kc-form-login|/realms/[^/\s\"']+/|okta-sign-in|"
    r"identifierid|shibboleth|/idp/profile|/cas/login|simplesaml", re.I)


def _identifier_first_step(soup: BeautifulSoup) -> bool:
    """Whether the page is the first screen of a two-step sign-in.

    ADFS, Keycloak, Okta and Google ask for the username alone and show the
    password field only after it is submitted, so there is no password input to
    find. Accept the page when one form holds exactly one visible account field
    and a submit control, and something says it is a sign-in: the control or
    the page title uses sign-in wording, or the markup is a known identity
    provider's. A search box, a newsletter form and a "forgot password" form
    have the same shape and are excluded by their own wording.
    """
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    headings = " ".join(h.get_text(" ", strip=True) for h in soup.find_all(["h1", "h2"])[:4])
    page_says_sign_in = _has_login_word(f"{title} {headings}")
    product = bool(_SSO_PRODUCT_RE.search(str(soup)[:200_000]))
    for form in soup.find_all("form"):
        if (form.get("role") or "").lower() == "search":
            continue
        fields = []
        for field in form.find_all("input"):
            kind = (field.get("type") or "text").lower()
            if kind in {"hidden", "submit", "button", "reset", "checkbox", "radio", "image"}:
                continue
            fields.append((kind, " ".join(str(field.get(k) or "") for k in
                                          ("name", "id", "placeholder", "autocomplete",
                                           "aria-label"))))
        if len(fields) != 1:
            continue
        kind, descriptor = fields[0]
        if kind not in {"text", "email", "tel", "number"}:
            continue
        if not _SSO_IDENTIFIER_FIELD_RE.search(descriptor):
            continue
        controls = form.find_all(["button", "input"])
        control_text = " ".join(
            " ".join([c.get_text(" ", strip=True), str(c.get("value") or ""),
                      str(c.get("aria-label") or "")])
            for c in controls
            if c.name == "button" or (c.get("type") or "").lower() in {"submit", "button"})
        if not control_text.strip() and not any(
                (c.get("type") or "").lower() == "submit" or c.name == "button"
                for c in controls):
            continue
        form_text = f"{form.get_text(' ', strip=True)} {descriptor} {form.get('action') or ''}"
        if _NOT_SIGN_IN_FORM_RE.search(f"{control_text} {descriptor} {form.get('action') or ''}"):
            continue
        if _has_login_word(control_text) or page_says_sign_in or product:
            # "Forgot your password?" is a normal link INSIDE a sign-in form,
            # so recovery wording is only disqualifying when the form has no
            # sign-in wording of its own.
            if (not _has_login_word(control_text) and not product
                    and _NOT_SIGN_IN_FORM_RE.search(form_text)):
                continue
            return True
    return False


def _login_form_evidence(html: str) -> tuple[bool, str]:
    """Return whether an HTML document exposes a usable sign-in affordance.

    We deliberately require a password field *and* an identifier/control.  A
    bare word such as "login" on a university homepage is not enough to call
    that page a student portal.  This is conservative by design: a page that
    cannot demonstrate a sign-in surface should not be emitted as a verified
    login URL.
    """
    soup = BeautifulSoup(html or "", "html.parser")
    password = soup.find("input", attrs={"type": re.compile(r"^password$", re.I)})
    if password is None:
        if _identifier_first_step(soup):
            return True, IDENTIFIER_FIRST_NOTE
        return False, "no password input"

    identifier = False
    for field in soup.find_all(["input", "textarea"]):
        field_type = (field.get("type") or "text").lower()
        if field_type in {"hidden", "password", "submit", "button", "reset", "checkbox", "radio"}:
            continue
        descriptor = " ".join(str(field.get(k) or "") for k in
                              ("name", "id", "placeholder", "autocomplete", "aria-label"))
        if field_type in {"email", "text", "number", "tel"} or _IDENTIFIER_RE.search(descriptor):
            identifier = True
            break
    if not identifier:
        return False, "no username/student identifier input"

    controls = soup.find_all(["button", "input", "a"])
    for control in controls:
        text = " ".join([
            control.get_text(" ", strip=True),
            str(control.get("value") or ""),
            str(control.get("aria-label") or ""),
            str(control.get("title") or ""),
        ])
        kind = (control.get("type") or "").lower()
        if _has_login_word(text) or kind == "submit":
            return True, "password, identifier, and sign-in control"
        # Some institutional LMS pages use an icon-only <a> or <div> whose
        # click handler submits the surrounding form.  It is still a genuine
        # authorisation control, just not a semantic <button>.  Require both
        # the password and identifier checks above before accepting it, so an
        # unrelated JavaScript click target cannot qualify a normal page.
        onclick = str(control.get("onclick") or "")
        if re.search(r"(?:\.submit\s*\(|requestSubmit\s*\()", onclick, re.I):
            return True, "password, identifier, and JavaScript submit control"
    return False, "no sign-in/authorise control"


def _student_audience_evidence(html: str, url: str) -> tuple[bool, str]:
    """Require evidence that a login belongs to students/academic services.

    The form check answers "can somebody sign in here?"; this check answers
    "is that somebody plausibly a student?"  Shared student/staff sign-in pages
    remain valid because they explicitly name students.  A staff-only or
    admissions-only page is rejected even when it has a perfectly real form.
    Known LMS, student-information-system and academic-library products are
    accepted as student services when their sparse login screen has no
    audience copy.

    Student evidence is read in the page's own language as well as English
    (`agent.languages.STUDENT_AUDIENCE_PATTERNS`); the non-student list is not
    widened, so a staff- or alumni-only page gains nothing from that.
    """
    soup = BeautifulSoup(html or "", "html.parser")
    page_text = soup.get_text(" ", strip=True)
    form_descriptors = " ".join(
        " ".join(str(field.get(k) or "") for k in
                 ("name", "id", "placeholder", "autocomplete", "aria-label"))
        for field in soup.find_all(["input", "textarea", "button"])
    )
    # NFC so decomposed diacritics (common on Vietnamese pages) still match.
    evidence = unicodedata.normalize(
        "NFC", f"{url}\n{page_text}\n{form_descriptors}")
    has_student_signal = bool(_STUDENT_AUDIENCE_RE.search(evidence)
                              or _NATIVE_STUDENT_AUDIENCE_RE.search(evidence))
    has_non_student_signal = bool(_NON_STUDENT_AUDIENCE_RE.search(evidence))
    if has_non_student_signal and not has_student_signal:
        return False, "non-student audience only"
    if has_student_signal:
        return True, "student/academic audience evidence"
    if _ACADEMIC_PLATFORM_RE.search(evidence):
        return True, "known LMS, student-system or academic-library platform"
    regional_platform = student_platform_fingerprint(evidence)
    if regional_platform:
        return True, f"known student information system ({regional_platform})"
    return False, "no student or academic audience evidence"


def _same_organisation_redirect(
    requested_url: str, final_url: str,
    shared_platform_roots: Iterable[str] = (),
) -> bool:
    """Whether a cross-host redirect stayed inside one organisation.

    Portals load-balance to sibling hosts (`portal.hcmus.edu.vn` ->
    `new-portal3.hcmus.edu.vn`), which is the same service under the same
    registrable root. Tenants of a multi-tenant vendor share the VENDOR's root
    while belonging to different institutions, so a hop between hosts under a
    root in ``shared_platform_roots`` is still a different organisation.
    """
    root = registrable_root(requested_url)
    if not root or root != registrable_root(final_url):
        return False
    return root not in {r.lower() for r in shared_platform_roots if r}


def _official_host(url: str, official_domain: str) -> bool:
    """Whether ``url`` is on one of the organisation's own domains.

    ``official_domain`` may list several domains separated by commas,
    semicolons or spaces; an organisation with two sites owns both.
    """
    host = _normalised_host(url)
    if not host:
        return False
    for domain in re.split(r"[,;\s]+", official_domain or ""):
        if not domain.strip():
            continue
        try:
            root = _normalised_host(domain)
        except ValueError:
            continue
        if root and (host == root or host.endswith("." + root)):
            return True
    return False


def _has_volatile_auth_state(url: str) -> bool:
    try:
        return any(_is_volatile_auth_param(k)
                   for k, _v in parse_qsl(urlsplit(url).query, keep_blank_values=True))
    except ValueError:
        return False


def _rendered_auth_control_target(
    html: str, base_url: str = "",
) -> tuple[bool, str, str]:
    """Find a real login/SSO action when credentials are entered downstream.

    Modern university portals often expose only a Login/SSO button and then
    redirect to Okta, Shibboleth or another IdP.  Require an actionable control
    rather than accepting a page merely because its prose mentions "login".
    """
    soup = BeautifulSoup(html or "", "html.parser")
    for control in soup.find_all(["button", "input", "a"]):
        text = " ".join([
            control.get_text(" ", strip=True),
            str(control.get("value") or ""),
            str(control.get("aria-label") or ""),
            str(control.get("title") or ""),
        ]).strip()
        href = str(control.get("href") or "").strip()
        onclick = str(control.get("onclick") or "").strip()
        kind = (control.get("type") or "").lower()
        descriptor = f"{text} {href} {onclick}"
        if not _has_login_word(descriptor):
            continue
        if (control.name in {"button", "input"} or kind in {"submit", "button"}
                or href.lower().startswith(("http://", "https://", "/"))
                or onclick):
            target = ""
            if control.name == "a" and href:
                target = urljoin(base_url, href) if base_url else href
            return True, "rendered login/SSO authorisation control", target
    return False, "no actionable rendered login/SSO control", ""


def _rendered_auth_control(html: str) -> tuple[bool, str]:
    """Compatibility wrapper used by focused unit tests."""
    accepted, reason, _target = _rendered_auth_control_target(html)
    return accepted, reason


#: Note added when a page's Login link could not be opened and checked, so the
#: page itself is published instead. `verify_student_login_surface_hybrid`
#: reads it to decide that a Chromium pass is worth its cost.
LINK_UNCONFIRMED_NOTE = "login link not confirmed"
#: Note added when a page is accepted on a Login BUTTON alone: the form it
#: opens was never seen. The hybrid verifier clicks it to find out.
BUTTON_UNOPENED_NOTE = "login button not opened"
#: The page's only sign-in links are labelled for another audience, or point
#: back to the page itself, which has no form.
NO_STUDENT_CONTROL_NOTE = "no student login control on the page"
# Link labels for audiences `_NON_STUDENT_AUDIENCE_RE` does not name.
_NON_STUDENT_LINK_LABEL_RE = re.compile(
    r"\b(?:recruiters?|employers?|teachers?|placements?|college|institute)\s+"
    r"(?:log\s*in|login|sign\s*in)\b", re.I)
#: `portal_url_allowed` reasons that are about the ADDRESS, not the page behind
#: it. A Login link to such an address is unverifiable rather than wrong.
_UNPUBLISHABLE_NOT_WRONG = frozenset({"literal IP host", "not a public DNS hostname"})
#: How many Login links of one page are opened before giving up.
_MAX_LOGIN_TARGETS = 3


def _login_controls(html: str, base_url: str = "") -> tuple[bool, list[str]]:
    """`_login_links` without the labels: ``(has_button, link_targets)``."""
    has_button, links = _login_links(html, base_url)
    return has_button, [target for target, _label in links]


def _login_links(html: str, base_url: str = "") -> tuple[bool, list[tuple[str, str]]]:
    """Every sign-in control on a page: ``(has_button, [(target, label), ...])``.

    ``has_button`` is true when a control signs in without a link of its own (a
    button, a submit input, a scripted element): the login then opens on this
    page. The links are the absolute addresses the Login links point to, each
    with the text the page shows for it, student-labelled links first. A link
    labelled for another audience ("Staff login", "Recruiter login") is left
    out, as is a link back to the page itself.
    """
    soup = BeautifulSoup(html or "", "html.parser")
    here = portal_dedupe_key(base_url) if base_url else ""
    has_button = False
    student_first: list[tuple[str, str]] = []
    others: list[tuple[str, str]] = []
    seen: set[str] = set()
    for control in soup.find_all(["button", "input", "a"]):
        text = " ".join([
            control.get_text(" ", strip=True),
            str(control.get("value") or ""),
            str(control.get("aria-label") or ""),
            str(control.get("title") or ""),
        ]).strip()
        href = str(control.get("href") or "").strip()
        onclick = str(control.get("onclick") or "").strip()
        kind = (control.get("type") or "").lower()
        if not _has_login_word(f"{text} {href} {onclick}"):
            continue
        is_link = control.name == "a" and href.lower().startswith(
            ("http://", "https://", "/"))
        if not is_link:
            if control.name in {"button", "input"} or kind in {"submit", "button"} or onclick:
                has_button = True
            continue
        label = unicodedata.normalize("NFC", f"{text} {href}")
        for_students = bool(_STUDENT_AUDIENCE_RE.search(label)
                            or _NATIVE_STUDENT_AUDIENCE_RE.search(label))
        other_audience = (_NON_STUDENT_AUDIENCE_RE.search(text)
                          or _NON_STUDENT_LINK_LABEL_RE.search(text))
        if other_audience and not for_students:
            continue
        target = urljoin(base_url, href) if base_url else href
        try:
            key = portal_dedupe_key(target)
        except ValueError:
            continue
        if key == here or key in seen:
            continue
        seen.add(key)
        (student_first if for_students else others).append((target, text))
    return has_button, student_first + others


def _classify_login_target(
    target: str, final_url: str, html: str, entry_url: str = "", label: str = "",
) -> tuple[str, str]:
    """Judge the page a Login link opened.

    ``student``  a student sign-in: publish it.
    ``wrong``    known to be something else (admissions, staff, a dead or
                 disallowed page).
    ``unknown``  loaded, but no sign-in form in this HTML. That is what a
                 JavaScript-built login looks like before it is rendered.
    ``unsure``   a sign-in form on another site that does not say who it is
                 for. Rendering will not change that.

    ``entry_url`` is the page that carried the link and ``label`` the text it
    showed for it ("Student Login").
    """
    allowed, reason = portal_url_allowed(final_url)
    if not allowed:
        return "wrong", reason
    admission, admission_reason = is_admission_portal(f"{target} {final_url}", html)
    if admission:
        return "wrong", f"{ADMISSION_NOTE}: {admission_reason}"
    form_ok, form_reason = _login_form_evidence(html)
    if not form_ok:
        return "unknown", form_reason
    # What the institution calls the link is evidence too: "Student Login"
    # says who the sign-in is for even when the form behind it says nothing.
    audience_ok, audience_reason = _student_audience_evidence(
        html, f"{label} {target} {final_url}")
    if audience_reason == "non-student audience only":
        return "wrong", audience_reason
    # On the institution's own site the linking page already vouched for the
    # audience, so a sparse "Username / Password" screen is fine. A login that
    # is served from another site gets no such credit: `samarth.ac.in` (staff)
    # and `samarth.edu.in` (students) look the same from a university homepage,
    # and only the student one says so. "Served from" is where the request
    # LANDED: an on-site `/login` that redirects to an outside identity
    # provider is an off-site login, however local the link looked.
    entry_root = registrable_root(entry_url) if entry_url else ""
    same_site = bool(entry_root) and (
        entry_root == registrable_root(target) == registrable_root(final_url))
    if not audience_ok and not same_site:
        # Not proof of anything: plenty of real student portals are a vendor's
        # bare login screen. It is only not enough to PUBLISH the link.
        return "unsure", f"off-site login with {audience_reason}"
    return "student", form_reason


async def _fetch_html(
    url: str, timeout_seconds: int, country_hint: str, use_proxy: bool,
) -> tuple[int, str, str, str]:
    """GET ``url``: ``(status, final_url, html, error)``. Never raises."""
    routes = (_proxy_urls(url, country_hint) if use_proxy else []) or [("direct", None)]
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    async with aiohttp.ClientSession(timeout=timeout) as sess:
        for index, (_provider, proxy_url) in enumerate(routes):
            try:
                async with sess.get(url, allow_redirects=True,
                                    headers={"User-Agent": USER_AGENT},
                                    proxy=proxy_url, ssl=_SSL_CONTEXT) as resp:
                    body = await resp.content.read(_MAX_LOGIN_PAGE_BYTES)
                    return (resp.status, str(resp.url),
                            body.decode("utf-8", errors="ignore"), "")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                if index + 1 < len(routes) and _proxy_transport_failure(exc, proxy_url):
                    continue
                return 0, url, "", type(exc).__name__
    return 0, url, "", "no route"


async def _render_html(
    url: str, timeout_seconds: int, country_hint: str,
) -> tuple[str, str, str]:
    """Render ``url`` in Chromium: ``(html, final_url, error)``. Never raises.

    Tries each proxy route in turn; a transport failure or timeout on one moves
    on to the next.
    """
    from crawl4ai import AsyncWebCrawler
    from agent.crawler import (_browser_config, _run_config, WAIT_FOR_JS,
                               proxy_candidates_for)
    routes = proxy_candidates_for(url, country_hint) or [("direct", None)]
    result = None
    for index, (provider, proxy_cfg) in enumerate(routes):
        try:
            async with _get_render_sem():
                async with AsyncWebCrawler(config=_browser_config(proxy_cfg)) as crawler:
                    result = await crawler.arun(
                        url=url,
                        config=_run_config(timeout_seconds * 1000, WAIT_FOR_JS),
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            if (index + 1 < len(routes)
                    and (_proxy.is_proxy_failure(exc)
                         or isinstance(exc, asyncio.TimeoutError))):
                logger.info(
                    "guardrails: rendered login %s failed via %s; retrying via %s",
                    url, provider, routes[index + 1][0],
                )
                continue
            return "", url, f"render {type(exc).__name__}"

        html = getattr(result, "html", "") or ""
        error = str(getattr(result, "error_message", "") or "")
        if (not html and index + 1 < len(routes)
                and _proxy.is_proxy_failure(error)):
            logger.info(
                "guardrails: rendered login %s failed via %s; retrying via %s",
                url, provider, routes[index + 1][0],
            )
            continue
        break

    if result is None:
        return "", url, "render returned no result"
    html = getattr(result, "html", "") or ""
    final_url = (getattr(result, "redirected_url", None)
                 or getattr(result, "url", None) or url)
    if not html:
        return "", final_url, str(
            getattr(result, "error_message", "") or "render returned no HTML")[:180]
    return html, final_url, ""


async def _resolve_login_target(
    targets: Sequence[Any],
    *,
    entry_url: str = "",
    timeout_seconds: int,
    country_hint: str = "",
    use_proxy: bool = True,
    render_timeout_seconds: int = 0,
) -> tuple[str, str, str]:
    """Open a page's Login links and find the one that is the student login.

    A Login link is a claim, not evidence: it can lead to a staff console, an
    admission site, a help page or a dead address. Returns
    ``(outcome, url, reason)``:

    ``confirmed``    a link opened a student sign-in; ``url`` is what to publish.
    ``unconfirmed``  at least one link loaded but its form could not be seen
                     (or could not be reached just now); nothing is known.
    ``wrong``        every link leads somewhere that is not a student login.

    With ``render_timeout_seconds`` set, the first link whose form is not in
    the plain HTML is rendered once in Chromium before giving up on it.
    """
    reasons: list[str] = []
    pending: list[str] = []
    # Each entry is a URL or a ``(url, label)`` pair from `_login_links`.
    pairs = [(t, "") if isinstance(t, str) else (t[0], t[1]) for t in targets]
    labels = dict(pairs)

    def publishable(target: str, final_url: str) -> str:
        return canonical_portal_url(_stable_verified_url(target, final_url))

    for target, label in pairs[:_MAX_LOGIN_TARGETS]:
        allowed, reason = portal_url_allowed(target)
        if not allowed:
            if reason in _UNPUBLISHABLE_NOT_WRONG:
                # The address is one this workflow will not publish or fetch,
                # which says nothing about what is behind it.
                pending.append("")
            else:
                reasons.append(reason)
            continue
        if _has_volatile_auth_state(target):
            # A one-time SSO transaction: it may well be right, but it cannot
            # be published or replayed.
            pending.append("")
            continue
        admission, admission_reason = is_admission_url(target)
        if admission:
            reasons.append(f"{ADMISSION_NOTE}: {admission_reason}")
            continue
        status, final_url, html, error = await _fetch_html(
            target, timeout_seconds, country_hint, use_proxy)
        if status in (404, 410) or status >= 500:
            reasons.append(f"HTTP {status}")
            continue
        if error or not html or status >= 400:
            pending.append(target)        # timeout, WAF wall: not a verdict
            continue
        verdict, reason = _classify_login_target(
            target, final_url, html, entry_url, label)
        if verdict == "student":
            return "confirmed", publishable(target, final_url), reason
        if verdict == "wrong":
            reasons.append(reason)
        elif verdict == "unsure":
            pending.append("")            # nothing a render would add
        else:
            pending.append(target)

    to_render = next((t for t in pending if t), "")
    if to_render and render_timeout_seconds:
        html, final_url, _error = await _render_html(
            to_render, render_timeout_seconds, country_hint)
        if html:
            verdict, reason = _classify_login_target(
                to_render, final_url, html, entry_url, labels.get(to_render, ""))
            if verdict == "student":
                return "confirmed", publishable(to_render, final_url), reason
            if verdict == "wrong":
                reasons.append(reason)
                pending.remove(to_render)
            elif verdict == "unsure":
                pending[pending.index(to_render)] = ""
    if pending:
        return "unconfirmed", "", LINK_UNCONFIRMED_NOTE
    return "wrong", "", "; ".join(dict.fromkeys(reasons)) or "no usable login link"


def _stable_verified_url(published_url: str, rendered_final_url: str) -> str:
    """Prefer a reusable entry point over an ephemeral IdP transaction URL."""
    published_url = canonical_portal_url(published_url)
    rendered_final_url = canonical_portal_url(rendered_final_url)
    # Search engines sometimes publish a currently active SSO transaction
    # rather than its stable entry point.  Recover a reusable `back`/
    # `returnUrl` destination when present; otherwise at least remove volatile
    # transaction parameters before the row reaches Sheets.
    if _has_volatile_auth_state(published_url):
        parsed = urlsplit(published_url)
        pairs = parse_qsl(parsed.query, keep_blank_values=True)
        for key, value in pairs:
            if key.lower() in {
                "back", "returnurl", "redirect", "redirect_uri",
                "openid.return_to",
            }:
                if value.lower().startswith(("http://", "https://")):
                    target_allowed, _reason = portal_url_allowed(value)
                    if target_allowed and not _has_volatile_auth_state(value):
                        published_url = canonical_portal_url(value)
                        break
        else:
            stable_pairs = [
                (key, value) for key, value in pairs
                if not _is_volatile_auth_param(key)
            ]
            published_url = urlunsplit((
                parsed.scheme, parsed.netloc, parsed.path,
                urlencode(stable_pairs, doseq=True), "",
            ))
    if (_normalised_host(published_url) != _normalised_host(rendered_final_url)
            or _has_volatile_auth_state(rendered_final_url)):
        return published_url
    return rendered_final_url


async def verify_student_login_surface_rendered(
    url_path: str,
    timeout_seconds: int = 25,
    *,
    country_hint: str = "",
    official_domain: str = "",
) -> tuple[bool, str, str]:
    """Render a candidate and verify form or SSO evidence in the final DOM.

    This is deliberately the second-line verifier: Chromium is expensive, so
    callers should use the static verifier first.  Unlike that fast path it can
    see JavaScript-created forms and accepts an institutional SSO hand-off while
    returning the stable published URL rather than its temporary transaction.
    """
    allowed, reason = portal_url_allowed(url_path)
    if not allowed:
        return False, reason, url_path
    published = _as_url(url_path)
    admission, admission_reason = is_admission_url(published)
    if admission:
        return False, f"{ADMISSION_NOTE}: {admission_reason}", published
    html, final_url, render_error = await _render_html(
        published, timeout_seconds, country_hint)
    if not html:
        return False, render_error, final_url

    form_ok, form_reason = _login_form_evidence(html)
    control_ok, control_reason, control_target = _rendered_auth_control_target(
        html, final_url)
    if not form_ok and not control_ok:
        return False, f"{form_reason}; {control_reason}", final_url

    # Keep the published entry point in the evidence.  A real campus/LMS URL
    # may redirect to a generic IdP whose transaction URL no longer contains
    # the student-service signal visible in the original address.
    audience_ok, audience_reason = _student_audience_evidence(
        html, f"{published} {final_url}")
    # Being university-owned proves ownership, not audience.  An official host
    # can still be an employee directory, administrator console, admissions
    # form, or certificate utility.  Require positive student/academic/LMS
    # evidence even on the institution's own domain.
    official_student_entry = (
        _official_host(published, official_domain)
        and bool(_OFFICIAL_STUDENT_SERVICE_URL_RE.search(published))
    )
    if not audience_ok and not official_student_entry:
        return False, audience_reason, final_url
    # The audience check passes any page that mentions students, and an
    # admission page does ("Applicant Login ... for students applying to").
    admission, admission_reason = is_admission_portal(final_url, html)
    if admission:
        return False, f"{ADMISSION_NOTE}: {admission_reason}", final_url

    evidence = form_reason if form_ok else control_reason
    binding = (audience_reason if audience_ok
               else "official student-service entry URL")
    # A documentation/landing page may legitimately point to the real login.
    # When there is no credential form on the candidate itself, emit that
    # actionable target rather than the page containing the link.  Volatile
    # SAML/OIDC transactions are deliberately not published.
    stable = _stable_verified_url(published, final_url)
    link_note = ""
    if not form_ok:
        has_button, targets = _login_links(html, final_url)
        if not has_button and not targets:
            return False, NO_STUDENT_CONTROL_NOTE, final_url
        if targets:
            outcome, target_url, why = await _resolve_login_target(
                targets, entry_url=final_url,
                timeout_seconds=min(timeout_seconds, 15),
                country_hint=country_hint,
                render_timeout_seconds=timeout_seconds)
            if outcome == "confirmed":
                stable = target_url
                link_note = "; login link opened and confirmed"
            elif outcome == "wrong" and not has_button:
                return False, f"login link is not a student login: {why}", final_url
            elif outcome == "unconfirmed":
                link_note = f"; {LINK_UNCONFIRMED_NOTE}"
        if has_button and "confirmed" not in link_note:
            link_note += f"; {BUTTON_UNOPENED_NOTE}"
    return True, f"{evidence}; {binding}{link_note}; Chromium-rendered", stable


def _click_could_settle(note: str) -> bool:
    """Whether a REJECTION is the kind a click can overturn.

    Only "this page shows no way to sign in" qualifies: the way in may be
    inside a closed menu or on the Students page. A page rejected for what it
    is (an admission site, a staff console, a disallowed address) or one that
    never loaded stays rejected.
    """
    if ADMISSION_NOTE in note or "audience" in note and "off-site" not in note:
        return False
    return ("no password input" in note or NO_STUDENT_CONTROL_NOTE in note
            or "login link is not a student login" in note)


async def _click_through(url: str, country_hint: str):
    """Run the click-through, or return ``None`` when it is switched off."""
    from agent import click_through
    if not click_through.ENABLED:
        return None
    return await click_through.find_login_by_clicking(url, country_hint=country_hint)


async def verify_student_login_surface_hybrid(
    url_path: str,
    timeout_seconds: int = 10,
    *,
    country_hint: str = "",
    official_domain: str = "",
    render_timeout_seconds: int = 25,
    shared_platform_roots: Iterable[str] = (),
) -> tuple[bool, str, str]:
    """Cheapest check first: plain HTML, then Chromium, then real clicks.

    Each later step runs only when the earlier one left a question open:
    Chromium when the HTML shows no form (or a Login link that could not be
    read), and the click-through when the page offers a Login button whose form
    was never seen, or no visible way to sign in at all.
    """
    static = await verify_student_login_surface(
        url_path, timeout_seconds, country_hint=country_hint,
        shared_platform_roots=shared_platform_roots)
    if static[0]:
        best = (static[0], static[1],
                _stable_verified_url(_as_url(url_path), static[2]))
        if LINK_UNCONFIRMED_NOTE in best[1]:
            # A student page with a Login link whose target could not be read
            # as plain HTML. Chromium can render that target; keep the static
            # answer (the page itself) if the render adds nothing.
            rendered = await verify_student_login_surface_rendered(
                url_path, render_timeout_seconds, country_hint=country_hint,
                official_domain=official_domain)
            if rendered[0]:
                best = rendered
        return await _settle_by_clicking(best, country_hint)
    if static[1].startswith(ADMISSION_NOTE):
        return static
    rendered = await verify_student_login_surface_rendered(
        url_path, render_timeout_seconds, country_hint=country_hint,
        official_domain=official_domain)
    if rendered[0]:
        return await _settle_by_clicking(rendered, country_hint)
    failure = (False, f"static: {static[1]}; rendered: {rendered[1]}", rendered[2])
    # When Chromium could not load the page at all, the plain-HTML verdict is
    # the one to go by: the click-through has its own browser and routes.
    render_failed = rendered[1].startswith(("render ", "Unexpected error")) \
        or "Page.goto" in rendered[1]
    if not (_click_could_settle(rendered[1])
            or (render_failed and _click_could_settle(static[1]))):
        return failure
    found = await _click_through(_as_url(url_path), country_hint)
    if found is None or found.outcome == "none":
        if found is not None:
            failure = (False, f"{failure[1]}; click-through: {found.note}", failure[2])
        return failure
    page = found.page or canonical_portal_url(rendered[2] or _as_url(url_path))
    published = found.url if found.outcome == "one" else page
    return True, f"{found.note}; click-through", published


async def _settle_by_clicking(
    accepted: tuple[bool, str, str], country_hint: str,
) -> tuple[bool, str, str]:
    """Upgrade an accepted page whose login was never actually seen.

    The page stays accepted whatever the click finds: a button that could not
    be clicked today is not evidence against it. A click that reaches exactly
    one student login replaces the page's address with that login's.
    """
    note = accepted[1]
    if BUTTON_UNOPENED_NOTE not in note and LINK_UNCONFIRMED_NOTE not in note:
        return accepted
    found = await _click_through(accepted[2], country_hint)
    if found is None or found.outcome == "none":
        return accepted
    if found.outcome == "one":
        return True, f"{note}; {found.note}; click-through", found.url
    return True, f"{note}; {found.note}; click-through", found.page or accepted[2]


async def verify_student_login_surface(
    url_path: str,
    timeout_seconds: int = 10,
    *,
    use_proxy: bool = True,
    country_hint: str = "",
    shared_platform_roots: Iterable[str] = (),
) -> tuple[bool, str, str]:
    """Verify that a reachable URL is an HTML student-login surface.

    Returns ``(accepted, note, final_url)``.  Unlike the cheaper reachability
    guard, this GET reads a bounded HTML response and requires password +
    identifier + sign-in/authorise evidence.  PDFs, IP-literal hosts, external
    identity-provider redirects, and non-HTML responses are rejected.

    A redirect to a sibling host on the same registrable root (a portal
    load-balancing across nodes) is followed; one to another organisation is
    not.  ``shared_platform_roots`` names multi-tenant vendor roots, where
    sibling hosts are different institutions and the redirect is still
    rejected.  After an accepted cross-host redirect the returned URL is the
    requested entry point, not the node that happened to answer.
    """
    allowed, reason = portal_url_allowed(url_path)
    if not allowed:
        return False, reason, url_path

    url = _as_url(url_path)
    admission, admission_reason = is_admission_url(url)
    if admission:
        return False, f"{ADMISSION_NOTE}: {admission_reason}", url
    proxy_candidates = _proxy_urls(url, country_hint) if use_proxy else []
    routes = proxy_candidates or [("direct", None)]
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    headers = {"User-Agent": USER_AGENT}
    body = b""
    content_type = ""
    final_url = url
    host_changed = False
    async with aiohttp.ClientSession(timeout=timeout) as sess:
        for index, (provider, proxy_url) in enumerate(routes):
            try:
                async with sess.get(url, allow_redirects=True, headers=headers,
                                    proxy=proxy_url, ssl=_SSL_CONTEXT) as resp:
                    final_url = str(resp.url)
                    final_allowed, final_reason = portal_url_allowed(final_url)
                    if not final_allowed:
                        return False, f"final URL: {final_reason}", final_url
                    host_changed = (_normalised_host(final_url)
                                    != _normalised_host(url))
                    if host_changed and not _same_organisation_redirect(
                            url, final_url, shared_platform_roots):
                        return False, "redirected to another host", final_url
                    content_type = (resp.headers.get("Content-Type") or "").lower()
                    if "pdf" in content_type:
                        return False, "PDF response", final_url
                    if not (200 <= resp.status < 400):
                        return False, f"HTTP {resp.status}; login form unavailable", final_url
                    body = await resp.content.read(_MAX_LOGIN_PAGE_BYTES)
                break
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                if (index + 1 < len(routes)
                        and _proxy_transport_failure(exc, proxy_url)):
                    logger.info(
                        "guardrails: login fetch %s failed via %s; retrying via %s",
                        url, provider, routes[index + 1][0],
                    )
                    continue
                return False, type(exc).__name__, url

    if body.lstrip().startswith(b"%PDF"):
        return False, "PDF response", final_url
    # Some old portals omit Content-Type, so accept HTML-looking content rather
    # than rejecting it solely for a broken header.
    text = body.decode("utf-8", errors="ignore")
    if "html" not in content_type and "<html" not in text.lower() and "<form" not in text.lower():
        return False, "not an HTML login page", final_url
    accepted, evidence = _login_form_evidence(text)
    control_ok, control_evidence, control_target = _rendered_auth_control_target(
        text, final_url)
    if not accepted and not control_ok:
        return False, f"{evidence}; {control_evidence}", final_url
    # After a same-organisation hop the requested entry point is still part of
    # the evidence (as in the rendered verifier): the node that answered may
    # have a name that says nothing about the service.
    audience_ok, audience_reason = _student_audience_evidence(
        text, f"{url} {final_url}" if host_changed else final_url)
    if not audience_ok:
        return False, audience_reason, final_url
    admission, admission_reason = is_admission_portal(final_url, text)
    if admission:
        return False, f"{ADMISSION_NOTE}: {admission_reason}", final_url
    # Publish the stable entry point rather than one node of a rotating pool.
    stable_url = (_stable_verified_url(url, final_url) if host_changed
                  else final_url)
    link_note = ""
    if not accepted:
        # The page has no form of its own, only a Login link. Open the link:
        # publish it when it is the student login, publish this page when the
        # link cannot be judged, and reject the page when its only way in
        # leads somewhere else.
        has_button, targets = _login_links(text, final_url)
        if not has_button and not targets:
            return False, NO_STUDENT_CONTROL_NOTE, final_url
        if targets:
            outcome, target_url, why = await _resolve_login_target(
                targets, entry_url=final_url, timeout_seconds=timeout_seconds,
                country_hint=country_hint, use_proxy=use_proxy)
            if outcome == "confirmed":
                stable_url = target_url
                link_note = "; login link opened and confirmed"
            elif outcome == "wrong" and not has_button:
                return False, f"login link is not a student login: {why}", final_url
            elif outcome == "unconfirmed":
                link_note = f"; {LINK_UNCONFIRMED_NOTE}"
        if has_button and "confirmed" not in link_note:
            link_note += f"; {BUTTON_UNOPENED_NOTE}"
    auth_evidence = evidence if accepted else control_evidence
    return True, f"{auth_evidence}; {audience_reason}{link_note}", stable_url


def _proxy_url(url: str, country_hint: str = "") -> str | None:
    """Residential proxy URL with credentials inline, or None for direct.

    aiohttp takes the proxy as a single URL string, so the username/password
    from `agent.proxy` are embedded rather than passed as BasicAuth.
    """
    candidates = _proxy_urls(url, country_hint)
    return candidates[0][1] if candidates else None


def _proxy_urls(url: str, country_hint: str = "") -> list[tuple[str, str]]:
    """Ordered ``(provider, aiohttp proxy URL)`` candidates."""
    output: list[tuple[str, str]] = []
    for provider, cfg in _proxy.playwright_proxy_candidates(country_hint, url):
        server = cfg["server"].replace("http://", "").replace("https://", "")
        user = quote(cfg["username"], safe="")
        password = quote(cfg["password"], safe="")
        output.append((provider, f"http://{user}:{password}@{server}"))
    return output


def _proxy_transport_failure(exc: Exception, proxy_url: str | None) -> bool:
    """Whether an exception warrants retrying through the next provider."""
    if not proxy_url:
        return False
    transport_types = (
        asyncio.TimeoutError,
        aiohttp.ClientHttpProxyError,
        aiohttp.ClientProxyConnectionError,
        aiohttp.ClientConnectorError,
    )
    return isinstance(exc, transport_types) or _proxy.is_proxy_failure(exc)


def _classify(status: int) -> bool:
    if 200 <= status < 400:
        return True
    if status in WAF_ALIVE_CODES:
        return True
    return False


async def verify_portal_endpoint_final(
    url_path: str,
    timeout_seconds: int = 5,
    *,
    session: aiohttp.ClientSession | None = None,
    use_proxy: bool = True,
    country_hint: str = "",
    retry_timeout_seconds: int | None = None,
) -> tuple[bool, int, str, str]:
    """(alive, http_status, note, final_url) for `url_path`.

    `final_url` is where the request LANDED after redirects, which is not
    always where it started: many institutional portals are published as http
    and redirect to https. Recording the landing URL both upgrades those for
    free and is the only way to apply an https policy correctly — V2 checked
    `resp.url`, not the input, for exactly that reason.

    Same logic as `verify_portal_endpoint`, but keeps the status code so callers
    that log or persist it do not have to probe the endpoint a second time.

    Args:
        url_path: absolute URL (scheme added when absent).
        timeout_seconds: total per-attempt budget.
        session: reuse an open `ClientSession` — batch callers should, since a
            fresh session per URL pays TCP + TLS setup every time.
        use_proxy: route via the residential proxy chosen by ccTLD.
        country_hint: org country, used when the URL carries no ccTLD.
        retry_timeout_seconds: when set, a TIMEOUT (only) is retried once with
            this longer budget. Off by default to keep the contract exact — but
            see the note in the module docs: slow is not the same as dead, and
            V2 re-checks at a longer timeout flipped 26 of 52 "unreachable"
            portals to working.

    Never raises: any aiohttp or network anomaly resolves to False so one bad
    URL cannot break the async processing loop.
    """
    try:
        url = _as_url(url_path)
    except ValueError as exc:
        logger.warning("guardrails: %s", exc)
        return False, 0, "bad url", url_path

    proxy_candidates = _proxy_urls(url, country_hint) if use_proxy else []
    routes = proxy_candidates or [("direct", None)]
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    headers = {"User-Agent": USER_AGENT}

    async def _attempt(sess: aiohttp.ClientSession,
                       budget: aiohttp.ClientTimeout,
                       proxy_url: str | None) -> tuple[bool, int, str, str]:
        # 1) HEAD — cheapest possible probe.
        try:
            async with sess.head(url, allow_redirects=True, timeout=budget,
                                 headers=headers, proxy=proxy_url,
                                 ssl=_SSL_CONTEXT) as resp:
                status, landed = resp.status, str(resp.url)
            if status != METHOD_NOT_ALLOWED:
                alive = _classify(status)
                logger.debug("guardrails: HEAD %s -> %s (alive=%s)", url, status, alive)
                return alive, status, "head", landed
        except asyncio.TimeoutError:
            raise
        except aiohttp.ClientError as exc:
            # Some servers reject HEAD at the connection layer rather than with
            # a 405, so a client error is not yet proof of death — fall through
            # to GET and let that decide.
            logger.debug("guardrails: HEAD %s failed (%s) — trying GET",
                         url, type(exc).__name__)

        # 2) GET — 405, or a HEAD the server mishandled.
        async with sess.get(url, allow_redirects=True, timeout=budget,
                            headers=headers, proxy=proxy_url,
                            ssl=_SSL_CONTEXT) as resp:
            status, landed = resp.status, str(resp.url)
            await resp.release()
        alive = _classify(status)
        logger.debug("guardrails: GET %s -> %s (alive=%s)", url, status, alive)
        return alive, status, "get", landed

    own_session = session is None
    sess = session or aiohttp.ClientSession(timeout=timeout)
    try:
        for index, (provider, proxy_url) in enumerate(routes):
            failure: Exception | None = None
            try:
                return await _attempt(sess, timeout, proxy_url)
            except asyncio.TimeoutError as exc:
                failure = exc
                if retry_timeout_seconds:
                    logger.info("guardrails: %s timed out at %ss via %s — retrying at %ss",
                                url, timeout_seconds, provider, retry_timeout_seconds)
                    try:
                        return await _attempt(
                            sess, aiohttp.ClientTimeout(total=retry_timeout_seconds),
                            proxy_url,
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception as retry_exc:  # noqa: BLE001
                        failure = retry_exc
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                failure = exc

            if (failure is not None and index + 1 < len(routes)
                    and _proxy_transport_failure(failure, proxy_url)):
                logger.info(
                    "guardrails: %s proxy transport failed via %s (%s); retrying via %s",
                    url, provider, type(failure).__name__, routes[index + 1][0],
                )
                continue
            if isinstance(failure, asyncio.TimeoutError):
                logger.info("guardrails: %s timed out after %ss via %s — treated as dead",
                            url, timeout_seconds, provider)
                return False, 0, "timeout", url
            if isinstance(failure, aiohttp.ClientError):
                logger.info("guardrails: %s unreachable via %s (%s)",
                            url, provider, type(failure).__name__)
                return False, 0, type(failure).__name__, url
            logger.warning("guardrails: %s unexpected %s via %s: %s",
                           url, type(failure).__name__, provider, str(failure)[:160])
            return False, 0, type(failure).__name__, url
        return False, 0, "no route", url
    finally:
        if own_session:
            await sess.close()


async def verify_portal_endpoint_detailed(
    url_path: str,
    timeout_seconds: int = 5,
    **kwargs: Any,
) -> tuple[bool, int, str]:
    """(alive, http_status, note) — the pre-existing 3-tuple contract.

    Kept so callers that do not care where the request landed are unaffected.
    New code should prefer `verify_portal_endpoint_final`.
    """
    alive, status, note, _final = await verify_portal_endpoint_final(
        url_path, timeout_seconds, **kwargs)
    return alive, status, note


# --------------------------------------------------------------------------- #
#  Transport policy — is a plain-http login acceptable?                        #
# --------------------------------------------------------------------------- #
#: Reject portals whose FINAL url is not https. Default on.
#:
#: V2 enforced this in two places (`magic.py` accept filter, and discovery's
#: hard-gate on `resp.url`) and the V3 rewrite dropped it — so V3 shipped
#: plain-http logins for months. 57 of 287 portals in domain_history are http,
#: and a sample showed roughly half of those stay http after redirects.
#:
#: The check is on the FINAL url, never the input: plenty of institutions
#: publish http and redirect to https, and rejecting those would throw away
#: perfectly secure portals over a published link's spelling.
#:
#: It is a real trade, not a free win. Genuinely http-only student portals
#: exist and are legitimate — Universidad Pedagógica de Durango publishes one
#: from its own homepage. Turning this on drops them. Callers are expected to
#: REPORT what was dropped rather than let it vanish, because a silent filter
#: is indistinguishable from "this university has no portal".
REQUIRE_HTTPS = os.getenv("GENIE_REQUIRE_HTTPS", "1").strip().lower() \
    not in ("0", "false", "no", "off")


def transport_ok(final_url: str) -> bool:
    """True when this URL satisfies the transport policy.

    Always True when REQUIRE_HTTPS is off, so the flag is the only switch.
    """
    if not REQUIRE_HTTPS:
        return True
    return (final_url or "").strip().lower().startswith("https://")


async def verify_many(
    urls: Sequence[str],
    timeout_seconds: int = 5,
    *,
    concurrency: int = 20,
    use_proxy: bool = True,
    country_hint: str = "",
    retry_timeout_seconds: int | None = None,
) -> dict[str, bool]:
    """Verify a batch through ONE session, bounded by `concurrency`."""
    sem = asyncio.Semaphore(max(1, concurrency))
    results: dict[str, bool] = {}
    connector = aiohttp.TCPConnector(limit=max(1, concurrency), ssl=_SSL_CONTEXT)
    async with aiohttp.ClientSession(connector=connector) as sess:
        async def one(u: str) -> None:
            async with sem:
                results[u] = await verify_portal_endpoint(
                    u, timeout_seconds, session=sess, use_proxy=use_proxy,
                    country_hint=country_hint,
                    retry_timeout_seconds=retry_timeout_seconds,
                )
        await asyncio.gather(*(one(u) for u in urls), return_exceptions=True)
    return results


async def verify_portal_endpoint(
    url_path: str,
    timeout_seconds: int = 5,
    *,
    session: aiohttp.ClientSession | None = None,
    use_proxy: bool = True,
    country_hint: str = "",
    retry_timeout_seconds: int | None = None,
) -> bool:
    """True if `url_path` is a live endpoint, False if dead or unreachable.

    The Layer 3 contract: 2xx/3xx alive; 401/403/429 alive (a WAF answering is
    still a server answering); 405 retried with GET; 404/5xx/timeout/network
    dead. Never raises — one bad URL must not break the async processing loop.
    """
    alive, _status, _note = await verify_portal_endpoint_detailed(
        url_path, timeout_seconds, session=session, use_proxy=use_proxy,
        country_hint=country_hint, retry_timeout_seconds=retry_timeout_seconds,
    )
    return alive

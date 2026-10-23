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
from typing import Any, Iterable, Sequence
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import aiohttp
from bs4 import BeautifulSoup

from agent import proxy as _proxy

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
    r"/online_guide(?:/|$)|"
    r"/services?/portal/(?:faq|student|member)(?:/|$)|"
    r"/(?:okomariguide|mfatype(?:-en)?)\.html(?:/|$)|"
    r"/(?:first|1st)[_-]?login(?:\.[a-z0-9]+)?(?:/|$)|"
    r"(?:^|/)(?:privacy|privacy-policy|policy|policies|terms|terms-of-use)"
    r"(?:\.[a-z0-9]+)?(?:/|$)",
    re.I,
)
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
_ACADEMIC_PLATFORM_RE = re.compile(
    r"(?:moodle|canvas(?:lms)?|blackboard|brightspace|d2l|chamilo|sakailms|"
    r"(?:^|[./_-])lms(?:[./_-]|$)|koha|ezproxy|library\s+catalog|opac)", re.I)
_OFFICIAL_STUDENT_SERVICE_URL_RE = re.compile(
    r"(?:^|[./_-])(?:student|campus|portal|lms|moodle|canvas|library|"
    r"my)(?:[./_-]|$)", re.I)
_MAX_LOGIN_PAGE_BYTES = 512 * 1024
_VOLATILE_AUTH_PARAMS = frozenset({
    "sessid", "session", "sessionid", "samlrequest", "samlresponse",
    "relaystate", "signature", "sigalg", "state", "nonce", "code",
})
_render_sem: asyncio.Semaphore | None = None


def _get_render_sem() -> asyncio.Semaphore:
    global _render_sem
    if _render_sem is None:
        _render_sem = asyncio.Semaphore(max(
            1, int(os.getenv("GENIE_VERIFY_BROWSER_CONCURRENCY", "2"))))
    return _render_sem

#: aiohttp validates certificates by default; university TLS often fails that
#: check while the site itself is fine. We are testing reachability, not trust.
_INSECURE_SSL = ssl.create_default_context()
_INSECURE_SSL.check_hostname = False
_INSECURE_SSL.verify_mode = ssl.CERT_NONE


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
    if _host_is_ip_literal(normalised):
        return False, "literal IP host"
    if _NON_PORTAL_UTILITY_HOST_RE.search(parsed.hostname or ""):
        return False, "non-portal directory utility host"
    if _DOCUMENT_PATH_RE.search(parsed.path or ""):
        return False, "document URL"
    if _NON_PORTAL_CONTENT_PATH_RE.search(parsed.path or ""):
        return False, "informational/content route"
    return True, "ok"


def canonical_portal_url(url: str) -> str:
    """Return a reusable output URL, removing presentation-only variants."""
    cleaned = _as_url(url)
    parsed = urlsplit(cleaned)
    query = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        lower = key.lower()
        if lower.startswith("utm_") or lower in {"lang", "changelangcd"}:
            continue
        query.append((key, value))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path,
                       urlencode(query, doseq=True), ""))


def portal_dedupe_key(url: str) -> str:
    """Canonical key used immediately before writing verified portal rows."""
    parsed = urlsplit(canonical_portal_url(url))
    path = (parsed.path or "/").rstrip("/") or "/"
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path,
                       parsed.query, ""))


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
        if _LOGIN_WORD_RE.search(text) or kind == "submit":
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
    Known LMS and academic-library products are accepted as student services
    when their sparse login screen has no audience copy.
    """
    soup = BeautifulSoup(html or "", "html.parser")
    page_text = soup.get_text(" ", strip=True)
    form_descriptors = " ".join(
        " ".join(str(field.get(k) or "") for k in
                 ("name", "id", "placeholder", "autocomplete", "aria-label"))
        for field in soup.find_all(["input", "textarea", "button"])
    )
    evidence = f"{url}\n{page_text}\n{form_descriptors}"
    has_student_signal = bool(_STUDENT_AUDIENCE_RE.search(evidence))
    has_non_student_signal = bool(_NON_STUDENT_AUDIENCE_RE.search(evidence))
    if has_non_student_signal and not has_student_signal:
        return False, "non-student audience only"
    if has_student_signal:
        return True, "student/academic audience evidence"
    if _ACADEMIC_PLATFORM_RE.search(evidence):
        return True, "known LMS or academic-library platform"
    return False, "no student or academic audience evidence"


def _official_host(url: str, official_domain: str) -> bool:
    host = _normalised_host(url)
    root = _normalised_host(official_domain)
    return bool(host and root and (host == root or host.endswith("." + root)))


def _has_volatile_auth_state(url: str) -> bool:
    try:
        return any(k.lower() in _VOLATILE_AUTH_PARAMS
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
        if not _LOGIN_WORD_RE.search(descriptor):
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
            if key.lower() in {"back", "returnurl", "redirect", "redirect_uri"}:
                if value.lower().startswith(("http://", "https://")):
                    target_allowed, _reason = portal_url_allowed(value)
                    if target_allowed and not _has_volatile_auth_state(value):
                        published_url = canonical_portal_url(value)
                        break
        else:
            stable_pairs = [
                (key, value) for key, value in pairs
                if key.lower() not in _VOLATILE_AUTH_PARAMS
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
    try:
        from crawl4ai import AsyncWebCrawler
        from agent.crawler import (_browser_config, _run_config, WAIT_FOR_JS,
                                   proxy_for)
        proxy_cfg = proxy_for(published, country_hint)
        async with _get_render_sem():
            async with AsyncWebCrawler(config=_browser_config(proxy_cfg)) as crawler:
                result = await crawler.arun(
                    url=published,
                    config=_run_config(timeout_seconds * 1000, WAIT_FOR_JS),
                )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        return False, f"render {type(exc).__name__}", published

    html = getattr(result, "html", "") or ""
    final_url = (getattr(result, "redirected_url", None)
                 or getattr(result, "url", None) or published)
    if not html:
        error = str(getattr(result, "error_message", "") or "render returned no HTML")
        return False, error[:180], final_url

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

    evidence = form_reason if form_ok else control_reason
    binding = (audience_reason if audience_ok
               else "official student-service entry URL")
    # A documentation/landing page may legitimately point to the real login.
    # When there is no credential form on the candidate itself, emit that
    # actionable target rather than the page containing the link.  Volatile
    # SAML/OIDC transactions are deliberately not published.
    stable = _stable_verified_url(published, final_url)
    if not form_ok and control_target:
        target_allowed, _target_reason = portal_url_allowed(control_target)
        if target_allowed and not _has_volatile_auth_state(control_target):
            stable = canonical_portal_url(control_target)
    return True, f"{evidence}; {binding}; Chromium-rendered", stable


async def verify_student_login_surface_hybrid(
    url_path: str,
    timeout_seconds: int = 10,
    *,
    country_hint: str = "",
    official_domain: str = "",
    render_timeout_seconds: int = 25,
) -> tuple[bool, str, str]:
    """Fast static verification followed by Chromium on any ambiguity."""
    static = await verify_student_login_surface(
        url_path, timeout_seconds, country_hint=country_hint)
    if static[0]:
        return static[0], static[1], _stable_verified_url(_as_url(url_path), static[2])
    rendered = await verify_student_login_surface_rendered(
        url_path, render_timeout_seconds, country_hint=country_hint,
        official_domain=official_domain)
    if rendered[0]:
        return rendered
    return False, f"static: {static[1]}; rendered: {rendered[1]}", rendered[2]


async def verify_student_login_surface(
    url_path: str,
    timeout_seconds: int = 10,
    *,
    use_proxy: bool = True,
    country_hint: str = "",
) -> tuple[bool, str, str]:
    """Verify that a reachable URL is an HTML student-login surface.

    Returns ``(accepted, note, final_url)``.  Unlike the cheaper reachability
    guard, this GET reads a bounded HTML response and requires password +
    identifier + sign-in/authorise evidence.  PDFs, IP-literal hosts, external
    identity-provider redirects, and non-HTML responses are rejected.
    """
    allowed, reason = portal_url_allowed(url_path)
    if not allowed:
        return False, reason, url_path

    url = _as_url(url_path)
    proxy = _proxy_url(url, country_hint) if use_proxy else None
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    headers = {"User-Agent": USER_AGENT}
    try:
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.get(url, allow_redirects=True, headers=headers,
                                proxy=proxy, ssl=_INSECURE_SSL) as resp:
                final_url = str(resp.url)
                final_allowed, final_reason = portal_url_allowed(final_url)
                if not final_allowed:
                    return False, f"final URL: {final_reason}", final_url
                if _normalised_host(final_url) != _normalised_host(url):
                    return False, "redirected to another host", final_url
                content_type = (resp.headers.get("Content-Type") or "").lower()
                if "pdf" in content_type:
                    return False, "PDF response", final_url
                if not (200 <= resp.status < 400):
                    return False, f"HTTP {resp.status}; login form unavailable", final_url
                body = await resp.content.read(_MAX_LOGIN_PAGE_BYTES)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - failure should only reject this candidate
        return False, type(exc).__name__, url

    if body.lstrip().startswith(b"%PDF"):
        return False, "PDF response", final_url
    # Some old portals omit Content-Type, so accept HTML-looking content rather
    # than rejecting it solely for a broken header.
    text = body.decode("utf-8", errors="ignore")
    if "html" not in content_type and "<html" not in text.lower() and "<form" not in text.lower():
        return False, "not an HTML login page", final_url
    accepted, evidence = _login_form_evidence(text)
    if not accepted:
        return False, evidence, final_url
    audience_ok, audience_reason = _student_audience_evidence(text, final_url)
    if not audience_ok:
        return False, audience_reason, final_url
    return True, f"{evidence}; {audience_reason}", final_url


def _proxy_url(url: str, country_hint: str = "") -> str | None:
    """Residential proxy URL with credentials inline, or None for direct.

    aiohttp takes the proxy as a single URL string, so the username/password
    from `agent.proxy` are embedded rather than passed as BasicAuth.
    """
    cfg = _proxy.playwright_proxy(country_hint, url)
    if not cfg:
        return None
    server = cfg["server"].replace("http://", "").replace("https://", "")
    return f"http://{cfg['username']}:{cfg['password']}@{server}"


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

    proxy = _proxy_url(url, country_hint) if use_proxy else None
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    headers = {"User-Agent": USER_AGENT}

    async def _attempt(sess: aiohttp.ClientSession,
                       budget: aiohttp.ClientTimeout) -> tuple[bool, int, str, str]:
        # 1) HEAD — cheapest possible probe.
        try:
            async with sess.head(url, allow_redirects=True, timeout=budget,
                                 headers=headers, proxy=proxy,
                                 ssl=_INSECURE_SSL) as resp:
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
                            headers=headers, proxy=proxy,
                            ssl=_INSECURE_SSL) as resp:
            status, landed = resp.status, str(resp.url)
            await resp.release()
        alive = _classify(status)
        logger.debug("guardrails: GET %s -> %s (alive=%s)", url, status, alive)
        return alive, status, "get", landed

    own_session = session is None
    sess = session or aiohttp.ClientSession(timeout=timeout)
    try:
        try:
            return await _attempt(sess, timeout)
        except asyncio.TimeoutError:
            if retry_timeout_seconds:
                logger.info("guardrails: %s timed out at %ss — retrying at %ss",
                            url, timeout_seconds, retry_timeout_seconds)
                try:
                    return await _attempt(
                        sess, aiohttp.ClientTimeout(total=retry_timeout_seconds))
                except Exception as exc:  # noqa: BLE001
                    logger.info("guardrails: %s dead on retry (%s)", url, type(exc).__name__)
                    return False, 0, f"retry {type(exc).__name__}", url
            logger.info("guardrails: %s timed out after %ss — treated as dead",
                        url, timeout_seconds)
            return False, 0, "timeout", url
        except aiohttp.ClientError as exc:
            logger.info("guardrails: %s unreachable (%s)", url, type(exc).__name__)
            return False, 0, type(exc).__name__, url
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — never break the caller's loop
            logger.warning("guardrails: %s unexpected %s: %s",
                           url, type(exc).__name__, str(exc)[:160])
            return False, 0, type(exc).__name__, url
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
    connector = aiohttp.TCPConnector(limit=max(1, concurrency), ssl=_INSECURE_SSL)
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

"""Genie-V3 · admission-portal detection.

An admission portal has a perfectly real login form, but it serves applicants,
not enrolled students, so the form check and the audience check both let it
through: "Applicant Login" on a page that also says "student" counts as a
student page. This module is the check that asks the missing question.

It applies the same four layers as the V2 detector
(`agent.legacy.stages.discovery_rules.is_admission_portal`) and reads the same
signal lists from `agent.config`, so the two engines agree on what an admission
page looks like:

    Layer 1  URL path / host tokens            (no fetch needed)
    Layer 4  known central admission platforms (no fetch needed)
    Layer 3  <title> / <h1> phrases
    Layer 2  visible-text signals: one strong, or several moderate

One deliberate difference from V2 in Layer 2: a strong phrase does not reject a
page that also carries enrolled-student wording, unless the phrase names the
applicant as the person signing in ("Applicant Login"). Run against 168
hand-verified student portals, the V2 rule rejected four real ones for a single
admission link beside the login form.

The V2 function is not imported because its module builds an HTTP session and
pulls in the whole V2 discovery stack at import time.

The V3-only additions below are kept HERE rather than appended to the shared
config lists: those lists also drive the V2 engine that runs in production, and
widening them would change its output.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from agent.config import (KNOWN_ADMISSION_PLATFORMS, MODERATE_ADMISSION_SIGNALS,
                          MOODLE_LOGIN_COUNTER_SIGNALS, STRONG_ADMISSION_SIGNALS,
                          STUDENT_LOGIN_COUNTER_SIGNALS, TITLE_ADMISSION_PHRASES,
                          URL_ADMISSION_HOST_KEYWORDS, URL_ADMISSION_PATH_KEYWORDS,
                          URL_ADMISSION_REGISTER_EXEMPT_TOKENS)

#: Admission words that appear glued to other text in a path
#: (`/lnipeadmission/index`, `/post-admisssion/welcome.aspx`,
#: `/applicant-login`), plus the Portuguese, Spanish and Indonesian terms for
#: an entrance exam or an applicant sign-up.
_V3_PATH_SUBSTRINGS: tuple[str, ...] = (
    "admission", "admisssion", "applicant",
    "/vestibular", "/processo-seletivo", "/processoseletivo",
    "/inscricao", "/inscricoes", "/inscripcion", "/preinscripcion",
    "/candidato", "/aspirante", "/pendaftaran",
)
#: `entrance.<university>` hosts serve entrance-test application forms.
_V3_HOST_SUBSTRINGS: tuple[str, ...] = ("entrance",)
#: Samarth gives each institution a separate admission tenant whose label ends
#: in `adm` (`sgbauadm.samarth.edu.in`); the student tenant has no suffix.
_SAMARTH_ADMISSION_HOST_RE = re.compile(
    r"(?:^|\.)[a-z0-9-]+adm\.samarth\.(?:edu|ac)\.in$", re.I)
#: A title naming an intake cycle — "(Admissions 2024-28)" — is an admission
#: portal even when it never uses the word "portal".
_ADMISSION_CYCLE_TITLE_RE = re.compile(r"\badmissions?\s*\(?\s*20\d\d\b", re.I)
#: Phrases that name the APPLICANT as the person signing in. Unlike a stray
#: "Apply for admission" link, these describe the login itself, so they decide
#: the page whatever else it says.
_APPLICANT_LOGIN_SIGNALS: tuple[str, ...] = (
    "applicant login", "applicant log in", "applicant sign in",
    "applicant portal", "new applicant registration",
    "आवेदक लॉगिन",
    "portal do candidato", "área do candidato", "area do candidato",
    "portal del aspirante",
)
#: Wording only an enrolled student's portal uses. A real student portal often
#: carries one admission link beside its login ("New student registration",
#: "Apply for admission" on the MDU and Samarth student portals), so a strong
#: admission phrase decides the page only when none of these is present. This
#: is narrower than `STUDENT_LOGIN_COUNTER_SIGNALS`: "forgot password",
#: "result" and "library" appear on admission portals too.
_ENROLLED_STUDENT_SIGNALS: tuple[str, ...] = (
    "enrollment number", "enrolment number", "roll number", "university roll no",
    "student id", "student code", "registration number",
    "already registered", "existing student", "old student",
    "exam form", "attendance", "timetable", "time table",
    "fee receipt", "admit card download", "back paper", "backlog",
)


def _url_token(parsed) -> str:
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").lower()
    exempt = any(t in path for t in URL_ADMISSION_REGISTER_EXEMPT_TOKENS)
    for kw in URL_ADMISSION_PATH_KEYWORDS:
        if kw in path:
            # An enrolled student's own "registration" page (course or exam
            # registration behind a login) is not an applicant sign-up.
            if kw in ("/register", "/registration") and exempt:
                continue
            return f"path={kw!r}"
    for kw in _V3_PATH_SUBSTRINGS:
        if kw in path:
            return f"path={kw!r}"
    for kw in (*URL_ADMISSION_HOST_KEYWORDS, *_V3_HOST_SUBSTRINGS):
        if kw in host:
            return f"host={kw!r}"
    if _SAMARTH_ADMISSION_HOST_RE.search(host):
        return "host=Samarth admission tenant"
    return ""


def _known_platform(host: str) -> str:
    host = (host or "").lower().lstrip(".")
    for entry in KNOWN_ADMISSION_PLATFORMS:
        if host == entry or host.endswith("." + entry):
            return entry
    return ""


def is_admission_url(url: str) -> tuple[bool, str]:
    """URL-only verdict (Layers 1 and 4). Safe to call before any fetch."""
    try:
        parsed = urlsplit(url or "")
    except ValueError:
        return False, ""
    token = _url_token(parsed)
    if token:
        return True, f"URL admission token {token}"
    platform = _known_platform(parsed.hostname or "")
    if platform:
        return True, f"known admission platform host {platform!r}"
    return False, ""


def is_admission_portal(url: str, html: str | None = None) -> tuple[bool, str]:
    """Whether ``(url, html)`` is an applicant-facing admission portal.

    Returns ``(is_admission, reason)``. With ``html=None`` only the URL layers
    run. ``url`` may hold several space-separated URLs (the published entry and
    the address it redirected to); any one of them matching is enough.
    """
    for candidate in (url or "").split():
        hit, reason = is_admission_url(candidate)
        if hit:
            return True, reason
    if not html:
        return False, ""

    # Stock Moodle login pages say "Create new account" and "Is this your first
    # time here?", which read like applicant sign-up. Any Moodle marker in the
    # raw HTML (form actions included) settles it as a student login.
    lowered = html.lower()
    if any(marker in lowered for marker in MOODLE_LOGIN_COUNTER_SIGNALS):
        return False, ""

    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    headings = " | ".join(h.get_text(" ", strip=True) for h in soup.find_all("h1")[:5])
    title_h1 = f"{title} | {headings}".lower()
    existing_login = "login" in title_h1 and "existing" in title_h1
    if not existing_login:
        for phrase in TITLE_ADMISSION_PHRASES:
            if phrase in title_h1:
                return True, f"title/h1 admission phrase {phrase!r}"
        cycle = _ADMISSION_CYCLE_TITLE_RE.search(title_h1)
        if cycle:
            return True, f"title/h1 names an admission cycle {cycle.group(0)!r}"

    text = soup.get_text(" ", strip=True).lower()
    if not text:
        return False, ""
    for signal in _APPLICANT_LOGIN_SIGNALS:
        if signal in text:
            return True, f"strong admission signal {signal!r}"
    enrolled = any(signal in text for signal in _ENROLLED_STUDENT_SIGNALS)
    if not enrolled:
        for signal in STRONG_ADMISSION_SIGNALS:
            if signal in text:
                return True, f"strong admission signal {signal!r}"
    moderate = [s for s in MODERATE_ADMISSION_SIGNALS if s in text]
    counter = [s for s in STUDENT_LOGIN_COUNTER_SIGNALS if s in text]
    if len(moderate) >= 2 and not counter:
        return True, (f"moderate admission signals x{len(moderate)} "
                      f"(no student counter-signals): {moderate[:3]}")
    if len(moderate) >= 4:
        return True, (f"high admission signal count x{len(moderate)} "
                      f"(despite x{len(counter)} counter-signals): {moderate[:3]}")
    return False, ""

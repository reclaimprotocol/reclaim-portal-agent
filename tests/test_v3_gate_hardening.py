"""Checks for the rules added after the 267-org SheerID run.

Every URL below is a real one that the V3 gate let through (or wrongly merged,
or wrongly kept apart) during that run; the page snippets are trimmed copies of
what those URLs served.
"""

import asyncio
from types import SimpleNamespace

import pytest

from agent import v3_orchestrator as orch
from agent.admission import is_admission_portal, is_admission_url
from agent.guardrails import (
    ADMISSION_NOTE,
    _official_host,
    canonical_portal_url,
    portal_dedupe_key,
    portal_url_allowed,
    verify_student_login_surface,
)

# --------------------------------------------------------------------------- #
#  portal_url_allowed                                                          #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("url", [
    "https://support@magadhonline.in",      # support address typed as a URL
    "support@magadhonline.in",
    "https://mviraj215@gmail.com",
    "https://user:secret@portal.example.edu/login",
])
def test_rejects_an_email_address_or_credentials_in_the_url(url: str) -> None:
    allowed, reason = portal_url_allowed(url)
    assert allowed is False
    assert reason == "e-mail address or credentials in URL"


@pytest.mark.parametrize("url", [
    "https://www.lawschoollagos.org/wp-login.php",
    "https://imsciences.edu.pk/wp-login.php?action=register",
    "https://sat5.dpuerp.in/login_up.php?success_redirect_url=%2F",   # Plesk
    "https://example.edu/administrator/",
    "https://files.example.edu/nextcloud/index.php/login",
    "https://example.edu/roundcube/",
])
def test_rejects_cms_hosting_mail_and_storage_logins(url: str) -> None:
    allowed, reason = portal_url_allowed(url)
    assert allowed is False
    assert reason == "CMS, hosting-panel, mail or file-storage login"


@pytest.mark.parametrize("url", [
    "https://webmail.example.edu/",
    "https://placement.example.ac.in/login",
    "https://alumni.example.edu/login",
    "https://grievance.example.ac.in/login.aspx",
])
def test_rejects_non_portal_service_hosts(url: str) -> None:
    assert portal_url_allowed(url)[0] is False


@pytest.mark.parametrize("url", [
    "https://www.telanganauniversity.ac.in/recruiter_login.php",
    "https://online.ietlucknow.ac.in/STMSAPP/Init/staff/login.aspx",
    "https://www.jimsindia.org/placement-login.aspx",
    "https://beu-bih.ac.in/admin",
    "https://uncuvirtual.uncuyo.edu.ar/admin/login",
    "https://example.edu/faculty-portal/",
    "https://example.edu/parents/login",
    "https://example.edu/hr/login",
])
def test_rejects_paths_that_name_another_audience(url: str) -> None:
    allowed, reason = portal_url_allowed(url)
    assert allowed is False
    assert reason == "non-student audience path"


@pytest.mark.parametrize("url", [
    "https://www.telanganauniversity.ac.in/student_login.php",
    "https://gnsu.org/ERP/Student/StudentLogin.aspx",
    "https://uniamerica.br/alunos/",
    "https://example.edu/student-staff/login",          # shared sign-in
    "https://example.edu/transparent/login",            # "parent" inside a word
    "https://example.edu/shr/login",                    # "hr" inside a word
    "https://moodle.example.edu/admin/tool/mobile/launch.php",
    "https://library.example.edu/login",                # libraries stay valid
    "https://cloud.hrituniversity.edu.in/login",
    "https://accounts.descomplica.com.br/",             # a Google/Microsoft-style
    "https://login.microsoftonline.com/common/oauth2",  # IdP is not a reason
])
def test_new_rules_leave_real_student_portals_alone(url: str) -> None:
    assert portal_url_allowed(url) == (True, "ok")


# --------------------------------------------------------------------------- #
#  canonical_portal_url / portal_dedupe_key                                    #
# --------------------------------------------------------------------------- #


def test_canonical_url_drops_click_tracking_and_session_ids() -> None:
    assert canonical_portal_url(
        "https://www.udc.edu.br/site/?gad_source=1&gad_campaignid=9598923091"
        "&gbraid=0AAAAAD1cOss5N3nKXO0") == "https://www.udc.edu.br/site/"
    assert canonical_portal_url(
        "https://accounts.descomplica.com.br/?_gl=1*er7jev*_up*MQ..&fbclid=abc"
    ) == "https://accounts.descomplica.com.br/"
    assert canonical_portal_url(
        "https://portal.example.edu/login.do;jsessionid=0A1B2C3D?tenant=7"
    ) == "https://portal.example.edu/login.do?tenant=7"


def test_canonical_url_keeps_parameters_that_select_the_page() -> None:
    url = "https://charaka.wbuhs.ac.in/home/loginui?t=4"
    assert canonical_portal_url(url) == url


@pytest.mark.parametrize("first, second", [
    ("http://portal.example.edu/login", "https://portal.example.edu/login"),
    ("https://www.portalacademico.uneb.br/PortalSagres/",
     "https://portalacademico.uneb.br/PortalSagres"),
    ("https://login.gitam.edu/Login.aspx", "https://login.gitam.edu/login.aspx"),
    ("https://moodle.example.edu/login/index.php", "https://moodle.example.edu/login/"),
    ("https://example.edu/portal/default.aspx", "https://example.edu/portal"),
    ("https://www.udc.edu.br/site/?gbraid=abc", "https://www.udc.edu.br/site/"),
])
def test_dedupe_key_matches_two_addresses_of_one_page(first: str, second: str) -> None:
    assert portal_dedupe_key(first) == portal_dedupe_key(second)


@pytest.mark.parametrize("first, second", [
    ("https://lms1.numl.edu.pk/login/index.php", "https://lms2.numl.edu.pk/login/index.php"),
    ("https://charaka.wbuhs.ac.in/home/loginui?t=4", "https://charaka.wbuhs.ac.in/home/loginui?t=5"),
    ("https://www.fibrapara.edu.br/aluno-online/index.php",
     "https://www.fibrapara.edu.br/aluno-online-pos/"),
    ("https://portal.example.edu:8443/login", "https://portal.example.edu/login"),
])
def test_dedupe_key_keeps_different_portals_apart(first: str, second: str) -> None:
    assert portal_dedupe_key(first) != portal_dedupe_key(second)


# --------------------------------------------------------------------------- #
#  admission detection                                                         #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("url", [
    "https://sgbauadm.samarth.edu.in/index.php/site/login",
    "https://magadhuniversityadm.samarth.edu.in/index.php/site/login",
    "https://muugadmission.samarth.edu.in/index.php/site/login",
    "https://makaut1.ucanapply.com/student",
    "https://admissions.uenr.edu.gh/applicant-login",
    "https://www.lnipe.in/lnipeadmission/index",
    "http://admission.baou.edu.in/post-admisssion/welcome.aspx",
    "https://www.anhanguera.com/candidato/area-logada/login/",
    "https://entrance.gnsu.org/ERP/OnlineForm/FormLogin.aspx",
    "https://example.edu.br/vestibular/login",
    "https://josaa.nic.in/login",
])
def test_admission_urls_are_recognised_without_a_fetch(url: str) -> None:
    hit, reason = is_admission_url(url)
    assert hit is True, url
    assert reason


@pytest.mark.parametrize("url", [
    "https://magadhuniversity.samarth.edu.in/index.php/site/login",
    "https://cus.samarth.edu.in/index.php/site/login",
    "https://dte-assam.samarth.edu.in/index.php/site/login",
    "https://student.mdu.ac.in/index",
    "https://ausexamination.ac.in/registration/login",   # enrolled-student exam registration
    "https://sps.unipune.ac.in/app/landing/home",
    "https://autoservicios.buap.mx/autoservicios/twbkwbis.P_WWWLogin",
])
def test_student_portal_urls_are_not_admission_urls(url: str) -> None:
    assert is_admission_url(url) == (False, "")


MAGADH_ADMISSIONS = """
<html><head><title>(Admissions 2024-28) Magadh University, Bodh Gaya</title></head>
<body><h2>Magadh University, Bodh Gaya</h2><h3>Admission 2024-28 Portal</h3>
<form><h4>Applicant Login</h4><input name="username"><input type="password">
<small>Password is case-sensitive</small><button type="submit">Login</button>
<a href="/forgot">Forgot your Username/Password ?</a></form>
<p>Please note that all payments made on this website are non-refundable.
For students applying this year, read our Policy Statement.</p></body></html>
"""

# A real student portal that carries one admission link beside its login.
TMV_STUDENT_PORTAL = """
<html><head><title>Tilak Maharashtra Vidyapeeth</title></head><body>
<form><h3>Login</h3><input name="username"><a>Forgot Username?</a>
<input type="password"><a>Forgot Password?</a><button type="submit">Login</button></form>
<p>OR</p><a href="/new">New Student Registration</a>
<a href="/old">Old Student Registration</a>
<p>Already registered students can sign in above.</p></body></html>
"""

MOODLE_LOGIN = """
<html><head><title>Log in to the site</title></head><body>
<form action="https://lms.example.edu/login/index.php"><input name="username">
<input type="password"><button type="submit">Log in</button></form>
<h2>Is this your first time here?</h2><a>Create new account</a>
<p>New student registration and applicant login are handled elsewhere.</p>
</body></html>
"""


def test_admission_cycle_title_marks_an_admission_portal() -> None:
    hit, reason = is_admission_portal(
        "https://www.magadhonline.in/2024-28/student-login", MAGADH_ADMISSIONS)
    assert hit is True
    assert "admission" in reason


def test_applicant_login_text_is_enough_without_the_title() -> None:
    html = MAGADH_ADMISSIONS.replace("(Admissions 2024-28) ", "")
    hit, reason = is_admission_portal("https://www.magadhonline.in/x/student-login", html)
    assert hit is True
    assert reason == "strong admission signal 'applicant login'"


def test_applicant_login_decides_even_beside_enrolled_student_wording() -> None:
    """The Magadh admissions HOMEPAGE: no cycle in the title, and it tells
    applicants their password is built from their registration number."""
    html = """<html><head><title>Magadh University, Bodh Gaya</title></head><body>
    <a href="/ug-2026-30/student-login">Applicant Login (आवेदक लॉगिन)</a>
    <a href="/college">College Login</a><a>Admission Rules</a><a>Seats Availability</a>
    <p>Dear Students, your password is the first 4 characters of your name and
    your registration number.</p></body></html>"""
    hit, reason = is_admission_portal("https://www.magadhonline.in/", html)
    assert hit is True
    assert reason == "strong admission signal 'applicant login'"


def test_one_admission_link_does_not_condemn_a_student_portal() -> None:
    assert is_admission_portal(
        "https://tmv.academydesk.com/index.html", TMV_STUDENT_PORTAL) == (False, "")


def test_moodle_login_is_never_an_admission_portal() -> None:
    assert is_admission_portal(
        "https://lms.example.edu/login/index.php", MOODLE_LOGIN) == (False, "")


def test_redirect_target_is_checked_as_well_as_the_entry_url() -> None:
    hit, _ = is_admission_portal(
        "https://example.ac.in/login https://exampleadm.samarth.edu.in/index.php/site/login")
    assert hit is True


def _serve(monkeypatch, html: str, landed_url: str) -> None:
    class Content:
        async def read(self, _limit):
            return html.encode("utf-8")

    class Response:
        status = 200
        url = landed_url
        headers = {"Content-Type": "text/html; charset=utf-8"}
        content = Content()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def get(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr("agent.guardrails.aiohttp.ClientSession",
                        lambda **_kwargs: Session())


def test_login_check_rejects_an_admission_page_with_a_real_form(monkeypatch) -> None:
    """The form and the word "students" are both there; it is still admissions."""
    url = "https://www.magadhonline.in/2024-28/student-login"
    _serve(monkeypatch, MAGADH_ADMISSIONS, url)
    accepted, note, _final = asyncio.run(
        verify_student_login_surface(url, use_proxy=False))
    assert accepted is False
    assert note.startswith(ADMISSION_NOTE)


def test_login_check_rejects_an_admission_url_before_fetching(monkeypatch) -> None:
    def no_network(**_kwargs):
        raise AssertionError("an admission URL must not be fetched")

    monkeypatch.setattr("agent.guardrails.aiohttp.ClientSession", no_network)
    accepted, note, _final = asyncio.run(verify_student_login_surface(
        "https://sgbauadm.samarth.edu.in/index.php/site/login", use_proxy=False))
    assert accepted is False
    assert note.startswith(ADMISSION_NOTE)


def test_login_check_still_accepts_a_student_portal_with_an_admission_link(
        monkeypatch) -> None:
    url = "https://tmv.academydesk.com/index.html"
    _serve(monkeypatch, TMV_STUDENT_PORTAL, url)
    accepted, note, _final = asyncio.run(
        verify_student_login_surface(url, use_proxy=False))
    assert accepted is True, note


# --------------------------------------------------------------------------- #
#  several official domains                                                    #
# --------------------------------------------------------------------------- #


def test_split_domains_handles_the_separators_sheets_use() -> None:
    assert orch.split_domains("mgmcri.ac.in, sbvu.ac.in") == ["mgmcri.ac.in", "sbvu.ac.in"]
    assert orch.split_domains("a.edu;b.edu  A.edu\nc.edu.") == ["a.edu", "b.edu", "c.edu"]
    assert orch.split_domains("  ") == []


def test_official_host_accepts_any_of_the_organisations_domains() -> None:
    domains = "mgmcri.ac.in, sbvu.ac.in"
    assert _official_host("https://garuda.sbvu.ac.in/", domains) is True
    assert _official_host("https://www.mgmcri.ac.in/login", domains) is True
    assert _official_host("https://sbvu.ac.in.evil.example/", domains) is False
    assert _official_host("https://example.edu/", "") is False


def test_row_exposes_every_domain_and_defaults_to_the_primary() -> None:
    row = orch.UniversityRow(1, "1", "X", "a.edu", "IN", ("a.edu", "b.edu"))
    assert row.official_domains == "a.edu,b.edu"
    assert orch.UniversityRow(1, "1", "X", "a.edu").official_domains == "a.edu"


# --------------------------------------------------------------------------- #
#  batch pipeline: reachable is not enough                                     #
# --------------------------------------------------------------------------- #


def _portal(url: str) -> SimpleNamespace:
    return SimpleNamespace(exact_url=url, category="ERP", portal_system_name="")


def _patch_pipeline(monkeypatch, verdicts: dict[str, tuple[bool, str, str]],
                    dead: set[str] = frozenset()) -> list[dict]:
    calls: list[dict] = []

    async def endpoint(url, *_args, **_kwargs):
        return (url not in dead, 200 if url not in dead else 0, "head", url)

    async def gate(url, **kwargs):
        calls.append({"url": url, **kwargs})
        return verdicts[url]

    monkeypatch.setattr(orch, "verify_portal_endpoint_final", endpoint)
    monkeypatch.setattr(orch, "verify_student_login_surface_hybrid", gate)
    monkeypatch.setattr(orch, "LOGIN_GATE", True)
    return calls


def test_batch_drops_a_reachable_page_that_is_not_a_student_login(monkeypatch) -> None:
    home = "https://www.example.ac.in/"
    login = "https://erp.example.ac.in/student/login"
    adm = "https://www.magadhonline.in/2024-28/student-login"
    gone = "https://old.example.ac.in/login"
    calls = _patch_pipeline(monkeypatch, {
        home: (False, "no password input; no actionable rendered login/SSO control", home),
        login: (True, "password, identifier, and sign-in control", login),
        adm: (False, f"{ADMISSION_NOTE}: title/h1 names an admission cycle", adm),
    }, dead={gone})
    row = orch.UniversityRow(7, "663889", "Example", "example.ac.in", "IN",
                             ("example.ac.in", "example.edu.in"))
    live, dead, insecure, rejected = asyncio.run(orch.verify_portals(
        row, [_portal(home), _portal(login), _portal(adm), _portal(gone)], {"samarth.edu.in"}))
    assert [p.exact_url for p, _ in live] == [login]
    assert dead == 1 and insecure == []
    assert [url for url, _ in rejected] == [home, adm]
    # The dead endpoint is never rendered, and every domain reaches the gate.
    assert [c["url"] for c in calls] == [home, login, adm]
    assert calls[0]["official_domain"] == "example.ac.in,example.edu.in"
    assert calls[0]["shared_platform_roots"] == {"samarth.edu.in"}
    # The row loop unpacks the endpoint check; it has four fields, not three.
    for _portal_obj, (alive, status, *_rest) in live:
        assert alive is True and status == 200


def test_batch_publishes_the_stable_url_and_merges_duplicates(monkeypatch) -> None:
    front = "https://siakad.uinib.ac.id/"
    form = "https://siakad.uinib.ac.id/gate/login"
    tracked = "https://www.siakad.uinib.ac.id/gate/login/?gclid=abc"
    _patch_pipeline(monkeypatch, {
        front: (True, "rendered login/SSO authorisation control", form),
        form: (True, "password, identifier, and sign-in control", form),
        tracked: (True, "password, identifier, and sign-in control", tracked),
    })
    row = orch.UniversityRow(1, "10056842", "UIN", "uinib.ac.id", "ID")
    live, _dead, _insecure, rejected = asyncio.run(orch.verify_portals(
        row, [_portal(front), _portal(form), _portal(tracked)]))
    assert [p.exact_url for p, _ in live] == [form]
    assert rejected == []


def test_batch_rejects_a_login_that_resolves_to_a_disallowed_url(monkeypatch) -> None:
    home = "https://www.example.edu/"
    _patch_pipeline(monkeypatch, {
        home: (True, "rendered login/SSO authorisation control",
               "https://www.example.edu/wp-login.php"),
    })
    row = orch.UniversityRow(1, "1", "X", "example.edu")
    live, _dead, _insecure, rejected = asyncio.run(
        orch.verify_portals(row, [_portal(home)]))
    assert live == []
    assert rejected == [(home, "final URL: CMS, hosting-panel, mail or file-storage login")]


def test_batch_survives_a_login_check_that_raises_or_hangs(monkeypatch) -> None:
    boom, slow = "https://a.example.edu/login", "https://b.example.edu/login"

    async def endpoint(url, *_args, **_kwargs):
        return True, 200, "head", url

    async def gate(url, **_kwargs):
        if url == boom:
            raise RuntimeError("browser crashed")
        await asyncio.sleep(5)
        return True, "", url

    monkeypatch.setattr(orch, "verify_portal_endpoint_final", endpoint)
    monkeypatch.setattr(orch, "verify_student_login_surface_hybrid", gate)
    monkeypatch.setattr(orch, "LOGIN_GATE", True)
    monkeypatch.setattr(orch, "LOGIN_GATE_TIMEOUT_S", 0.05)
    row = orch.UniversityRow(1, "1", "X", "example.edu")
    live, _dead, _insecure, rejected = asyncio.run(
        orch.verify_portals(row, [_portal(boom), _portal(slow)]))
    assert live == []
    assert rejected[0] == (boom, "login check RuntimeError")
    assert rejected[1][1].startswith("login check timed out")


def test_batch_judges_the_returned_url_before_following_its_redirect(monkeypatch) -> None:
    """`https://support@host` redirects to a clean homepage; reject it first."""
    email = "https://support@magadhonline.in"
    probed: list[str] = []

    async def endpoint(url, *_args, **_kwargs):
        probed.append(url)
        return True, 200, "head", "https://www.magadhonline.in/"

    async def gate(*_args, **_kwargs):
        raise AssertionError("an e-mail address must never reach the login check")

    monkeypatch.setattr(orch, "verify_portal_endpoint_final", endpoint)
    monkeypatch.setattr(orch, "verify_student_login_surface_hybrid", gate)
    monkeypatch.setattr(orch, "LOGIN_GATE", True)
    row = orch.UniversityRow(1, "663889", "Magadh", "magadhuniversity.ac.in")
    live, _dead, _insecure, rejected = asyncio.run(
        orch.verify_portals(row, [_portal(email)]))
    assert live == [] and probed == []
    assert rejected == [(email, "e-mail address or credentials in URL")]


def test_gate_can_be_switched_off(monkeypatch) -> None:
    home = "https://www.example.ac.in/"

    async def endpoint(url, *_args, **_kwargs):
        return True, 200, "head", url

    async def gate(*_args, **_kwargs):
        raise AssertionError("the login check must not run when the gate is off")

    monkeypatch.setattr(orch, "verify_portal_endpoint_final", endpoint)
    monkeypatch.setattr(orch, "verify_student_login_surface_hybrid", gate)
    monkeypatch.setattr(orch, "LOGIN_GATE", False)
    row = orch.UniversityRow(1, "1", "X", "example.ac.in")
    live, _dead, _insecure, rejected = asyncio.run(
        orch.verify_portals(row, [_portal(home)]))
    assert [p.exact_url for p, _ in live] == [home]
    assert rejected == []


def test_batch_never_publishes_a_plain_http_replacement(monkeypatch) -> None:
    """An https page whose Login link leads to an http form: neither the
    clear-text login nor the page that only links to it is published."""
    home = "https://www.example.ac.in/"
    _patch_pipeline(monkeypatch, {
        home: (True, "rendered login/SSO authorisation control; login link opened "
                     "and confirmed", "http://erp.example.ac.in/login"),
    })
    row = orch.UniversityRow(1, "1", "X", "example.ac.in")
    live, _dead, _insecure, rejected = asyncio.run(
        orch.verify_portals(row, [_portal(home)]))
    assert live == []
    assert rejected[0][0] == home and rejected[0][1].startswith("needs review:")

"""Focused checks for GenieV3's final student-login acceptance gate."""

import asyncio
import unicodedata
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.guardrails import (
    _login_form_evidence,
    _same_organisation_redirect,
    _get_render_sem,
    _rendered_auth_control,
    _rendered_auth_control_target,
    _stable_verified_url,
    _student_audience_evidence,
    canonical_portal_url,
    portal_dedupe_key,
    portal_url_allowed,
    verify_student_login_surface,
)
from agent.filters import LocalKnowledgeMatrixFilter
from agent.lookup import _balanced_render_indexes, _get_browser_sem


def test_rejects_documents_and_literal_ip_hosts() -> None:
    assert portal_url_allowed("https://example.edu/login.pdf")[0] is False
    assert portal_url_allowed("https://203.0.113.7/login")[0] is False
    assert portal_url_allowed("https://[2001:db8::7]/login")[0] is False
    assert portal_url_allowed(
        "https://utelecon-directory.adm.u-tokyo.ac.jp/login")[0] is False
    assert portal_url_allowed("https://javascript:void(0);")[0] is False
    assert portal_url_allowed("javascript:void(0)")[0] is False


def test_browser_limiters_are_scoped_to_each_event_loop() -> None:
    async def semaphores() -> tuple[asyncio.Semaphore, asyncio.Semaphore]:
        return _get_browser_sem(), _get_render_sem()

    first_browser, first_render = asyncio.run(semaphores())
    second_browser, second_render = asyncio.run(semaphores())
    assert first_browser is not second_browser
    assert first_render is not second_render


def test_balanced_fast_render_only_runs_when_static_finds_nothing() -> None:
    eligible = [
        (SimpleNamespace(exact_url="https://lms.example.edu/login"), None),
        (SimpleNamespace(exact_url="https://example.edu/news"), None),
        (SimpleNamespace(exact_url="https://portal.example.edu/sso"), None),
    ]
    failures = [(False, "no form", url.exact_url) for url, _ in eligible]
    assert _balanced_render_indexes(eligible, failures, limit=2) == [0, 2]

    one_static_success = list(failures)
    one_static_success[0] = (True, "form", eligible[0][0].exact_url)
    assert _balanced_render_indexes(eligible, one_static_success, limit=2) == [2]


def test_rejects_informational_and_internal_lms_content_routes() -> None:
    rejected = [
        "https://example.edu/faq/student-login",
        "https://example.edu/online_guide/sso/",
        "https://lms.example.edu/admin/tool/policy/view.php?id=2",
        "https://lms.example.edu/course/view.php?id=10",
        "https://lms.example.edu/enrol/index.php?id=10",
        "https://lms.example.edu/courses/26335",
        "https://lms.example.edu/portal/site/!error/page/!error-100",
        "https://auth.example.edu/portal/en/1st-login.html",
        "https://study.example.edu/lms/error/notLogin",
    ]
    assert all(portal_url_allowed(url)[0] is False for url in rejected)


def test_accepts_a_real_login_form() -> None:
    accepted, reason = _login_form_evidence("""
        <form action="/session" method="post">
          <input name="student_id" type="text">
          <input name="password" type="password">
          <button type="submit">Student sign in</button>
        </form>
    """)
    assert accepted is True
    assert "password" in reason


def test_rejects_an_informational_login_link_without_a_form() -> None:
    accepted, reason = _login_form_evidence("""
        <main><h1>Library information</h1><a href="/login">Login</a></main>
    """)
    assert accepted is False
    assert reason == "no password input"


def test_rendered_sso_gate_requires_an_actionable_control() -> None:
    actionable, _ = _rendered_auth_control(
        '<a href="/sso/login">Student SSO Login</a>')
    prose_only, _ = _rendered_auth_control(
        '<p>Read the student login documentation before continuing.</p>')
    assert actionable is True
    assert prose_only is False


def test_static_verifier_accepts_actionable_academic_sso(monkeypatch) -> None:
    """A real LMS login button should not require Chromium to be accepted.

    The link is opened before it is published: here it leads to the sign-in
    form, so the form's address is what comes back.
    """
    pages = {
        "https://lms.example.edu/": b"""
            <html><h1>Student learning management system</h1>
            <a href="/sso/login">Student SSO Login</a></html>
        """,
        "https://lms.example.edu/sso/login": b"""
            <html><form><input name="username"><input type="password">
            <button type="submit">Sign in</button></form></html>
        """,
    }

    class Response:
        status = 200
        headers = {"Content-Type": "text/html; charset=utf-8"}

        def __init__(self, url):
            self.url = url
            body = pages[url]

            class Content:
                async def read(self, _limit):
                    return body

            self.content = Content()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def get(self, url, *_args, **_kwargs):
            return Response(url)

    monkeypatch.setattr("agent.guardrails.aiohttp.ClientSession",
                        lambda **_kwargs: Session())
    accepted, note, stable = asyncio.run(verify_student_login_surface(
        "https://lms.example.edu/", use_proxy=False,
    ))
    assert accepted is True
    assert "login/SSO" in note
    assert "login link opened and confirmed" in note
    assert stable == "https://lms.example.edu/sso/login"


def test_login_link_yields_the_authentication_target() -> None:
    accepted, _, target = _rendered_auth_control_target(
        '<nav><a href="/sso/login">Student SSO Login</a></nav>',
        "https://example.edu/help/portal",
    )
    assert accepted is True
    assert target == "https://example.edu/sso/login"


def test_canonical_output_strips_markdown_and_language_tracking_variants() -> None:
    first = canonical_portal_url(
        "https://lms.example.edu/login?changeLangCd=en&utm_source=openai%60")
    second = canonical_portal_url("https://lms.example.edu/login")
    assert first == "https://lms.example.edu/login"
    assert portal_dedupe_key(first) == portal_dedupe_key(second)


def test_preserves_stable_entry_url_for_ephemeral_auth_redirect() -> None:
    published = "https://slink.example/user/index.php?tenant=university"
    final = "https://slink.example/login?sessid=temporary&RelayState=x"
    assert _stable_verified_url(published, final) == published


def test_recovers_stable_back_url_from_published_session_transaction() -> None:
    published = (
        "https://slink.example/pub/tenantlogin.cgi?sessid=temporary&"
        "back=https%3A%2F%2Fslink.example%2Fuser%2Findex.php%3Ftenant%3Duniversity"
    )
    final = "https://slink.example/login?sessid=another-temporary-value"
    assert _stable_verified_url(published, final) == (
        "https://slink.example/user/index.php?tenant=university")


def test_recovers_stable_openid_return_url() -> None:
    published = (
        "https://portal.example.edu/openid/action/login?"
        "openid.ns=http%3A%2F%2Fspecs.openid.net%2Fauth%2F2.0&"
        "openid.return_to=https%3A%2F%2Fportal.example.edu%2Fstudent%2Fhome&"
        "openid.assoc_handle=temporary-transaction"
    )
    final = "https://idp.example.net/sso?state=temporary"
    assert _stable_verified_url(published, final) == (
        "https://portal.example.edu/student/home")


def test_rejects_password_only_without_student_identifier() -> None:
    accepted, reason = _login_form_evidence("""
        <form><input type="password"><button type="submit">Login</button></form>
    """)
    assert accepted is False
    assert reason == "no username/student identifier input"


def test_rejects_staff_only_login_even_when_the_form_is_real() -> None:
    accepted, reason = _student_audience_evidence("""
        <h1>Employee HR Portal</h1>
        <form><input name="username"><input type="password"><button>Sign in</button></form>
    """, "https://hr.example.edu/login")
    assert accepted is False
    assert reason == "non-student audience only"


def test_generic_official_login_has_no_student_audience_evidence() -> None:
    accepted, reason = _student_audience_evidence("""
        <h1>University account directory</h1>
        <form><input name="username"><input type="password"><button>Sign in</button></form>
    """, "https://directory.example.edu/login")
    assert accepted is False
    assert reason == "no student or academic audience evidence"


def test_my_university_url_is_a_student_service_hint_not_a_directory() -> None:
    assert portal_url_allowed("https://my.example.edu/")[0] is True
    assert portal_url_allowed("https://directory.example.edu/login")[0] is False


def test_accepts_student_and_lms_evidence() -> None:
    student_ok, _ = _student_audience_evidence("""
        <form><input name="roll_number"><input type="password"><button>Student login</button></form>
    """, "https://portal.example.edu/login")
    moodle_ok, _ = _student_audience_evidence("""
        <form><input name="username"><input type="password"><button>Log in</button></form>
    """, "https://moodle.example.edu/login/index.php")
    assert student_ok is True
    assert moodle_ok is True
    sparse_lms_ok, _ = _student_audience_evidence(
        '<a href="/sso/login">Sign in</a>',
        "https://lms.gakusei.kyoto-u.ac.jp/portal/login",
    )
    assert sparse_lms_ok is True


def test_accepts_javascript_submit_and_japanese_lms_evidence() -> None:
    accepted, _ = _login_form_evidence("""
        <form id="loginForm" action="/login" method="post">
          <input id="username" name="username" type="text">
          <input id="password" name="password" type="password">
          <a onclick="document.getElementById('loginForm').submit();"></a>
        </form>
    """)
    audience_ok, _ = _student_audience_evidence("""
        UTOL（UTokyo LMS）は、本学の教職員及び学生に対して提供する
        学習管理システムです。
    """, "https://utol.ecc.u-tokyo.ac.jp/login")
    assert accepted is True
    assert audience_ok is True


def test_ranks_native_language_portal_labels() -> None:
    links = [
        {"url": "https://portal.example.jp/login", "text": "学生ログイン"},
        {"url": "https://lms.example.ae/", "text": "بوابة الطالب"},
        {"url": "https://e.example.ru/", "text": "Личный кабинет студента"},
    ]
    ranked = LocalKnowledgeMatrixFilter().filter_and_rank_links(links, country="Japan")
    assert {item["url"] for item in ranked} == {item["url"] for item in links}


# --------------------------------------------------------------------------- #
#  Native-language audience evidence, SIS platforms, same-root redirects       #
# --------------------------------------------------------------------------- #
_FIXTURES = Path(__file__).parent / "fixtures" / "login_gate"

#: A real sign-in form with no audience copy of its own.
_BARE_FORM = """
    <form method="post"><input name="username" type="text">
    <input name="password" type="password"><button type="submit">OK</button></form>
"""


def _labelled_form(label: str) -> str:
    return f"<h1>{label}</h1>{_BARE_FORM}"


@pytest.mark.parametrize("fixture, url", [
    ("hcmus_login.html", "https://new-portal4.hcmus.edu.vn/login.aspx"),
    ("siu_guarani_unr.html", "https://autogestion-guarani.unr.edu.ar"),
    ("siu_guarani_uncuyo.html", "https://guarani3.ingenieria.uncuyo.edu.ar/g3w3/"),
    ("tinchi_hau.html", "https://tinchi.hau.edu.vn"),
    ("coesfhe_sign_in.html", "https://coesfhe.com/users/sign_in"),
    ("etlab_tkmce.html", "https://tkmce.etlab.in/user/login"),
    ("eduno_ava.html", "https://ava07.eduno.com.br/login"),
])
def test_accepts_reported_non_english_student_logins(fixture: str, url: str) -> None:
    """Pages users reported that the gate used to reject for audience."""
    html = (_FIXTURES / fixture).read_text(encoding="utf-8")
    form_ok, form_reason = _login_form_evidence(html)
    audience_ok, audience_reason = _student_audience_evidence(html, url)
    assert form_ok is True, form_reason
    assert audience_ok is True, audience_reason


@pytest.mark.parametrize("label", [
    # Spanish
    "Acceso para estudiantes", "Portal del Alumno", "Ingreso de alumnos",
    "Bienestar estudiantil", "Sistema de Control de Estudios", "Aula Virtual",
    # Portuguese
    "Portal do Aluno", "Acesso do estudante", "Área do discente",
    "Sistema Acadêmico", "Ambiente Virtual de Aprendizagem",
    "Educação a Distância",
    # Vietnamese
    "Cổng thông tin sinh viên", "CỔNG THÔNG TIN SINH VIÊN",
    "Phòng Đào tạo", "Đăng ký tín chỉ", "Cổng học viên",
    # French
    "Espace étudiant", "Portail des étudiantes", "Service de la scolarité",
    # Indonesian / Malay
    "Portal Mahasiswa", "Sistem Informasi Akademik", "SIAKAD", "Portal Pelajar",
    # Turkish
    "Öğrenci Bilgi Sistemi", "ÖĞRENCİ GİRİŞİ", "Uzaktan Eğitim",
    # Languages the discovery profiles already rank on
    "學生入口", "بوابة الطالب", "Личный кабинет студента", "विद्यार्थी पोर्टल",
])
def test_native_language_student_terms_are_audience_evidence(label: str) -> None:
    accepted, reason = _student_audience_evidence(
        _labelled_form(label), "https://acceso.example.org/")
    assert accepted is True, f"{label}: {reason}"
    assert reason == "student/academic audience evidence"


@pytest.mark.parametrize("field", [
    '<input name="matricula" type="text">',                 # es / pt
    '<input name="user" placeholder="Matrícula" type="text">',
    '<input id="inputLegajo" name="user" type="text">',     # Argentina
    '<input id="txtMaSinhVien" name="user" type="text">',   # Vietnam
    '<input name="user" placeholder="MSSV" type="text">',
    '<input name="nim" type="text">',                       # Indonesia
    '<input name="ogrenci_no" type="text">',                # Turkey
    '<input name="user" aria-label="Numéro étudiant" type="text">',
])
def test_student_identifier_fields_are_audience_evidence(field: str) -> None:
    accepted, reason = _student_audience_evidence(
        f'<form>{field}<input type="password"><button type="submit">OK</button></form>',
        "https://acceso.example.org/")
    assert accepted is True, f"{field}: {reason}"


def test_decomposed_vietnamese_diacritics_still_match() -> None:
    decomposed = unicodedata.normalize("NFD", "Cổng thông tin sinh viên")
    assert decomposed != "Cổng thông tin sinh viên"
    accepted, _ = _student_audience_evidence(
        _labelled_form(decomposed), "https://acceso.example.org/")
    assert accepted is True


@pytest.mark.parametrize("url", [
    "https://autogestion-guarani.unr.edu.ar/",             # SIU Guaraní host
    "https://guarani3.ingenieria.uncuyo.edu.ar/g3w3/",     # ... versioned path
    "https://g3w.example.edu.ar/",
    "https://du.samarth.edu.in/index.php/site/login",      # Samarth students
    "https://tkmce.etlab.in/user/login",                   # etlab
    "https://example.mastersofterp.in/",                   # MasterSoft ERP
])
def test_known_student_information_systems_need_no_audience_copy(url: str) -> None:
    accepted, reason = _student_audience_evidence(_BARE_FORM, url)
    assert accepted is True, f"{url}: {reason}"
    assert "known" in reason


def test_student_information_systems_are_recognised_from_page_branding() -> None:
    url = "https://erp.example.ac.in/login"
    for branding in ("SIU Guaraní - Acceso", "etlab | login",
                     "Designed by MasterSoft ERP Solutions Pvt. Ltd."):
        accepted, reason = _student_audience_evidence(
            f"<title>{branding}</title>{_BARE_FORM}", url)
        assert accepted is True, f"{branding}: {reason}"


@pytest.mark.parametrize("html, url, expected", [
    # English non-student wording still wins over a recognised SIS platform.
    ("<h1>Staff login</h1>" + _BARE_FORM,
     "https://tkmce.etlab.in/user/login", "non-student audience only"),
    ("<h1>Employee self service</h1>" + _BARE_FORM,
     "https://autogestion.example.edu.ar/", "non-student audience only"),
    ("<h1>Admissions - applicant login</h1>" + _BARE_FORM,
     "https://du.samarth.edu.in/index.php/site/login", "non-student audience only"),
    # Alumni words in Spanish/Portuguese/Latin are not the new student terms.
    ("<h1>Alumni portal - ex-alumnos</h1>" + _BARE_FORM,
     "https://egresados.example.edu/login", "non-student audience only"),
    ("<h1>Alumni - antigos alunos</h1>" + _BARE_FORM,
     "https://www.example.pt/login", "non-student audience only"),
    ("<h1>Alumni and alumnae network</h1>" + _BARE_FORM,
     "https://network.example.edu/login", "non-student audience only"),
    # Native-language pages with no student wording stay unproven.
    ("<h1>Acceso docentes y personal administrativo</h1>" + _BARE_FORM,
     "https://intranet.example.edu.ar/login",
     "no student or academic audience evidence"),
    ("<h1>Universidad Nacional - Iniciar sesión</h1>" + _BARE_FORM,
     "https://cuentas.example.edu.ar/login",
     "no student or academic audience evidence"),
    # "Autogestión" is ordinary Spanish for self-service, not proof of SIU Guaraní.
    ("<h1>Autogestión de personal</h1>" + _BARE_FORM,
     "https://rrhh.example.edu.ar/autogestion",
     "no student or academic audience evidence"),
    (_BARE_FORM, "https://autogestion.example.edu.ar/acceso",
     "no student or academic audience evidence"),
    # Samarth's employee apex and a bare "Samarth" institution name.
    ("<h1>Samarth eGov</h1>" + _BARE_FORM,
     "https://nehu.samarth.ac.in/index.php/site/login",
     "no student or academic audience evidence"),
])
def test_non_student_and_unproven_logins_are_still_rejected(
        html: str, url: str, expected: str) -> None:
    accepted, reason = _student_audience_evidence(html, url)
    assert accepted is False
    assert reason == expected


def _serve(monkeypatch, html: str, landed_url: str) -> None:
    """Make the static verifier's GET land on ``landed_url`` with ``html``."""

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


def test_same_root_redirect_is_the_same_organisation() -> None:
    assert _same_organisation_redirect(
        "https://portal.hcmus.edu.vn",
        "https://new-portal3.hcmus.edu.vn/Login.aspx?ReturnUrl=%2f") is True
    # `edu.vn` is a public suffix, not an organisation.
    assert _same_organisation_redirect(
        "https://portal.hcmus.edu.vn", "https://portal.hcmut.edu.vn/") is False
    assert _same_organisation_redirect(
        "https://portal.example.edu", "https://login.vendor.com/sso") is False
    # Sibling tenants of one vendor are different institutions.
    assert _same_organisation_redirect(
        "https://tkmce.etlab.in/user/login", "https://demo.etlab.in/user/login",
        {"etlab.in"}) is False


def test_static_verifier_follows_a_load_balanced_sibling_host(monkeypatch) -> None:
    _serve(monkeypatch, (_FIXTURES / "hcmus_login.html").read_text(encoding="utf-8"),
           "https://new-portal3.hcmus.edu.vn/Login.aspx?ReturnUrl=%2f")
    accepted, note, stable = asyncio.run(verify_student_login_surface(
        "https://portal.hcmus.edu.vn", use_proxy=False))
    assert accepted is True, note
    assert "student/academic audience evidence" in note
    # The entry point is published, not whichever node answered this time.
    assert stable == "https://portal.hcmus.edu.vn"


def test_static_verifier_still_rejects_a_redirect_to_another_organisation(
        monkeypatch) -> None:
    html = (_FIXTURES / "hcmus_login.html").read_text(encoding="utf-8")
    _serve(monkeypatch, html, "https://portal.hcmut.edu.vn/login.aspx")
    accepted, note, final = asyncio.run(verify_student_login_surface(
        "https://portal.hcmus.edu.vn", use_proxy=False))
    assert accepted is False
    assert note == "redirected to another host"
    assert final == "https://portal.hcmut.edu.vn/login.aspx"


def test_static_verifier_rejects_a_hop_between_vendor_tenants(monkeypatch) -> None:
    html = (_FIXTURES / "etlab_tkmce.html").read_text(encoding="utf-8")
    _serve(monkeypatch, html, "https://demo.etlab.in/user/login")
    accepted, note, _final = asyncio.run(verify_student_login_surface(
        "https://tkmce.etlab.in/user/login", use_proxy=False,
        shared_platform_roots={"etlab.in"}))
    assert accepted is False
    assert note == "redirected to another host"

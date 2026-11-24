"""Sign-in wording in other languages, username-first sign-in pages, and
opening a Login link before it is published."""

import asyncio

import pytest

from agent import guardrails as G
from agent.guardrails import (
    IDENTIFIER_FIRST_NOTE,
    LINK_UNCONFIRMED_NOTE,
    NO_STUDENT_CONTROL_NOTE,
    _has_login_word,
    _login_controls,
    _login_form_evidence,
    _rendered_auth_control,
    verify_student_login_surface,
    verify_student_login_surface_hybrid,
)

# --------------------------------------------------------------------------- #
#  sign-in wording                                                             #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("label", [
    "Login", "Sign in",
    "Entrar", "Acessar o portal", "Fazer login",            # Portuguese
    "Iniciar sesión", "Inicio de sesion", "Ingresar",       # Spanish
    "Se connecter", "Connexion",                            # French
    "Öğrenci Girişi", "GİRİŞ", "Giriş Yap", "Oturum Aç",    # Turkish
    "Masuk", "Log Masuk",                                   # Indonesian, Malay
    "Đăng nhập",                                            # Vietnamese
    "Войти", "Вход",                                        # Russian
    "लॉगिन", "تسجيل الدخول", "登录",
])
def test_sign_in_wording_is_recognised(label: str) -> None:
    assert _has_login_word(label) is True


@pytest.mark.parametrize("label", [
    "Acesso à informação",        # on every Brazilian public site
    "Ingreso 2026",               # Spanish: admission intake
    "Jalur Masuk", "Seleksi masuk PTN",   # Indonesian: admission pathway
    "Girişimcilik Merkezi",       # Turkish: entrepreneurship centre
    "Entrarão em vigor", "Notícias", "Transparência", "Admissions",
])
def test_lookalike_words_are_not_sign_in_wording(label: str) -> None:
    assert _has_login_word(label) is False


def test_a_native_language_login_link_counts_as_a_control() -> None:
    assert _rendered_auth_control('<a href="/acesso/login">Entrar</a>')[0] is True
    assert _rendered_auth_control('<a href="/ogrenci">Öğrenci Girişi</a>')[0] is True
    assert _rendered_auth_control('<a href="/pmb">Jalur Masuk</a>')[0] is False


# --------------------------------------------------------------------------- #
#  username-first sign-in                                                      #
# --------------------------------------------------------------------------- #

ADFS = """<html><head><title>Sign In</title></head><body>
<form id="loginForm" action="/adfs/ls/?client-request-id=1">
<input id="userNameInput" name="UserName" type="email" placeholder="someone@example.com">
<input type="submit" value="Next"></form></body></html>"""

KEYCLOAK = """<html><head><title>Sign in to solapur</title></head><body>
<form id="kc-form-login" action="https://id.studentscenter.in/realms/solapur/login-actions/authenticate">
<input id="username" name="username" type="text">
<input type="submit" id="kc-login" value="Sign In"></form></body></html>"""

PORTUGUESE = """<html><head><title>Portal do Aluno</title></head><body>
<form action="/auth"><input name="usuario" type="text" placeholder="CPF ou matrícula">
<button type="submit">Entrar</button><a href="/esqueci">Esqueceu a senha?</a></form>
</body></html>"""


@pytest.mark.parametrize("html", [ADFS, KEYCLOAK, PORTUGUESE])
def test_username_first_sign_in_page_is_accepted(html: str) -> None:
    assert _login_form_evidence(html) == (True, IDENTIFIER_FIRST_NOTE)


@pytest.mark.parametrize("html", [
    # site search
    '<title>University</title><form role="search" action="/search">'
    '<input name="q" type="text"><button>Search</button></form>',
    '<title>Login help</title><form action="/buscar">'
    '<input name="username_search" type="text"><button>Buscar</button></form>',
    # newsletter
    '<title>Sign in news</title><form action="/subscribe">'
    '<input name="email" type="email"><button>Subscribe</button></form>',
    # password recovery, with and without a sign-in title
    '<title>Forgot password</title><form action="/reset">'
    '<input name="email" type="email"><button>Send reset link</button></form>',
    '<title>Student login</title><form action="/password/recover">'
    '<input name="email" type="email"><button>Continue</button></form>',
    # two visible fields and no password: not a sign-in step we can vouch for
    '<title>Login</title><form><input name="username"><input name="captcha">'
    '<button>Login</button></form>',
    # a homepage with a Login link and a search box
    '<title>Uni</title><a href="/login">Login</a>'
    '<form action="/s"><input name="s" type="search"></form>',
])
def test_single_field_forms_that_are_not_a_sign_in(html: str) -> None:
    assert _login_form_evidence(html) == (False, "no password input")


# --------------------------------------------------------------------------- #
#  reading the Login links of a page                                           #
# --------------------------------------------------------------------------- #


def test_student_link_is_tried_first_and_other_audiences_are_skipped() -> None:
    """telanganauniversity.ac.in lists "Staff login" before "Student login"."""
    html = """
        <a href="/staff_login.php">Staff login</a>
        <a href="/recruiter_login.php">Recruiter login</a>
        <a href="/login.php">Login</a>
        <a href="/student_login.php">Student login</a>
        <a href="/">Login</a>
    """
    has_button, targets = _login_controls(html, "https://www.telanganauniversity.ac.in/")
    assert has_button is False
    assert targets == [
        "https://www.telanganauniversity.ac.in/student_login.php",
        "https://www.telanganauniversity.ac.in/login.php",
    ]


def test_a_button_is_a_control_without_a_link_target() -> None:
    has_button, targets = _login_controls(
        '<button onclick="openLogin()">Entrar</button>', "https://example.edu/")
    assert has_button is True and targets == []


# --------------------------------------------------------------------------- #
#  opening the link before publishing it                                       #
# --------------------------------------------------------------------------- #

HOME = "https://www.example.ac.in/"
FORM = ('<html><form><input name="username"><input type="password">'
        '<button type="submit">Sign in</button></form></html>')
EMPLOYEE = ('<html><h1>Employee Portal</h1><form><input name="username">'
            '<input type="password"><button type="submit">Sign in</button></form></html>')
SPA_SHELL = '<html><body><div id="root"></div><script src="/app.js"></script></body></html>'


def _site(monkeypatch, pages: dict[str, object]) -> list[str]:
    """Serve ``pages`` (url -> html, or an int status, or an Exception)."""
    fetched: list[str] = []

    class Response:
        headers = {"Content-Type": "text/html; charset=utf-8"}

        def __init__(self, url: str):
            page = pages[url]
            self.url = url
            self.status = page if isinstance(page, int) else 200
            body = b"" if isinstance(page, int) else page.encode("utf-8")

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
            fetched.append(url)
            if isinstance(pages.get(url), Exception):
                raise pages[url]
            return Response(url)

    monkeypatch.setattr("agent.guardrails.aiohttp.ClientSession",
                        lambda **_kwargs: Session())
    return fetched


def _home(*links: str) -> str:
    return "<html><h1>Student services</h1>" + "".join(links) + "</html>"


def test_confirmed_link_is_published_instead_of_the_page(monkeypatch) -> None:
    login = "https://erp.example.ac.in/student/login"
    _site(monkeypatch, {HOME: _home(f'<a href="{login}">Student Login</a>'), login: FORM})
    accepted, note, url = asyncio.run(verify_student_login_surface(HOME, use_proxy=False))
    assert accepted is True
    assert "login link opened and confirmed" in note
    assert url == login


def test_link_to_an_employee_portal_is_not_published(monkeypatch) -> None:
    """Magadh's homepage linked to `samarth.ac.in`, Samarth's employee portal."""
    staff = "https://example.samarth.ac.in/index.php/site/login"
    _site(monkeypatch, {HOME: _home(f'<a href="{staff}">Login</a>'), staff: EMPLOYEE})
    accepted, note, url = asyncio.run(verify_student_login_surface(HOME, use_proxy=False))
    assert accepted is False
    assert note == "login link is not a student login: non-student audience only"
    assert url == HOME


SAMARTH_STAFF = """<html><head><title>Login - Samarth eGov Suite</title></head><body>
<h2>MAGADH UNIVERSITY</h2><form><input name="username"><input type="password">
<button type="submit">LOGIN</button></form><a href="/first-login">First Time Login ?</a>
</body></html>"""
SAMARTH_STUDENT = """<html><head><title>Samarth eGov</title></head><body>
<h2>MAGADH UNIVERSITY</h2><h3>Student Portal</h3><form><input name="username">
<input type="password"><button type="submit">Login</button></form></body></html>"""


def test_off_site_login_must_say_it_is_for_students(monkeypatch) -> None:
    """The real pair: `samarth.ac.in` never mentions students, `.edu.in` does."""
    staff = "https://magadhuniversity.samarth.ac.in/index.php/site/login"
    student = "https://magadhuniversity.samarth.edu.in/index.php/site/login"
    home = "https://www.magadhuniversity.ac.in/"
    # Not provably wrong, so the page is kept — but the link is NOT published.
    _site(monkeypatch, {home: _home(f'<a href="{staff}">Login</a>'), staff: SAMARTH_STAFF})
    accepted, note, url = asyncio.run(verify_student_login_surface(home, use_proxy=False))
    assert accepted is True and url == home
    assert LINK_UNCONFIRMED_NOTE in note

    _site(monkeypatch, {home: _home(f'<a href="{staff}">Login</a>',
                                    f'<a href="{student}">Login</a>'),
                        staff: SAMARTH_STAFF, student: SAMARTH_STUDENT})
    accepted, _note, url = asyncio.run(verify_student_login_surface(home, use_proxy=False))
    assert accepted is True and url == student


def test_login_link_to_an_ip_address_is_unverifiable_not_wrong(monkeypatch) -> None:
    """fasipe.com.br/aluno: the student system sits on a bare IP address."""
    _site(monkeypatch, {HOME: _home('<a href="http://203.0.113.9/aluno/login">Entrar</a>')})
    accepted, note, url = asyncio.run(verify_student_login_surface(HOME, use_proxy=False))
    assert accepted is True and url == HOME
    assert LINK_UNCONFIRMED_NOTE in note


def test_sparse_login_on_the_institutions_own_site_is_enough(monkeypatch) -> None:
    login = "https://erp.example.ac.in/login"
    _site(monkeypatch, {HOME: _home(f'<a href="{login}">Login</a>'), login: FORM})
    accepted, _note, url = asyncio.run(verify_student_login_surface(HOME, use_proxy=False))
    assert accepted is True and url == login


def test_the_next_link_is_tried_when_the_first_is_wrong(monkeypatch) -> None:
    adm = "https://exampleadm.samarth.edu.in/index.php/site/login"
    gone = "https://old.example.ac.in/login"
    login = "https://erp.example.ac.in/login"
    fetched = _site(monkeypatch, {
        HOME: _home(f'<a href="{adm}">Login</a>', f'<a href="{gone}">Login</a>',
                    f'<a href="{login}">Login</a>'),
        gone: 404, login: FORM})
    accepted, _note, url = asyncio.run(verify_student_login_surface(HOME, use_proxy=False))
    assert accepted is True and url == login
    assert adm not in fetched          # an admission URL is never fetched


def test_page_is_rejected_when_every_link_leads_elsewhere(monkeypatch) -> None:
    _site(monkeypatch, {
        HOME: _home('<a href="/wp-login.php">Login</a>',
                    '<a href="https://old.example.ac.in/login">Login</a>'),
        "https://old.example.ac.in/login": 404})
    accepted, note, _url = asyncio.run(verify_student_login_surface(HOME, use_proxy=False))
    assert accepted is False
    assert note.startswith("login link is not a student login")


def test_page_is_kept_when_its_link_cannot_be_judged(monkeypatch) -> None:
    """A JavaScript-built login shows no form in plain HTML: that is not a no."""
    app = "https://app.example.ac.in/login"
    _site(monkeypatch, {HOME: _home(f'<a href="{app}">Student Login</a>'), app: SPA_SHELL})
    accepted, note, url = asyncio.run(verify_student_login_surface(HOME, use_proxy=False))
    assert accepted is True
    assert LINK_UNCONFIRMED_NOTE in note
    assert url == HOME                 # the page, not the unverified link


def test_a_timeout_on_the_link_is_not_a_verdict(monkeypatch) -> None:
    app = "https://app.example.ac.in/login"
    _site(monkeypatch, {HOME: _home(f'<a href="{app}">Student Login</a>'),
                        app: asyncio.TimeoutError()})
    accepted, note, url = asyncio.run(verify_student_login_surface(HOME, use_proxy=False))
    assert accepted is True and url == HOME
    assert LINK_UNCONFIRMED_NOTE in note


def test_page_with_a_login_button_survives_a_wrong_link(monkeypatch) -> None:
    _site(monkeypatch, {HOME: _home('<a href="/wp-login.php">Login</a>',
                                    '<button onclick="openLogin()">Student Login</button>')})
    accepted, note, url = asyncio.run(verify_student_login_surface(HOME, use_proxy=False))
    assert accepted is True and url == HOME
    assert LINK_UNCONFIRMED_NOTE not in note


def test_page_whose_only_links_are_for_staff_is_rejected(monkeypatch) -> None:
    fetched = _site(monkeypatch, {HOME: _home('<a href="/emp/login">Staff Login</a>',
                                              '<a href="/">Login</a>')})
    accepted, note, _url = asyncio.run(verify_student_login_surface(HOME, use_proxy=False))
    assert accepted is False
    assert note == NO_STUDENT_CONTROL_NOTE
    assert fetched == [HOME]


def test_username_first_page_behind_a_link_is_confirmed(monkeypatch) -> None:
    sso = "https://fs.example.ac.in/adfs/ls/"
    _site(monkeypatch, {HOME: _home(f'<a href="{sso}">Entrar</a>'), sso: ADFS})
    accepted, note, url = asyncio.run(verify_student_login_surface(HOME, use_proxy=False))
    assert accepted is True and url == sso
    assert "login link opened and confirmed" in note


def test_link_that_redirects_to_another_host_publishes_the_link(monkeypatch) -> None:
    """`/sso/login` bounces to an identity provider; the entry is the stable URL."""
    sso = "https://lms.example.ac.in/sso/login"

    async def fetch(url, *_args, **_kwargs):
        return 200, "https://idp.sso-provider.net/login?SAMLRequest=abc&RelayState=x", FORM, ""

    _site(monkeypatch, {HOME: _home(f'<a href="{sso}">Student Login</a>')})
    monkeypatch.setattr(G, "_fetch_html", fetch)
    accepted, _note, url = asyncio.run(verify_student_login_surface(HOME, use_proxy=False))
    assert accepted is True and url == sso


# --------------------------------------------------------------------------- #
#  hybrid: render only when the link could not be read                         #
# --------------------------------------------------------------------------- #


def test_hybrid_renders_when_the_static_link_is_unconfirmed(monkeypatch) -> None:
    app = "https://app.example.ac.in/login"
    rendered_calls: list[str] = []

    async def static(url, *_a, **_k):
        return True, f"rendered login/SSO authorisation control; x; {LINK_UNCONFIRMED_NOTE}", url

    async def rendered(url, *_a, **_k):
        rendered_calls.append(url)
        return True, "password, identifier, and sign-in control; Chromium-rendered", app

    monkeypatch.setattr(G, "verify_student_login_surface", static)
    monkeypatch.setattr(G, "verify_student_login_surface_rendered", rendered)
    result = asyncio.run(verify_student_login_surface_hybrid(HOME))
    assert rendered_calls == [HOME]
    assert result[0] is True and result[2] == app


def test_hybrid_keeps_the_static_answer_when_the_render_fails(monkeypatch) -> None:
    async def static(url, *_a, **_k):
        return True, f"control; {LINK_UNCONFIRMED_NOTE}", url

    async def rendered(url, *_a, **_k):
        return False, "render TimeoutError", url

    monkeypatch.setattr(G, "verify_student_login_surface", static)
    monkeypatch.setattr(G, "verify_student_login_surface_rendered", rendered)
    accepted, note, url = asyncio.run(verify_student_login_surface_hybrid(HOME))
    assert accepted is True and url == HOME
    assert LINK_UNCONFIRMED_NOTE in note


def test_hybrid_does_not_render_a_confirmed_static_result(monkeypatch) -> None:
    async def static(url, *_a, **_k):
        return True, "password, identifier, and sign-in control; student", url

    async def rendered(*_a, **_k):
        raise AssertionError("a confirmed login must not cost a Chromium render")

    monkeypatch.setattr(G, "verify_student_login_surface", static)
    monkeypatch.setattr(G, "verify_student_login_surface_rendered", rendered)
    assert asyncio.run(verify_student_login_surface_hybrid(HOME))[0] is True

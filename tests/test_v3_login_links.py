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
    _login_links,
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
    has_button, links = _login_links(html, "https://www.telanganauniversity.ac.in/")
    targets = [target for target, _label in links]
    assert has_button is False
    assert targets == [
        "https://www.telanganauniversity.ac.in/student_login.php",
        "https://www.telanganauniversity.ac.in/login.php",
    ]


def test_a_button_is_a_control_without_a_link_target() -> None:
    has_button, links = _login_links(
        '<button onclick="openLogin()">Entrar</button>', "https://example.edu/")
    assert has_button is True and links == []


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
#  "same site" is judged by where the link LANDS                               #
# --------------------------------------------------------------------------- #

IDP = "https://sso.identity-host.net/signin"


def _redirecting(monkeypatch, link: str, lands_on: str, html: str, label: str) -> None:
    """HOME carries one Login link; opening it redirects to ``lands_on``."""
    async def fetch(url, *_args, **_kwargs):
        assert url == link
        return 200, lands_on, html, ""

    _site(monkeypatch, {HOME: _home(f'<a href="{link}">{label}</a>')})
    monkeypatch.setattr(G, "_fetch_html", fetch)


def test_classifier_gives_no_same_site_credit_after_an_off_site_redirect() -> None:
    link = "https://www.example.ac.in/login"
    # Stays on the institution's site: the linking page vouches for it.
    assert G._classify_login_target(link, link, FORM, HOME)[0] == "student"
    assert G._classify_login_target(
        link, "https://erp.example.ac.in/login", FORM, HOME)[0] == "student"
    # Lands on another site with a form that says nothing: no credit.
    verdict, reason = G._classify_login_target(link, IDP, FORM, HOME)
    assert verdict == "unsure"
    assert reason == "off-site login with no student or academic audience evidence"
    # ...unless the institution itself labelled the link for students,
    assert G._classify_login_target(link, IDP, FORM, HOME, "Student Login")[0] == "student"
    # or the page it lands on says so.
    student_form = FORM.replace("<form>", "<h1>Student Portal</h1><form>")
    assert G._classify_login_target(link, IDP, student_form, HOME)[0] == "student"
    # A page that names another audience is wrong wherever it is served from.
    assert G._classify_login_target(link, IDP, EMPLOYEE, HOME, "Login")[0] == "wrong"


def test_on_site_link_that_redirects_to_a_bare_outside_login_is_not_published(
        monkeypatch) -> None:
    """The reviewed case: `/login` on the institution's site hands off to an
    outside identity provider whose form never says who it is for."""
    link = "https://www.example.ac.in/login"
    _redirecting(monkeypatch, link, IDP, FORM, "Login")
    accepted, note, url = asyncio.run(verify_student_login_surface(HOME, use_proxy=False))
    assert accepted is True and url == HOME     # the page is kept ...
    assert LINK_UNCONFIRMED_NOTE in note        # ... the unverified link is not published


def test_student_labelled_link_through_an_outside_provider_is_published(monkeypatch) -> None:
    """Using Google, Microsoft or a vendor to sign in is not held against a portal."""
    link = "https://www.example.ac.in/login"
    _redirecting(monkeypatch, link, IDP + "?SAMLRequest=abc", FORM, "Student Login")
    accepted, note, url = asyncio.run(verify_student_login_surface(HOME, use_proxy=False))
    assert accepted is True and url == link     # the stable entry, not the transaction
    assert "login link opened and confirmed" in note


def test_on_site_link_that_redirects_to_an_outside_staff_login_rejects_the_page(
        monkeypatch) -> None:
    link = "https://www.example.ac.in/login"
    _redirecting(monkeypatch, link, IDP, EMPLOYEE, "Login")
    accepted, note, _url = asyncio.run(verify_student_login_surface(HOME, use_proxy=False))
    assert accepted is False
    assert note == "login link is not a student login: non-student audience only"


def test_redirect_to_a_sibling_host_is_still_the_same_site(monkeypatch) -> None:
    link = "https://www.example.ac.in/login"
    landed = "https://auth.example.ac.in/Account/Login"
    _redirecting(monkeypatch, link, landed, FORM, "Login")
    accepted, _note, url = asyncio.run(verify_student_login_surface(HOME, use_proxy=False))
    assert accepted is True and url == link


def test_login_links_carry_the_text_the_page_shows() -> None:
    _has_button, links = G._login_links(
        '<a href="/a">Login</a><a href="/b">Student Login</a>', HOME)
    assert links == [(HOME + "b", "Student Login"), (HOME + "a", "Login")]


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


# --------------------------------------------------------------------------- #
#  never fetch an internal address                                             #
# --------------------------------------------------------------------------- #


@pytest.fixture
def fake_dns(monkeypatch):
    """Resolve the test hosts to fixed addresses, and nothing else."""
    table = {
        "metadata.google.internal": "169.254.169.254",
        "intranet.example.ac.in": "10.0.0.5",
        "evil.example.net": "127.0.0.1",
        "erp.example.ac.in": "93.184.216.34",
        "www.example.ac.in": "93.184.216.34",
    }

    async def resolve(host):
        if host not in table:
            raise OSError("no such host")
        return [table[host]]

    monkeypatch.setattr(G, "_resolve", resolve)
    G._PUBLIC_HOST_CACHE.clear()
    yield table
    G._PUBLIC_HOST_CACHE.clear()


@pytest.mark.parametrize("host, public", [
    ("metadata.google.internal", False),     # cloud metadata, link-local
    ("intranet.example.ac.in", False),       # private network
    ("evil.example.net", False),             # resolves to this machine
    ("erp.example.ac.in", True),
    ("169.254.169.254", False), ("10.1.2.3", False), ("::1", False),
    ("93.184.216.34", True),
    # Internal names are refused even when they do not resolve here.
    ("metadata.internal", False), ("printer.local", False), ("localhost", False),
    ("nas.home.arpa", False), ("no-such-host.example.org", True),
])
def test_only_public_addresses_count_as_public(fake_dns, host, public) -> None:
    assert asyncio.run(G._host_is_public(host)) is public


def test_a_login_link_to_an_internal_address_is_never_fetched(fake_dns, monkeypatch) -> None:
    fetched = _site(monkeypatch, {
        HOME: _home('<a href="http://metadata.google.internal/computeMetadata/v1/">Login</a>')})
    accepted, note, _url = asyncio.run(verify_student_login_surface(HOME, use_proxy=False))
    assert fetched == [HOME]
    assert accepted is False
    assert G.NON_PUBLIC_NOTE in note


def test_a_redirect_to_an_internal_address_is_not_followed(fake_dns, monkeypatch) -> None:
    """A public link that bounces to a private address stops at the bounce."""
    requested: list[str] = []

    class Response:
        def __init__(self, url):
            self.url = url
            self.status = 302
            self.headers = {"Location": "http://intranet.example.ac.in/login"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def get(self, url, *_args, allow_redirects=True, **_kwargs):
            assert allow_redirects is False      # every hop is ours to check
            requested.append(url)
            return Response(url)

    monkeypatch.setattr("agent.guardrails.aiohttp.ClientSession", lambda **_k: Session())
    status, final, html, error = asyncio.run(G._fetch_html(
        "https://erp.example.ac.in/login", 5, "", False))
    assert requested == ["https://erp.example.ac.in/login"]
    assert status == 0 and html == ""
    assert final == "http://intranet.example.ac.in/login"
    assert error == f"redirect: {G.NON_PUBLIC_NOTE}"


def test_public_redirects_are_followed_hop_by_hop(fake_dns, monkeypatch) -> None:
    hops = {"https://www.example.ac.in/login": "/sso",
            "https://www.example.ac.in/sso": "https://erp.example.ac.in/login"}

    class Content:
        async def read(self, _limit):
            return FORM.encode()

    class Response:
        def __init__(self, url):
            self.url = url
            self.status = 302 if url in hops else 200
            self.headers = {"Location": hops[url]} if url in hops else {}
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

    monkeypatch.setattr("agent.guardrails.aiohttp.ClientSession", lambda **_k: Session())
    status, final, html, error = asyncio.run(G._fetch_html(
        "https://www.example.ac.in/login", 5, "", False))
    assert (status, final, error) == (200, "https://erp.example.ac.in/login", "")
    assert "password" in html


def test_click_through_browser_aborts_internal_requests(fake_dns) -> None:
    from agent import click_through as CT
    calls: list[str] = []

    class Route:
        def __init__(self, url):
            self.request = type("R", (), {"url": url})()

        async def abort(self, reason):
            calls.append(f"abort {self.request.url} {reason}")

        async def continue_(self):
            calls.append(f"continue {self.request.url}")

    for url in ("http://metadata.google.internal/computeMetadata/v1/",
                "https://erp.example.ac.in/login", "data:text/html,hi"):
        asyncio.run(CT._only_public(Route(url)))
    assert calls == [
        "abort http://metadata.google.internal/computeMetadata/v1/ blockedbyclient",
        "continue https://erp.example.ac.in/login",
        "continue data:text/html,hi",
    ]


# --------------------------------------------------------------------------- #
#  third review: relative links, no render of link targets                     #
# --------------------------------------------------------------------------- #


def test_plain_relative_login_links_are_followed() -> None:
    base = "https://www.example.ac.in/portal/"
    has_button, links = G._login_links(
        '<a href="login.php">Login</a><a href="../student/signin">Student Login</a>'
        '<a href="#login">Login</a><a href="javascript:void(0)">Login</a>'
        '<a href="mailto:help@example.ac.in">Login help</a>', base)
    assert has_button is False
    assert [t for t, _l in links] == ["https://www.example.ac.in/student/signin",
                                      "https://www.example.ac.in/portal/login.php"]


def test_relative_link_is_opened_and_published(monkeypatch) -> None:
    login = "https://www.example.ac.in/login.php"
    _site(monkeypatch, {HOME: _home('<a href="login.php">Login</a>'), login: FORM})
    accepted, note, url = asyncio.run(verify_student_login_surface(HOME, use_proxy=False))
    assert accepted is True and url == login
    assert "login link opened and confirmed" in note


def test_link_whose_redirect_was_blocked_is_never_rendered(monkeypatch) -> None:
    """The fetch refuses an internal redirect; nothing may then load the link
    in a browser that would follow that redirect unchecked."""
    link = "https://www.example.ac.in/login"

    async def fetch(*_a, **_k):
        return 0, "http://169.254.169.254/", "", "redirect: address is not on the public internet"

    async def render(*_a, **_k):
        raise AssertionError("a link target must never be rendered")

    _site(monkeypatch, {HOME: _home(f'<a href="{link}">Student Login</a>')})
    monkeypatch.setattr(G, "_fetch_html", fetch)
    monkeypatch.setattr(G, "_render_html", render)
    accepted, note, _url = asyncio.run(verify_student_login_surface(HOME, use_proxy=False))
    assert accepted is False
    assert G.NON_PUBLIC_NOTE in note

"""Click-through: which controls are clicked, what the clicks add up to, and
when the hybrid verifier reaches for it. The browser itself is not started."""

import asyncio

import pytest

from agent import guardrails as G
from agent.click_through import (
    ClickResult,
    decide,
    is_toggle,
    pick_controls,
    pick_sections,
    pick_toggles,
    publishable_destination,
    score_control,
)
from agent.guardrails import (
    BUTTON_UNOPENED_NOTE,
    LINK_UNCONFIRMED_NOTE,
    _click_could_settle,
    verify_student_login_surface,
    verify_student_login_surface_hybrid,
)

HOME = "https://www.example.ac.in/"


def _c(i, text, href="", tag="a", visible=True, label="", cls=""):
    return {"i": i, "text": text, "href": href, "tag": tag, "visible": visible,
            "label": label, "cls": cls}


# --------------------------------------------------------------------------- #
#  choosing what to click                                                      #
# --------------------------------------------------------------------------- #


def test_toggle_is_a_control_that_stays_on_the_page() -> None:
    assert is_toggle(_c(0, "Login", ""), HOME) is True
    assert is_toggle(_c(0, "Login", "javascript:void(0)"), HOME) is True
    assert is_toggle(_c(0, "Login", HOME + "#login"), HOME) is True
    assert is_toggle(_c(0, "Login", HOME + "login"), HOME) is False
    # A nav link to the page itself is neither: clicking it only reloads the
    # page (central.edu.py/alumnos lists "Alumnos" in its own menu).
    assert is_toggle(_c(0, "Alumnos", HOME), HOME) is False
    assert score_control(_c(0, "Student Login", HOME), HOME) <= 0
    assert pick_toggles([_c(0, "Students", HOME)], HOME) == []


@pytest.mark.parametrize("control", [
    _c(0, "Student Login", "https://erp.example.ac.in/login"),
    _c(0, "Portal do Aluno", "https://aluno.example.br/"),
    _c(0, "Öğrenci Girişi", "https://obs.example.edu.tr/"),
    _c(0, "Login", "", tag="button"),
    _c(0, "", "https://example.ac.in/student/login"),        # icon-only link
    _c(0, "CMS/LMS", "https://scottishcms.edu.in/"),         # scottishchurch.ac.in
    _c(0, "Campus Virtual", "https://virtual.example.edu.py/"),
])
def test_student_sign_in_controls_are_worth_clicking(control: dict) -> None:
    assert score_control(control, HOME) > 0


@pytest.mark.parametrize("control", [
    _c(0, "Staff Login", "https://example.ac.in/staff"),
    _c(0, "Recruiter login", "https://example.ac.in/recruiter_login.php"),
    _c(0, "Admission Login", "https://exampleadm.samarth.edu.in/index.php/site/login"),
    _c(0, "Login", "https://example.ac.in/wp-login.php"),
    _c(0, "Login with Facebook", "https://www.facebook.com/login"),
    _c(0, "Portal", "https://example.ac.in/"),               # no student wording
    _c(0, "About us", "https://example.ac.in/about"),
    _c(0, "Email us", "mailto:info@example.ac.in"),
])
def test_other_controls_are_not_clicked(control: dict) -> None:
    assert score_control(control, HOME) <= 0


def test_picks_are_ranked_and_one_per_destination() -> None:
    """telanganauniversity.ac.in: "Student login" must beat a generic Login."""
    controls = [
        _c(1, "Login", "https://www.example.ac.in/login.php"),
        _c(2, "Staff login", "https://www.example.ac.in/staff_login.php"),
        _c(3, "Student login", "https://www.example.ac.in/student_login.php"),
        _c(4, "Student Login", "http://example.ac.in/student_login.php/"),   # same page
        _c(5, "Login", "", tag="button"),
    ]
    picks = pick_controls(controls, HOME)
    assert [p["i"] for p in picks] == [3, 1, 5]
    assert [p["i"] for p in pick_controls(controls, HOME, limit=1)] == [3]


def test_menus_are_opened_hamburger_first_and_staff_menus_are_left_shut() -> None:
    controls = [
        _c(1, "Students", "", tag="a"),
        _c(2, "", "", tag="button", label="Toggle navigation", cls="navbar-toggler"),
        _c(3, "Faculty", "", tag="a"),
        _c(4, "Login", HOME + "#", tag="a"),
        _c(5, "Students", "", tag="a", visible=False),
        _c(6, "Student Portal", "https://erp.example.ac.in/"),     # a link, not a menu
    ]
    assert [t["i"] for t in pick_toggles(controls, HOME)] == [2, 1, 4]


def test_section_pages_are_same_site_student_pages() -> None:
    controls = [
        _c(1, "Students", "https://www.example.ac.in/students"),
        _c(2, "Student Corner", "https://www.example.ac.in/student-corner#top"),
        _c(3, "Students", "https://other.example.org/students"),          # another site
        _c(4, "Staff", "https://www.example.ac.in/staff"),
        _c(5, "Student Admission", "https://www.example.ac.in/admission/students"),
        _c(6, "Students", HOME),                                          # this page
        _c(7, "Student Life at Example", "https://www.example.ac.in/life"),
        _c(8, "Outstanding Former Students", "https://www.example.ac.in/former.php"),
        _c(9, "Students", "https://www.example.ac.in/achievements-students.php"),
        _c(10, "Students", "https://www.example.ac.in/hidden", visible=False),
    ]
    # Shortest label first: the plain menu entry beats a longer headline.
    assert pick_sections(controls, HOME) == [
        "https://www.example.ac.in/students",
        "https://www.example.ac.in/student-corner",
    ]


# --------------------------------------------------------------------------- #
#  what the clicks add up to                                                   #
# --------------------------------------------------------------------------- #


def _click(control, href, dest, verdict="student", changed=True, reason="form",
           for_students=False):
    return {"control": control, "href": href, "dest": dest, "verdict": verdict,
            "reason": reason, "url_changed": changed, "for_students": for_students}


def test_one_login_at_a_new_address_is_published() -> None:
    login = "https://erp.example.ac.in/student/login"
    result = decide([
        _click("Student Login", login, login + "?utm_source=home"),
        _click("Login", "https://example.ac.in/x", "https://example.ac.in/x",
               verdict="wrong", reason="non-student audience only"),
    ], HOME)
    assert result.outcome == "one"
    assert result.url == login
    assert "Student Login" in result.note


def test_two_controls_reaching_the_same_login_count_once() -> None:
    result = decide([
        _click("Login", "http://erp.example.ac.in/login", "https://erp.example.ac.in/login/"),
        _click("Student Login", "https://erp.example.ac.in/login", "https://erp.example.ac.in/login"),
    ], HOME)
    assert result.outcome == "one"


def test_several_different_logins_publish_the_best_one() -> None:
    """Never the landing page: the best login reached is published."""
    result = decide([
        _click("Library login", "https://opac.example.ac.in/login",
               "https://opac.example.ac.in/login"),
        _click("Student Portal", "https://erp.example.ac.in/login", "https://erp.example.ac.in/login"),
        _click("LMS", "https://lms.example.ac.in/login/index.php",
               "https://lms.example.ac.in/login/index.php"),
    ], HOME)
    assert result.outcome == "one" and result.url == "https://erp.example.ac.in/login"
    assert "best of 3" in result.note
    assert len(result.destinations) == 3


def test_the_one_student_labelled_login_wins_over_a_plain_login() -> None:
    """niu.edu.in: "Student Login" and a plain "LOGIN" lead to different forms."""
    student = "https://accsoft.niu.edu.in/accsoft_niu/studentlogin.aspx"
    result = decide([
        _click("LOGIN", "http://accsoft.niu.edu.in/Accsoft_NIU/Login.aspx",
               "http://accsoft.niu.edu.in/Accsoft_NIU/Login.aspx"),
        _click("Student Login", student, student, for_students=True),
    ], "https://niu.edu.in/")
    assert result.outcome == "one" and result.url == student
    assert "only student-labelled" in result.note


def test_two_student_labelled_logins_publish_the_first_offered() -> None:
    result = decide([
        _click("Student Portal", "https://erp.example.ac.in/login",
               "https://erp.example.ac.in/login", for_students=True),
        _click("Student LMS", "https://lms.example.ac.in/login/index.php",
               "https://lms.example.ac.in/login/index.php", for_students=True),
    ], HOME)
    assert result.outcome == "one" and result.url == "https://erp.example.ac.in/login"


def test_login_that_opens_without_changing_the_address_keeps_the_page() -> None:
    result = decide([_click("Login", "", HOME, changed=False)], HOME)
    assert result.outcome == "modal" and result.url == ""


def test_a_pop_up_login_beside_a_linked_login_keeps_the_page() -> None:
    """The page has a login of its own, so the page itself is a login."""
    result = decide([
        _click("Login", "", HOME, changed=False),
        _click("LMS", "https://lms.example.ac.in/login", "https://lms.example.ac.in/login"),
    ], HOME)
    assert result.outcome == "modal" and result.page == HOME
    assert result.destinations == ["https://lms.example.ac.in/login"]


def test_the_students_page_is_kept_when_its_buttons_are_the_way_in() -> None:
    """central.edu.py: the homepage has no login; /alumnos has three "Ingresar"."""
    home, section = "https://central.edu.py/", "https://central.edu.py/alumnos"
    clicks = [
        {**_click("Ingresar", "https://app.central.edu.py/",
                  "https://app.central.edu.py/#/auth/login"), "found_on": section},
        {**_click("Ingresar", "http://www.virtual.central.edu.py/",
                  "https://virtual.central.edu.py/auth"), "found_on": section},
    ]
    result = decide(clicks, home)
    assert result.outcome == "one" and result.url == "https://app.central.edu.py/#/auth/login".split("#")[0]
    # A pop-up on the section page keeps the section page too.
    modal = decide([{**_click("Ingresar", "", section, changed=False),
                     "found_on": section}], home)
    assert modal.outcome == "modal" and modal.page == section


def test_no_student_login_behind_any_control() -> None:
    result = decide([
        _click("Login", "https://x.example.ac.in/", "https://x.example.ac.in/",
               verdict="unknown", reason="no password input"),
    ], HOME)
    assert result.outcome == "none"
    assert "none led to a student login" in result.note
    assert decide([], HOME).note == "no login control to click"


def test_click_passes_the_control_label_to_the_classifier(monkeypatch) -> None:
    """The label is what lets "Student Login" -> outside provider count."""
    from agent import click_through as CT
    seen: list[tuple] = []

    def classify(target, final, html, entry, label=""):
        seen.append((target, final, entry, label))
        return "student", "form"

    async def follow(_context, _page, control, _base):
        return {"control": control["text"], "href": control["href"],
                "dest": "https://sso.identity-host.net/signin", "html": "<html/>",
                "url_changed": True, "for_students": True}

    async def collect(_page):
        return [_c(1, "Student Login", "https://www.example.ac.in/login")]

    async def nothing(*_a, **_k):
        return None

    class Page:
        url = HOME

        async def wait_for_load_state(self, *_a, **_k):
            return None

    monkeypatch.setattr(G, "_classify_login_target", classify)
    monkeypatch.setattr(CT, "_follow", follow)
    monkeypatch.setattr(CT, "_collect", collect)
    monkeypatch.setattr(CT, "_open_menus", nothing)
    clicks = asyncio.run(CT._explore(None, Page(), HOME, HOME, 4))
    assert seen == [("https://www.example.ac.in/login",
                     "https://sso.identity-host.net/signin", HOME, "Student Login")]
    assert clicks[0]["verdict"] == "student" and "html" not in clicks[0]


def test_one_time_sso_address_is_never_published() -> None:
    sso = "https://lms.example.ac.in/sso/login"
    idp = "https://idp.example.net/login?SAMLRequest=abc&RelayState=x"
    assert publishable_destination(sso, idp) == sso
    # A button has no link to fall back on: nothing stable to publish.
    assert publishable_destination("", idp) == ""
    assert decide([_click("Login", "", idp)], HOME).outcome == "modal"
    assert decide([_click("Student Login", sso, idp)], HOME).url == sso


# --------------------------------------------------------------------------- #
#  when the hybrid verifier clicks                                             #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("note, expected", [
    ("no password input; no actionable rendered login/SSO control", True),
    (G.NO_STUDENT_CONTROL_NOTE, True),
    ("login link is not a student login: HTTP 404", True),
    ("admission portal: title/h1 names an admission cycle", False),
    ("non-student audience only", False),
    ("no student or academic audience evidence", False),
    ("render TimeoutError", False),
    ("e-mail address or credentials in URL", False),
])
def test_only_a_missing_login_is_worth_a_click(note: str, expected: bool) -> None:
    assert _click_could_settle(note) is expected


def _wire(monkeypatch, static, rendered, found):
    calls: list[str] = []

    async def fake_static(url, *_a, **_k):
        return static

    async def fake_rendered(url, *_a, **_k):
        calls.append("render")
        return rendered

    async def fake_click(url, country_hint):
        calls.append(f"click {url}")
        return found

    monkeypatch.setattr(G, "verify_student_login_surface", fake_static)
    monkeypatch.setattr(G, "verify_student_login_surface_rendered", fake_rendered)
    monkeypatch.setattr(G, "_click_through", fake_click)
    return calls


NO_LOGIN = "no password input; no actionable rendered login/SSO control"


def test_login_hidden_in_a_menu_is_found_by_clicking(monkeypatch) -> None:
    login = "https://www.example.ac.in/student_login.php"
    calls = _wire(monkeypatch, (False, NO_LOGIN, HOME), (False, NO_LOGIN, HOME),
                  ClickResult("one", login, "login reached by clicking 'Student login'"))
    accepted, note, url = asyncio.run(verify_student_login_surface_hybrid(HOME))
    assert accepted is True and url == login
    assert note.endswith("click-through")
    assert calls == ["render", f"click {HOME}"]


def test_page_is_kept_when_its_login_opens_on_the_page(monkeypatch) -> None:
    _wire(monkeypatch, (False, NO_LOGIN, HOME), (False, NO_LOGIN, HOME),
          ClickResult("modal", note="login opens on the page after a click"))
    accepted, _note, url = asyncio.run(verify_student_login_surface_hybrid(HOME))
    assert accepted is True and url == HOME


def test_a_button_never_opened_is_review_not_accept(monkeypatch) -> None:
    """Item 1.1: a sign-in button whose form was never seen is not a login."""
    _wire(monkeypatch, (False, NO_LOGIN, HOME), (False, NO_LOGIN, HOME),
          ClickResult("button", note="login button on the page, no form seen"))
    accepted, note, url = asyncio.run(verify_student_login_surface_hybrid(HOME))
    assert accepted is False and G.is_review_note(note)
    assert G.UNSEEN_LOGIN_NOTE in note


def test_students_page_is_published_when_clicking_says_so(monkeypatch) -> None:
    section = "https://www.example.ac.in/students"
    _wire(monkeypatch, (False, NO_LOGIN, HOME), (False, NO_LOGIN, HOME),
          ClickResult("modal", page=section, note="login opens on the page after a click"))
    assert asyncio.run(verify_student_login_surface_hybrid(HOME))[2] == section


def test_click_is_still_tried_when_chromium_could_not_load_the_page(monkeypatch) -> None:
    login = "https://erp.example.ac.in/login"
    calls = _wire(monkeypatch, (False, NO_LOGIN, HOME),
                  (False, "Unexpected error in _crawl_web: Page.goto: net::ERR", HOME),
                  ClickResult("one", login, "login reached by clicking 'Login'"))
    assert asyncio.run(verify_student_login_surface_hybrid(HOME))[2] == login
    assert calls[-1] == f"click {HOME}"
    # ...but not when the plain HTML never loaded either.
    calls = _wire(monkeypatch, (False, "TimeoutError", HOME),
                  (False, "render TimeoutError", HOME), ClickResult("one", login))
    assert asyncio.run(verify_student_login_surface_hybrid(HOME))[0] is False
    assert calls == ["render"]


def test_page_stays_rejected_when_clicking_finds_nothing(monkeypatch) -> None:
    _wire(monkeypatch, (False, NO_LOGIN, HOME), (False, NO_LOGIN, HOME),
          ClickResult("none", note="no login control to click"))
    accepted, note, _url = asyncio.run(verify_student_login_surface_hybrid(HOME))
    assert accepted is False
    assert "click-through: no login control to click" in note


def test_a_page_rejected_for_what_it_is_gets_no_click(monkeypatch) -> None:
    calls = _wire(monkeypatch, (False, "non-student audience only", HOME),
                  (False, "non-student audience only", HOME), ClickResult("one", "x"))
    assert asyncio.run(verify_student_login_surface_hybrid(HOME))[0] is False
    assert calls == ["render"]

    calls = _wire(monkeypatch, (False, "admission portal: URL admission token", HOME),
                  (False, "x", HOME), ClickResult("one", "x"))
    assert asyncio.run(verify_student_login_surface_hybrid(HOME))[0] is False
    assert calls == []


def test_button_only_page_is_upgraded_to_the_form_it_opens(monkeypatch) -> None:
    form = "https://erp.example.ac.in/login"
    accepted_note = f"rendered login/SSO authorisation control; student; {BUTTON_UNOPENED_NOTE}"
    calls = _wire(monkeypatch, (True, accepted_note, HOME), None,
                  ClickResult("one", form, "login reached by clicking 'Login'"))
    accepted, note, url = asyncio.run(verify_student_login_surface_hybrid(HOME))
    assert accepted is True and url == form
    assert calls == [f"click {HOME}"]          # no render needed first


def test_button_only_page_goes_to_review_when_the_click_finds_nothing(monkeypatch) -> None:
    """Item 1.1: a button that could not be clicked today is not evidence
    against the page, but not for it either: REVIEW, never ACCEPT."""
    accepted_note = f"control; student; {BUTTON_UNOPENED_NOTE}"
    _wire(monkeypatch, (True, accepted_note, HOME), None, ClickResult("none", note="x"))
    accepted, note, url = asyncio.run(verify_student_login_surface_hybrid(HOME))
    assert accepted is False and url == HOME
    assert G.is_review_note(note) and BUTTON_UNOPENED_NOTE in note


def test_unread_link_goes_to_chromium_then_to_a_click(monkeypatch) -> None:
    form = "https://app.example.ac.in/login"
    note = f"control; student; {LINK_UNCONFIRMED_NOTE}"
    calls = _wire(monkeypatch, (True, note, HOME),
                  (True, note + "; Chromium-rendered", HOME),
                  ClickResult("one", form, "login reached by clicking 'Login'"))
    accepted, _note, url = asyncio.run(verify_student_login_surface_hybrid(HOME))
    assert accepted is True and url == form
    assert calls == ["render", f"click {HOME}"]


def test_a_page_with_its_own_form_is_never_clicked(monkeypatch) -> None:
    calls = _wire(monkeypatch,
                  (True, "password, identifier, and sign-in control; student", HOME),
                  None, ClickResult("one", "https://elsewhere.example/"))
    assert asyncio.run(verify_student_login_surface_hybrid(HOME))[2] == HOME
    assert calls == []


def test_click_through_can_be_switched_off(monkeypatch) -> None:
    """With GENIE_CLICK_THROUGH=0 the verifier behaves exactly as before."""
    async def fake_static(url, *_a, **_k):
        return False, NO_LOGIN, HOME

    monkeypatch.setattr(G, "verify_student_login_surface", fake_static)
    monkeypatch.setattr(G, "verify_student_login_surface_rendered", fake_static)
    accepted, note, _url = asyncio.run(verify_student_login_surface_hybrid(HOME))
    assert accepted is False
    assert "click-through" not in note


def test_static_check_marks_a_button_only_page(monkeypatch) -> None:
    html = ("<html><h1>Student services</h1>"
            '<button onclick="openLogin()">Student Login</button></html>')

    class Content:
        async def read(self, _limit):
            return html.encode()

    class Response:
        status = 200
        url = HOME
        headers = {"Content-Type": "text/html"}
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

    monkeypatch.setattr("agent.guardrails.aiohttp.ClientSession", lambda **_k: Session())
    accepted, note, url = asyncio.run(verify_student_login_surface(HOME, use_proxy=False))
    assert accepted is True and url == HOME
    assert BUTTON_UNOPENED_NOTE in note

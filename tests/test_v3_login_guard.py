"""Focused checks for GenieV3's final student-login acceptance gate."""

from agent.guardrails import (
    _login_form_evidence,
    _student_audience_evidence,
    portal_url_allowed,
)
from agent.filters import LocalKnowledgeMatrixFilter


def test_rejects_documents_and_literal_ip_hosts() -> None:
    assert portal_url_allowed("https://example.edu/login.pdf")[0] is False
    assert portal_url_allowed("https://203.0.113.7/login")[0] is False
    assert portal_url_allowed("https://[2001:db8::7]/login")[0] is False


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


def test_accepts_student_and_lms_evidence() -> None:
    student_ok, _ = _student_audience_evidence("""
        <form><input name="roll_number"><input type="password"><button>Student login</button></form>
    """, "https://portal.example.edu/login")
    moodle_ok, _ = _student_audience_evidence("""
        <form><input name="username"><input type="password"><button>Log in</button></form>
    """, "https://moodle.example.edu/login/index.php")
    assert student_ok is True
    assert moodle_ok is True


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
    ranked = LocalKnowledgeMatrixFilter().filter_and_rank_links(links)
    assert {item["url"] for item in ranked} == {item["url"] for item in links}

"""Focused checks for GenieV3's final student-login acceptance gate."""

from agent.guardrails import (
    _login_form_evidence,
    _rendered_auth_control,
    _rendered_auth_control_target,
    _stable_verified_url,
    _student_audience_evidence,
    canonical_portal_url,
    portal_dedupe_key,
    portal_url_allowed,
)
from agent.filters import LocalKnowledgeMatrixFilter


def test_rejects_documents_and_literal_ip_hosts() -> None:
    assert portal_url_allowed("https://example.edu/login.pdf")[0] is False
    assert portal_url_allowed("https://203.0.113.7/login")[0] is False
    assert portal_url_allowed("https://[2001:db8::7]/login")[0] is False
    assert portal_url_allowed(
        "https://utelecon-directory.adm.u-tokyo.ac.jp/login")[0] is False


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

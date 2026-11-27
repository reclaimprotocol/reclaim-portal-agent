"""Precision fixes from the Tier A review (1,281 reported URLs, 568 SheerID orgs).

One section per fix ("Order of work" rows 1-4 of the review):

1. No ACCEPT without a seen login; publish the login that was found.
2. The login must belong to the organisation.
3. One key for one login, compared within one organisation and against what
   the organisation already has.
4. Never publish a one-time or identity-provider URL.

Review ids (A0105, ...) are rows of `portal_agent_decisions_reviewed_tierA.csv`.
"""

import asyncio
import base64

import pytest

from agent import guardrails as G
from agent.guardrails import (
    ACCEPT, ALREADY_HAVE, REJECT, REVIEW,
    canonical_portal_url, is_identity_provider_url,
    judge_student_login, org_branded, ownership_evidence, portal_dedupe_key,
    publishable_login_url,
)

FORM = ("<html><title>Student Login</title><form><input name='username'>"
        "<input type='password'><button type='submit'>Login</button></form></html>")


def _verifier(result):
    async def verify(url, **_kwargs):
        return result if result[2] else (result[0], result[1], url)
    return verify


def _no_network(monkeypatch):
    async def fetch(*_a, **_k):
        raise AssertionError("this decision must not need the network")
    monkeypatch.setattr(G, "_fetch_html", fetch)


def _pages(monkeypatch, pages: dict[str, tuple[int, str]]):
    """Serve ``url -> (status, html)``; anything else is a timeout."""
    fetched: list[str] = []

    async def fetch(url, *_a, **_k):
        fetched.append(url)
        if url in pages:
            status, html = pages[url]
            return status, url, html, ""
        return 0, url, "", "TimeoutError"
    monkeypatch.setattr(G, "_fetch_html", fetch)
    return fetched


def judge(url, verdict, **kwargs):
    kwargs.setdefault("vendor_roots", G.student_system_vendor_roots())
    return asyncio.run(judge_student_login(url, verifier=_verifier(verdict), **kwargs))


# --------------------------------------------------------------------------- #
#  1. No ACCEPT without a seen login; publish the login that was found          #
# --------------------------------------------------------------------------- #


def test_a_confirmed_login_link_on_another_host_is_published_not_the_landing(monkeypatch) -> None:
    """A0195 / A1188: "login link opened and confirmed", yet the landing page
    was published because the login lives on another host."""
    landing = "https://akademik.unpatti.ac.id/layanan-akademik/"
    login = "https://siakad.unpatti.ac.id/login"

    async def static(url, *_a, **_k):
        return (True, "rendered login/SSO authorisation control; student/academic "
                      "audience evidence; login link opened and confirmed", login)

    monkeypatch.setattr(G, "verify_student_login_surface", static)
    accepted, _note, url = asyncio.run(G.verify_student_login_surface_hybrid(landing))
    assert accepted is True and url == login


def test_an_entry_page_publishes_the_login_it_redirects_to() -> None:
    """Item 3.2: `/my/` redirects to the Moodle login; publish the login."""
    assert G._stable_verified_url(
        "https://lms.example.edu/my/", "https://lms.example.edu/login/index.php"
    ) == "https://lms.example.edu/login/index.php"


def test_a_login_never_seen_is_review_not_accept(monkeypatch) -> None:
    _no_network(monkeypatch)
    result = judge("https://www.example.edu/", (
        False, f"{G.REVIEW_PREFIX}{G.UNSEEN_LOGIN_NOTE} (login link not confirmed)", ""),
        official_domain="example.edu")
    assert result.decision == REVIEW and result.reason == "login_unseen"
    assert result.accepted is False and result.url == ""


def test_a_rejected_page_stays_rejected(monkeypatch) -> None:
    _no_network(monkeypatch)
    result = judge("https://www.example.edu/", (False, "no password input", ""),
                   official_domain="example.edu")
    assert result.decision == REJECT and result.as_tuple()[0] is False


def test_a_seen_login_on_the_orgs_domain_is_accepted(monkeypatch) -> None:
    _no_network(monkeypatch)
    login = "https://siakad.unpatti.ac.id/login"
    result = judge(login, (True, "password, identifier, and sign-in control", login),
                   official_domain="unpatti.ac.id")
    assert result.decision == ACCEPT and result.url == login
    assert result.ownership == "official"
    assert result.as_tuple() == (True, result.note, login)


# --------------------------------------------------------------------------- #
#  2. Ownership                                                                #
# --------------------------------------------------------------------------- #

VENDORS = G.student_system_vendor_roots()


def test_vendor_list_lives_in_the_knowledge_base() -> None:
    curated = {"instructure.com", "q10.com", "samarth.edu.in", "tcsion.com",
               "jacad.com.br", "linways.com", "icloudph.com", "mannesoftprime.com.br"}
    assert curated <= VENDORS
    # ... and the saas_infra_whitelist counts too.
    assert {"knimbus.com"} <= VENDORS


@pytest.mark.parametrize("url, org, domain", [
    # A0105: Cosmos Creative Academy's portal for Mangalayatan University.
    ("https://studentportal.ccaindia.com/login", "Mangalayatan University", "mangalayatan.edu.in"),
    # A0185: University of Calabar's result portal for UNICROSS.
    ("https://unicalexams.edu.ng/login", "University of Cross River State (UNICROSS)",
     "unicross.edu.ng"),
    # A0936: Universidad Veracruzana for Anáhuac Veracruz.
    ("https://dsia.uv.mx/miuv/escritorio/login.aspx",
     "Universidad Anáhuac Veracruz Campus Córdoba", "anahuac.mx"),
    # A0237: a Studocu profile for Universitas Pattimura.
    ("https://www.studocu.id/id/u/8601899?page=2", "Universitas Pattimura", "unpatti.ac.id"),
])
def test_another_organisations_site_is_not_owned(url, org, domain) -> None:
    relation, why = ownership_evidence(url, official_domain=domain, org_name=org,
                                       page_text="Student Portal Login", vendor_roots=VENDORS)
    assert relation == ""
    assert "neither the organisation's" in why


def test_another_universitys_samarth_tenant_is_not_owned() -> None:
    """A0421: CCS University's Samarth tenant reported for IIMT University."""
    relation, why = ownership_evidence(
        "https://ccsuniversity.samarth.edu.in/index.php/site/login",
        official_domain="iimtindia.net, vmsuniversity.in", org_name="Iimt University",
        page_text="Samarth eGov Login", vendor_roots=VENDORS)
    assert relation == "" and "samarth.edu.in" in why


def test_the_orgs_own_samarth_tenant_is_owned() -> None:
    relation, _why = ownership_evidence(
        "https://iimt.samarth.edu.in/index.php/site/login",
        official_domain="iimtindia.net", org_name="Iimt University",
        page_text="Samarth eGov Login", vendor_roots=VENDORS)
    assert relation == "vendor"


def test_a_vendor_page_branded_for_the_org_is_owned() -> None:
    url = "https://site6.q10.com/login?aplentid=7e530e5a-0d5d-4bfe-a7af-27bd57d9964c"
    assert ownership_evidence(url, official_domain="censa.edu.co", org_name="CENSA Medellín",
                              page_text="Iniciar sesión - CENSA", vendor_roots=VENDORS)[0] == "vendor"
    # A1052: the same Q10 shape for a different institute says nothing of CENSA.
    assert ownership_evidence(url, official_domain="censa.edu.co", org_name="CENSA Medellín",
                              page_text="Iniciar sesión", vendor_roots=VENDORS)[0] == ""


def test_branding_by_name_words_and_acronym() -> None:
    assert org_branded("https://faa.jacad.com.br/aluno-v2/login",
                       "Sistema Acadêmico Centro Universitário de Barra Mansa",
                       org_name="Centro Universitário De Barra Mansa (UBM)",
                       official_domain="ubm.br")[0] is True
    # A0589: the same vendor branded for UniFAA / Valença.
    assert org_branded("https://faa.jacad.com.br/aluno-v2/login",
                       "Login | Sistema Acadêmico SWA.JACAD UNIFAA Valença",
                       org_name="Centro Universitário De Barra Mansa (UBM)",
                       official_domain="ubm.br")[0] is False


def test_a_current_portal_proves_its_domain_but_a_vendor_host_only_its_tenant() -> None:
    known = ["https://pucpr.instructure.com/login/canvas", "https://portal.pucpr.edu.br/x"]
    assert ownership_evidence("https://pucpr.instructure.com/", known_urls=known,
                              vendor_roots=VENDORS)[0] == "known"
    assert ownership_evidence("https://other.pucpr.edu.br/login", known_urls=known,
                              vendor_roots=VENDORS)[0] == "known"
    assert ownership_evidence("https://other.instructure.com/", known_urls=known,
                              org_name="Pontifícia Universidade Católica do Paraná",
                              vendor_roots=VENDORS)[0] == ""


def test_a_login_reached_from_the_orgs_own_page_is_owned(monkeypatch) -> None:
    """Apex University's homepage links to its TCS iON page."""
    _no_network(monkeypatch)
    login = "https://g01.tcsion.com/SelfServices/"
    result = judge("https://www.apexuniversity.co.in/", (True, "x; login link opened and confirmed",
                                                         login),
                   official_domain="apexuniversity.co.in", org_name="Apex University",
                   vendor_roots=frozenset())
    assert result.decision == ACCEPT and result.ownership == "official_link"


def test_the_orgs_homepage_linking_to_a_vendor_tenant_proves_it(monkeypatch) -> None:
    """CENSA's own site links to its Q10 tenant, whose page names nobody."""
    tenant = "https://site6.q10.com/login?aplentid=7e530e5a"
    home = ("<html><a href='https://site6.q10.com/login?aplentid=7e530e5a'>"
            "Plataforma Q10</a></html>")
    _pages(monkeypatch, {tenant: (200, FORM), "https://censa.edu.co/": (200, home)})
    result = judge(tenant, (True, "password, identifier, and sign-in control", tenant),
                   official_domain="censa.edu.co", org_name="CENSA Medellín")
    assert result.decision == ACCEPT and result.ownership == "official_link"


def test_an_unowned_login_goes_to_review_with_a_reason(monkeypatch) -> None:
    login = "https://studentportal.ccaindia.com/login"
    _pages(monkeypatch, {"https://mangalayatan.edu.in/": (200, "<a href='/x'>x</a>")})
    result = judge(login, (True, "password, identifier, and sign-in control", login),
                   official_domain="mangalayatan.edu.in", org_name="Mangalayatan University")
    assert result.decision == REVIEW and result.reason == "wrong_org"
    assert result.url == login                   # kept for the reviewer
    accepted, note, _url = result.as_tuple()
    assert accepted is False and G.is_review_note(note) and "ownership not proven" in note


# --------------------------------------------------------------------------- #
#  3. One login, one key, per organisation                                     #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("first, second", [
    ("https://g3.unsl.edu.ar/g3/acceso", "https://g3.unsl.edu.ar/g3/acceso/login?ref=abc"),
    ("https://portal.afit.edu.ng/remstudent/", "https://portal.afit.edu.ng/remstudent/index"),
    ("https://webprosindia.com/vignanit/default.aspx?returnurl=%2fvignanit%2f",
     "https://webprosindia.com/vignanit/"),
    ("https://egov.uok.edu.in/eConduct/Account/Login?ReturnUrl=%2Feconduct%2Fstudent%2F",
     "https://egov.uok.edu.in/eConduct/Account/Login?returnURL=%2Feconduct"),
    ("https://portal.example.edu/login?Tenant=7&lang=en", "https://portal.example.edu/login?tenant=7"),
    ("https://portal.example.edu/login?a=1&b=2", "https://portal.example.edu/login?b=2&a=1"),
    ("https://portal.example.edu/student/home", "https://portal.example.edu/student/"),
    ("https://portal.example.edu/index.html", "https://portal.example.edu/"),
    ("https://lms.example.edu/login/index.php?next=%2Fmy%2F&logout=1",
     "https://lms.example.edu/login/"),
    # MasterSoft: every `iitms...` segment of a host is the same login.
    ("https://erp.sgtu.in/iitms5axbvrxqr8xpmewfa0vam3u1w_fg8w75fmhps?enc=tkndqf6uzt",
     "https://erp.sgtu.in/iitmsv9wN0kFOXxFdxcxKsqZ1bY_pdzy2F39D86JCSfxY47?enc=NW5BwWmEb"),
    # Session ids in the path or the query.
    ("https://ulicori.acamsys.com/student/login;jacamsysid=de4a6490bc3c119b",
     "https://ulicori.acamsys.com/student/login;jacamsysid=0368dfe7695f9283"),
    ("https://moodle.example.edu/login/index.php?sesskey=abc123", "https://moodle.example.edu/login/"),
])
def test_same_login_spellings_share_a_key(first, second) -> None:
    assert portal_dedupe_key(first) == portal_dedupe_key(second)


@pytest.mark.parametrize("first, second", [
    ("https://g3.unsl.edu.ar/g3/acceso", "https://g3.unsl.edu.ar/g3/inicio_alumno"),
    ("https://site6.q10.com/login?aplentid=aaa", "https://site6.q10.com/login?aplentid=bbb"),
    ("https://erp.sgtu.in/iitmsabc", "https://erp.other.in/iitmsabc"),
    ("https://portal.example.edu/student/login", "https://portal.example.edu/staff/login"),
])
def test_different_logins_keep_different_keys(first, second) -> None:
    assert portal_dedupe_key(first) != portal_dedupe_key(second)


def test_a_login_the_org_already_has_is_reported_as_such(monkeypatch) -> None:
    _no_network(monkeypatch)
    login = "https://portal.afit.edu.ng/remstudent/index"
    result = judge(login, (True, "password, identifier, and sign-in control", login),
                   official_domain="afit.edu.ng",
                   known_urls=["https://portal.afit.edu.ng/remstudent/ (inactive)".split()[0]])
    assert result.decision == ALREADY_HAVE and result.accepted
    assert result.reason == "already_have"


# --------------------------------------------------------------------------- #
#  4. One-time and identity-provider URLs                                      #
# --------------------------------------------------------------------------- #


def test_canonical_url_leaves_return_targets_and_session_ids_for_the_publish_step() -> None:
    assert canonical_portal_url(
        "https://lms.example.edu/login/index.php?ReturnUrl=%2F&utm_source=x&lang=es"
    ) == "https://lms.example.edu/login/index.php?ReturnUrl=%2F"
    for url in ("https://ulicori.acamsys.com/student/login;jacamsysid=de4a6490bc3c",
                "https://lms.example.edu/login/index.php?sesskey=x",
                "https://uauth.example.in/?code=abc"):
        assert canonical_portal_url(url) == url


def test_return_targets_are_dropped_only_when_a_fresh_reload_works(monkeypatch) -> None:
    seen = "https://egov.uok.edu.in/eConduct/Account/Login?ReturnUrl=%2Feconduct%2Fstudent%2F"
    clean = "https://egov.uok.edu.in/eConduct/Account/Login"
    _pages(monkeypatch, {clean: (200, FORM)})
    published, note = asyncio.run(publishable_login_url(seen, seen))
    assert published == clean and "return target removed" in note
    # No proof the bare URL works: publish the URL the verifier actually saw.
    _pages(monkeypatch, {})
    assert asyncio.run(publishable_login_url(seen, seen))[0] == canonical_portal_url(seen)


@pytest.mark.parametrize("key", ["sesskey", "sid", "token", "jwt", "ticket", "AuthState",
                                 "jacamsysid", "JSESSIONID", "code", "SAMLRequest"])
def test_volatile_parameters(key) -> None:
    assert G._is_volatile_auth_param(key)


@pytest.mark.parametrize("url, expected", [
    ("https://login.microsoftonline.com/abc/oauth2/authorize?client_id=x", True),
    ("https://accounts.google.com/o/oauth2/v2/auth?client_id=x", True),
    ("https://uni.okta.com/app/x/sso/saml", True),
    ("https://acceso.uvq.edu.ar/simplesaml/module.php/core/loginuserpass?AuthState=_x", True),
    ("https://sso.uni.edu/realms/students/protocol/openid-connect/auth?client_id=x", True),
    ("https://id.uni.edu/authenticationendpoint/login.do?sessionDataKey=x", True),
    ("https://lms.uni.edu/auth/oauth2/login.php?id=2", True),
    ("https://lms.uni.edu/login/index.php", False),
    ("https://portal.uni.edu/student/login", False),
])
def test_identity_provider_urls(url, expected) -> None:
    assert is_identity_provider_url(url) is expected


def test_simplesaml_authstate_publishes_the_portal_that_redirected(monkeypatch) -> None:
    """A0666: UNQ's Guaraní entry redirected to a one-time SimpleSAML URL.
    The entry is published, after a fresh reload shows it still signs in."""
    entry = "https://autogestion.uvq.edu.ar/unq/"
    idp = "https://acceso.uvq.edu.ar/simplesaml/module.php/core/loginuserpass?AuthState=_abc"
    fetched = _pages(monkeypatch, {entry: (200, FORM)})
    url, note = asyncio.run(publishable_login_url(entry, idp))
    assert url == entry and "fresh reload" in note
    assert fetched == [entry]
    _pages(monkeypatch, {entry: (404, "")})
    assert asyncio.run(publishable_login_url(entry, idp))[0] == ""


def test_moodle_oauth2_with_sesskey_publishes_the_portal(monkeypatch) -> None:
    """A0449: UNLP AU24 `/my/courses.php` -> `auth/oauth2/login.php?...&sesskey=`."""
    _pages(monkeypatch, {"https://www.au24-2026.econo.unlp.edu.ar/my/courses.php": (200, FORM)})
    url, _note = asyncio.run(publishable_login_url(
        "https://www.au24-2026.econo.unlp.edu.ar/my/courses.php",
        "https://www.au24-2026.econo.unlp.edu.ar/auth/oauth2/login.php?id=2&sesskey=abc"))
    assert url == "https://www.au24-2026.econo.unlp.edu.ar/my/courses.php"


def test_an_idp_url_with_no_portal_in_front_is_not_published(monkeypatch) -> None:
    _no_network(monkeypatch)
    idp = "https://login.microsoftonline.com/tenant/oauth2/authorize?client_id=x"
    assert asyncio.run(publishable_login_url(idp, idp))[0] == ""


def test_mysore_static_code_is_kept_because_a_fresh_reload_shows_the_login(monkeypatch) -> None:
    """Mysore's `?code=` is a static base64 client blob, not a one-time code."""
    blob = base64.b64encode(b'{"client":"uom","request_type":"Student"}').decode()
    url = canonical_portal_url(f"https://uauth.uni-mysore.ac.in/?code={blob}")
    fetched = _pages(monkeypatch, {url: (200, FORM)})
    published, note = asyncio.run(publishable_login_url(url, url))
    assert published == url and "fresh reload" in note
    assert fetched == [url]


def test_a_one_time_token_url_that_does_not_reload_is_never_published(monkeypatch) -> None:
    """A0331: FAVENI `login-automatico?token=` (an auto-login link)."""
    url = "https://ava.example.com.br/wp-json/api/v1/json/login-automatico?token=abc"
    expired = "<html><title>Erro</title><p>Token inválido</p></html>"
    _pages(monkeypatch, {url: (200, expired),
                         "https://ava.example.com.br/wp-json/api/v1/json/login-automatico":
                             (404, "")})
    assert asyncio.run(publishable_login_url(url, url))[0] == ""


def test_a_stripped_url_is_published_when_it_reloads(monkeypatch) -> None:
    url = "https://portal.example.edu/login?ticket=ST-123&tenant=7"
    _pages(monkeypatch, {url: (200, "<p>Session expired</p>"),
                         "https://portal.example.edu/login?tenant=7": (200, FORM)})
    published, note = asyncio.run(publishable_login_url(url, url))
    assert published == "https://portal.example.edu/login?tenant=7"
    assert "removed" in note


def test_mastersoft_iitms_paths_are_reusable_without_a_reload(monkeypatch) -> None:
    """MasterSoft `iitms...` paths look one-time but reload fine; no regex
    treats them as volatile."""
    _no_network(monkeypatch)
    url = "https://erp.sgtu.in/iitmsv9wN0kFOXxFdxcxKsqZ1bY_pdzy2F39D86JCSfxY47?enc=NW5BwWmEb"
    assert asyncio.run(publishable_login_url(url, url))[0] == url


def test_a_volatile_url_whose_reload_is_unknown_goes_to_review(monkeypatch) -> None:
    url = "https://portal.example.edu/login?token=abc"
    _pages(monkeypatch, {})                       # every reload times out
    result = judge(url, (True, "password, identifier, and sign-in control", url),
                   official_domain="example.edu")
    assert result.decision == REVIEW and result.reason == "volatile"


@pytest.mark.parametrize("status, html, expected", [
    (200, FORM, True),
    (200, "<p>Your session has expired</p>", False),
    (404, "", False),
    (403, "", None),
    (200, "<div id='app'></div>", None),
])
def test_reload_verdicts(monkeypatch, status, html, expected) -> None:
    url = "https://portal.example.edu/login"
    _pages(monkeypatch, {url: (status, html)})
    assert asyncio.run(G._reloads_as_login(url)) is expected


@pytest.mark.parametrize("url", [
    "https://ava.graduacao.unifaveni.com.br/membros/50420738843/course/",   # A0329
    "https://ava.example.com.br/wp-json/api/v1/json/login-automatico?token=abc",
    "https://raffles.ems.com.my/apex/f?p=412%3ALOGIN_DESKTOP%3A10442875380697%3A%3A",
])
def test_personal_and_auto_login_urls_are_never_published_as_is(monkeypatch, url) -> None:
    _pages(monkeypatch, {})
    assert asyncio.run(publishable_login_url(url, url))[0] == ""


def test_a_tenant_host_starting_with_the_orgs_domain_label_is_branded() -> None:
    """A0561: HSNC University's LMS on studentscenter.in."""
    assert ownership_evidence("https://hsnculms.studentscenter.in/login/index.php",
                              official_domain="hrcollege.edu, hsncu.edu.in",
                              org_name="Hassaram Rijhumal College of Commerce",
                              vendor_roots=VENDORS)[0] == "vendor"


@pytest.mark.parametrize("seen", [
    "https://portal.example.edu/login?sid=s3ss10n",
    "https://portal.example.edu/login;jsessionid=0A1B2C",
])
def test_a_session_id_dropped_by_canonicalising_still_needs_a_reload(monkeypatch, seen) -> None:
    """Only the session-bearing URL was seen: the cleaned one must reload too."""
    clean = "https://portal.example.edu/login"
    _pages(monkeypatch, {clean: (200, "<p>Your session has expired</p>")})
    assert asyncio.run(publishable_login_url(seen, seen))[0] == ""
    _pages(monkeypatch, {clean: (200, FORM)})
    published, note = asyncio.run(publishable_login_url(seen, seen))
    assert published == clean and "fresh reload" in note


def test_a_confirmed_link_with_a_session_id_is_reload_tested_end_to_end(monkeypatch) -> None:
    """PR review: the verifier must not drop `?sid=` before the publish step."""
    landing = "https://www.example.edu/students/"
    seen = "https://portal.example.edu/login?sid=per-visit-id"
    clean = "https://portal.example.edu/login"

    async def static(url, *_a, **_k):
        return True, "x; student; login link opened and confirmed", seen

    monkeypatch.setattr(G, "verify_student_login_surface", static)
    _pages(monkeypatch, {clean: (200, "<p>Your session has expired</p>"),
                         seen: (200, "<p>Your session has expired</p>"),
                         landing: (200, "<p>Students</p>")})
    result = asyncio.run(judge_student_login(landing, official_domain="example.edu"))
    assert result.decision == REVIEW and result.reason == "volatile"

    _pages(monkeypatch, {clean: (200, FORM)})
    result = asyncio.run(judge_student_login(landing, official_domain="example.edu"))
    assert result.decision == ACCEPT and result.url == clean


def test_a_link_published_in_place_of_a_one_time_destination_is_reload_tested(monkeypatch) -> None:
    """PR review: a clean `/sso` link whose click landed on `?state=...` is
    published instead of that one-time URL, so it must pass a fresh reload."""
    from agent.click_through import decide
    sso = "https://lms.example.edu/sso"
    landed = "https://lms.example.edu/oidc/callback?state=txn-123&code=abc"
    found = decide([{"control": "Student Login", "href": sso, "dest": landed,
                     "verdict": "student", "reason": "form", "url_changed": True}],
                   "https://www.example.edu/")
    assert found.outcome == "one" and found.url == sso
    assert G.REPLACED_ONE_TIME_NOTE in found.note

    verdict = (True, f"{found.note}; click-through", sso)
    _pages(monkeypatch, {sso: (200, "<p>Bad request: invalid state</p>")})
    result = judge("https://www.example.edu/", verdict, official_domain="example.edu")
    assert result.decision == REVIEW and result.reason == "volatile"

    _pages(monkeypatch, {sso: (200, FORM)})
    result = judge("https://www.example.edu/", verdict, official_domain="example.edu")
    assert result.decision == ACCEPT and result.url == sso


def test_a_url_seen_as_it_is_needs_no_reload(monkeypatch) -> None:
    _no_network(monkeypatch)
    assert not G._replaces_one_time("https://lms.example.edu/login",
                                    "https://lms.example.edu/login")
    assert not G._replaces_one_time("https://lms.example.edu/sso",
                                    "https://lms.example.edu/login")
    assert G._replaces_one_time("https://lms.example.edu/sso",
                                "https://login.microsoftonline.com/t/oauth2/authorize")


def test_a_click_landing_on_a_parameter_less_idp_screen_publishes_the_link(monkeypatch) -> None:
    """PR review: `/login` redirects to an Okta SAML path with no one-time
    parameters. The link is published, never the IdP or the homepage, and
    only after a fresh reload."""
    from agent.click_through import decide, publishable_destination
    login = "https://www.example.edu/login"
    okta = "https://example.okta.com/app/example_canvas/exk123/sso/saml"
    assert publishable_destination(login, okta) == login
    found = decide([{"control": "Login", "href": login, "dest": okta,
                     "verdict": "student", "reason": "form", "url_changed": True}],
                   "https://www.example.edu/")
    assert found.url == login and G.REPLACED_ONE_TIME_NOTE in found.note

    verdict = (True, f"{found.note}; click-through", login)
    _pages(monkeypatch, {})                       # the reload proves nothing
    assert judge("https://www.example.edu/", verdict,
                 official_domain="example.edu").decision == REVIEW
    _pages(monkeypatch, {login: (200, FORM)})
    result = judge("https://www.example.edu/", verdict, official_domain="example.edu")
    assert result.decision == ACCEPT and result.url == login


@pytest.mark.parametrize("url, expected", [
    ("https://app.central.edu.py/#/auth/login", "https://app.central.edu.py/#/auth/login"),
    ("https://portal.example.edu/#!/login", "https://portal.example.edu/#!/login"),
    ("https://portal.example.edu/login#main-content", "https://portal.example.edu/login"),
    ("https://portal.example.edu/cb#access_token=abc&state=x", "https://portal.example.edu/cb"),
])
def test_hash_routes_are_kept_other_fragments_dropped(url, expected) -> None:
    """PR review: a hash-routed SPA login must not be published as its root."""
    assert canonical_portal_url(url) == expected


def test_hash_routes_are_different_logins_but_anchors_are_not() -> None:
    assert portal_dedupe_key("https://app.example.edu/#/auth/login") != \
        portal_dedupe_key("https://app.example.edu/")
    assert portal_dedupe_key("https://app.example.edu/#/auth/login/") == \
        portal_dedupe_key("https://APP.example.edu/#!/auth/login")
    assert portal_dedupe_key("https://lms.example.edu/login#top") == \
        portal_dedupe_key("https://lms.example.edu/login")


def test_a_homepage_link_to_a_hosts_front_page_does_not_vouch_for_its_other_pages(monkeypatch) -> None:
    """PR review: linking to `third-party.example/` proves nothing about
    `third-party.example/another-org/login`."""
    login = "https://third-party.example/another-org/login"
    home = "<html><a href='https://third-party.example/'>Partner</a></html>"
    _pages(monkeypatch, {"https://example.edu/": (200, home)})
    assert asyncio.run(G._official_page_links_to(login, "example.edu")) is False
    _pages(monkeypatch, {"https://example.edu/": (200, f"<a href='{login}'>Login</a>")})
    assert asyncio.run(G._official_page_links_to(login, "example.edu")) is True


@pytest.mark.parametrize("key", ["sessionid", "session", "sessid", "jacamsysid", "ASPSESSIONIDQAQRBCRB"])
def test_query_session_ids_are_stripped_before_anything_is_published(monkeypatch, key) -> None:
    """PR review: `?sessionid=` must not be published when the clean URL works."""
    seen = f"https://portal.example.edu/login?{key}=SECRET"
    clean = "https://portal.example.edu/login"
    _pages(monkeypatch, {clean: (200, FORM), canonical_portal_url(seen): (200, FORM)})
    published, note = asyncio.run(publishable_login_url(seen, seen))
    assert published == clean and note.startswith("session id removed")


def test_a_kept_one_time_url_is_not_labelled_as_cleaned(monkeypatch) -> None:
    url = canonical_portal_url("https://uauth.example.in/?code=static-blob")
    _pages(monkeypatch, {url: (200, FORM)})
    published, note = asyncio.run(publishable_login_url(url, url))
    assert published == url and note.startswith("one-time-looking parameters kept")


# --------------------------------------------------------------------------- #
#  The one publish gate                                                        #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("key", ["access_token", "id_token", "jwt", "token", "ticket"])
def test_a_credential_in_the_url_is_never_published_even_when_it_reloads(monkeypatch, key) -> None:
    """PR review: a working reload does not make a token safe to hand out."""
    seen = f"https://portal.example.edu/login?{key}=SECRET"
    _pages(monkeypatch, {seen: (200, FORM)})          # only the token URL works
    assert asyncio.run(publishable_login_url(seen, seen))[0] == ""
    result = judge(seen, (True, "password, identifier, and sign-in control", seen),
                   official_domain="example.edu")
    assert result.decision == REVIEW and result.reason == "volatile"
    # Without the token it works: that address is published instead.
    _pages(monkeypatch, {seen: (200, FORM), "https://portal.example.edu/login": (200, FORM)})
    result = judge(seen, (True, "password, identifier, and sign-in control", seen),
                   official_domain="example.edu")
    assert result.decision == ACCEPT and result.url == "https://portal.example.edu/login"


def test_one_time_state_in_a_fragment_counts_as_one_time() -> None:
    """PR review: `#access_token=` is OAuth state even though it is not in the query."""
    url = "https://portal.example.edu/callback#access_token=SECRET&state=x"
    assert G._has_volatile_auth_state(url) and G._has_credential(url)
    assert G._has_volatile_auth_state("https://app.example.edu/#/cb?code=abc")
    assert not G._has_volatile_auth_state("https://app.example.edu/#/auth/login")
    assert not G._has_volatile_auth_state("https://lms.example.edu/login#main")


def test_a_click_landing_on_a_fragment_token_publishes_the_link_after_a_reload(monkeypatch) -> None:
    from agent.click_through import decide, publishable_destination
    link = "https://portal.example.edu/sso"
    landed = "https://portal.example.edu/callback#access_token=SECRET"
    assert publishable_destination(link, landed) == link
    found = decide([{"control": "Student Login", "href": link, "dest": landed,
                     "verdict": "student", "reason": "form", "url_changed": True}],
                   "https://www.example.edu/")
    assert found.url == link and G.REPLACED_ONE_TIME_NOTE in found.note
    verdict = (True, f"{found.note}; click-through", link)
    _pages(monkeypatch, {})
    assert judge("https://www.example.edu/", verdict,
                 official_domain="example.edu").decision == REVIEW
    _pages(monkeypatch, {link: (200, FORM)})
    assert judge("https://www.example.edu/", verdict,
                 official_domain="example.edu").url == link


def test_the_publish_gate_holds_whatever_the_verifier_returns(monkeypatch) -> None:
    """Defence in depth: even a verifier that hands back a credential URL as a
    plain accepted login cannot get it published."""
    bad = "https://portal.example.edu/login;jsessionid=ABC?sid=1"
    _pages(monkeypatch, {bad: (200, FORM)})
    result = judge(bad, (True, "password, identifier, and sign-in control", bad),
                   official_domain="example.edu")
    assert result.decision == REVIEW and result.url == ""


def test_a_hash_route_with_parameters_is_kept_and_published_as_seen(monkeypatch) -> None:
    """PR review: `#/login?next=/courses` must not collapse to the site root."""
    url = "https://portal.example.edu/#/login?next=/courses"
    assert canonical_portal_url(url) == url
    # No proof the route works without `next`: publish it exactly as seen.
    _pages(monkeypatch, {})
    assert asyncio.run(publishable_login_url(url, url))[0] == url
    # The route without the return target reloads as a login: publish that.
    _pages(monkeypatch, {"https://portal.example.edu/#/login": (200, FORM)})
    assert asyncio.run(publishable_login_url(url, url))[0] == "https://portal.example.edu/#/login"
    # Same login as the bare route; a different route is a different login.
    assert portal_dedupe_key(url) == portal_dedupe_key("https://portal.example.edu/#/login")
    assert portal_dedupe_key(url) != portal_dedupe_key("https://portal.example.edu/")


def test_cleaning_a_hash_routed_url_keeps_its_route(monkeypatch) -> None:
    seen = "https://app.example.edu/;jsessionid=ABC#/auth/login?code=xyz"
    clean = "https://app.example.edu/#/auth/login"
    assert G._strip_volatile_params(seen) == clean
    _pages(monkeypatch, {clean: (200, FORM)})
    published, _note = asyncio.run(publishable_login_url(seen, seen))
    assert published == clean


@pytest.mark.parametrize("part", [";PHPSESSID=SECRET", ";sessionid=SECRET", ";sid=SECRET",
                                  ";ASPSESSIONIDQAQRBCRB=SECRET", ";jsessionid=SECRET"])
def test_every_path_session_id_is_caught_and_never_published(monkeypatch, part) -> None:
    """PR review: `;PHPSESSID=` in the path was neither detected nor stripped."""
    seen = f"https://portal.example.edu/login{part}"
    assert G._has_credential(seen) and G._carries_session(seen)
    _pages(monkeypatch, {seen: (200, FORM)})          # only the session URL works
    result = judge(seen, (True, "password, identifier, and sign-in control", seen),
                   official_domain="example.edu")
    assert result.decision == REVIEW and result.url == ""
    assert portal_dedupe_key(seen) == portal_dedupe_key("https://portal.example.edu/login")


def test_path_session_names_match_the_query_session_names() -> None:
    for name in G._SESSION_ID_PARAMS:
        assert G._PATH_SESSION_RE.search(f"/login;{name}=x"), name



@pytest.mark.parametrize("seen", [
    "https://portal.example.edu/#/login;jsessionid=SECRET",
    "https://portal.example.edu/#!/login;PHPSESSID=SECRET?next=/x",
])
def test_a_session_id_inside_a_hash_route_is_caught_and_never_published(monkeypatch, seen) -> None:
    """PR review: `#/login;jsessionid=` hid a session id from the path check."""
    clean_route = "#!/login" if "#!" in seen else "#/login"
    clean = f"https://portal.example.edu/{clean_route}"
    assert G._has_credential(seen) and G._carries_session(seen)
    assert G._strip_volatile_params(seen) == clean
    assert portal_dedupe_key(seen) == portal_dedupe_key(clean)
    _pages(monkeypatch, {seen: (200, FORM)})          # only the session URL works
    result = judge(seen, (True, "password, identifier, and sign-in control", seen),
                   official_domain="example.edu")
    assert result.decision == REVIEW and result.url == ""
    _pages(monkeypatch, {clean: (200, FORM)})
    result = judge(seen, (True, "password, identifier, and sign-in control", seen),
                   official_domain="example.edu")
    assert result.decision == ACCEPT and result.url == clean

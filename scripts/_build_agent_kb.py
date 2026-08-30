#!/usr/bin/env python3
"""Build `agent_knowledge_base.json` — Genie-V3's static knowledge base.

Everything here is derived from evidence we already hold, never invented:

  * `KNOWN_SHARED_PLATFORM_PATTERNS` (90 entries in agent/config.py)
  * the 2026-08-29 mining of ALL six corpora (both CONFIDENTIAL_Provider
    Activation sheets, JulyBatch, August4000, Indian Universities, Bangladesh):
    every portal URL whose registrable root differs from the org's own email
    domain, kept when seen for 2+ DISTINCT institutions
  * the Category column of ~8,900 human-classified portal rows, which is where
    the category taxonomy comes from — it is observed, not designed
  * the reject rules already distilled into agent/magic.py and
    scripts/_run_portals_review.py from ~94 human review comments

Re-run this whenever the corpus grows; it is deterministic.
"""
from __future__ import annotations
import json, re, sys
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try:
    from dotenv import load_dotenv; load_dotenv(ROOT / ".env")
except Exception:  # noqa: BLE001
    pass
from agent.stages.discovery import KNOWN_SHARED_PLATFORM_PATTERNS as KNOWN  # noqa: E402

SP = Path("/private/tmp/claude-501/-Users-mrunomi-projects-reclaim-portal-agent/"
          "20fa4b23-fcf2-4415-89e2-ac66b5f3f0ab/scratchpad")
mined = json.loads((SP / "kb_vendors_merged.json").read_text()) if (SP / "kb_vendors_merged.json").exists() else {}

# --------------------------------------------------------------------------- #
# Roots that are infrastructure, not a student portal vendor. They appear in the
# corpus only because a portal happens to sit behind them (CDN, WAF, PaaS host,
# URL shortener). Whitelisting these would accept anything hosted anywhere.
# --------------------------------------------------------------------------- #
INFRA_EXCLUDE = {
    "cloudflareaccess.com", "cloudflare.com", "sucuri.net", "perfdrive.com",
    "azurewebsites.net", "vercel.app", "netlify.app", "herokuapp.com",
    "firebaseapp.com", "amazonaws.com", "cloudfront.net", "googleapis.com",
    "gstatic.com", "akamai.net", "wixsite.com", "weebly.com", "blogspot.com",
    "wordpress.com", "in.net", "bank.in", "odoo.com", "google.com",
    "microsoft.com", "microsoftonline.com", "office.com", "office365.com",
    "live.com", "outlook.com", "apple.com", "facebook.com", "linkedin.com",
    "youtube.com", "bit.ly", "tinyurl.com", "linktr.ee",
}
# Multi-org roots that are real, but are NOT an enrolled-student login.
NOT_A_STUDENT_PORTAL = {
    "upago.cl": "payment gateway (pay.upago.cl/payment/…)",
    "jntuksdc.co.in": "degree verification, not a student login",
    "oucealumni.org": "alumni association",
    "easebuzz.in": "payment gateway",
    "okiedokiepay.com": "payment gateway",
    "enrollonline.co.in": "admissions/enrolment",
    "ucanapply.com": "admissions application",
}

# Category assignments for mined roots, read off their evidence URLs.
CATEGORY = {
    # --- India: SIS / ERP / student portal ---
    "vmedulife.com": ("Student Portal", "/public/auth/#/login/{slug}", True),
    "myclassboard.com": ("Student Portal", "/Account/Login", False),
    "iitms.co.in": ("Student Portal", None, False),
    "osdes.in": ("Student Portal", None, False),
    "mgmuhsonline.in": ("Student Portal", "/stud_login.php", False),
    "muhsonline.net": ("Student Portal", None, False),
    "webprosindia.com": ("Student Portal", None, False),
    "unitedgn.in": ("Student Portal", "/Login.aspx", False),
    "uhsap.in": ("Student Portal", "/studentlogin/", False),
    "uuems.in": ("Student Portal", None, False),
    "studentscenter.in": ("Student Portal", "/login", False),
    "soaportals.com": ("Student Portal", "/StudentPortalSOA/", False),
    "sgtu.in": ("Student Portal", None, False),
    "sgterp.org": ("Student Portal", "/sgterp/login", False),
    "ppuponline.in": ("Student Portal", None, False),
    "onmark.co.in": ("Student Portal", None, False),
    "octopod.co.in": ("Student Portal", None, False),
    "beessoftware.cloud": ("Student Portal", "/studentselfservice", False),
    "heraizen.com": ("Student Portal", None, False),
    "juno.org.in": ("Student Portal", None, False),
    "samphireitsolutions.com": ("Student Portal", None, False),
    "polytropicsystem.in": ("Student Portal", None, False),
    "rightbrainstechnology.com": ("Student Portal", None, False),
    "enovasolutions.com": ("Student Portal", None, False),
    "emperor-solutions.com": ("Student Portal", None, False),
    "integratededucation.pwc.in": ("Student Portal", "/connectportal/Owner/Login", False),
    "pwc.in": ("Student Portal", "/connectportal/Owner/Login", False),
    "lpuonline.com": ("LMS", None, False),
    "makaut.online": ("Student Portal", None, False),
    "msbte.co.in": ("Exam/Results", None, False),
    "mdsuexam.org": ("Exam/Results", "/login", False),
    "nbuexams.net": ("Exam/Results", "/login.php", False),
    "shekhauniexam.in": ("Exam/Results", "/login_college.aspx", False),
    "jisexam.org": ("Exam/Results", "/ExamSystemNew/forms/frmLogin.aspx", False),
    "ptudocs.com": ("Exam/Results", None, False),
    "uceou.in": ("Student Portal", None, False),
    "onlineregistrationwbsu.com": ("Fees", None, False),
    "feepayr.com": ("Fees", None, False),
    # --- Brazil ---
    "techne.com.br": ("Student Portal", None, False),
    "portaledu.com.br": ("Student Portal", "/FrameHTML/web/app/edu/PortalEducacional/login", False),
    "lyceum.com.br": ("Student Portal", "/AOnline3/#/login", True),
    "portalava.com.br": ("LMS", "/login", False),
    "mannesoftprime.com.br": ("Student Portal", "/webaluno/", False),
    "tutory.com.br": ("LMS", "/login.php", False),
    "plataformatutory.com.br": ("LMS", None, False),
    "platosedu.io": ("LMS", "/v2/lms/login", False),
    "principia.net": ("Student Portal", "/auth", False),
    "minhabiblioteca.com.br": ("Library", "/Login.aspx", False),
    # --- LatAm ---
    "academic.lat": ("Student Portal", "/Autenticacion.aspx", False),
    "trytoku.com": ("LMS", None, False),
    "learnsquare.co": ("LMS", "/login", False),
    # --- global LMS hosting ---
    "mrooms.net": ("LMS", "/login/index.php", False),
    "moodle.com": ("LMS", "/login/index.php", False),
    "moodle.org": ("LMS", "/login/index.php", False),
    "lmstopserve.com": ("LMS", "/login/index.php", False),
    "auchiefslms.com": ("LMS", "/login/index.php", False),
    "simpleacademy.tech": ("LMS", "/login", False),
    "openathens.net": ("Library", "/auth", False),
    "mydsi.org": ("Student Portal", None, False),
}
# Geography, for the region packs and proxy exit selection.
GEO = {
    ".in": "IN", ".co.in": "IN", ".ac.in": "IN", ".org.in": "IN",
    ".com.br": "BR", ".br": "BR", ".mx": "MX", ".cl": "CL", ".com.ar": "AR",
    ".pe": "PE", ".co": "CO", ".uy": "UY", ".lat": "LATAM", ".ph": "PH",
    ".ng": "NG", ".ke": "KE", ".za": "ZA", ".bd": "BD", ".pk": "PK",
    ".id": "ID", ".my": "MY", ".vn": "VN", ".lk": "LK", ".np": "NP",
}
EXPLICIT_GEO = {
    "jacad.com.br": "BR", "ulife.com.br": "BR", "sereduc.com": "BR",
    "grupoa.education": "BR", "kroton.com.br": "BR", "anhanguera.com": "BR",
    "cloudtotvs.com.br": "BR", "techne.com.br": "BR", "lyceum.com.br": "BR",
    "platosedu.io": "BR", "principia.net": "BR",
    "eclass.com": "CL", "servoescolar.mx": "MX", "academic.lat": "LATAM",
    "arellanolms.com": "PH", "tssinclms.com": "PH", "auchiefslms.com": "PH",
    "lmstopserve.com": "PH", "waeup.org": "NG", "safsrms.com": "NG",
    "tcsion.com": "IN", "dhi-edu.com": "IN", "heraizen.com": "IN",
    "camudigitalcampus.com": "IN", "myclassboard.com": "IN",
    "mrooms.net": "GLOBAL", "moodle.com": "GLOBAL", "moodle.org": "GLOBAL",
    "openathens.net": "GLOBAL", "learnsquare.co": "GLOBAL",
    "instructure.com": "GLOBAL", "blackboard.com": "GLOBAL",
}


# The legacy 67 entries in config.py were mined from the India corpus alone
# (its own comment says so), so an .in-less root like digiicampus.com is still
# an Indian vendor. Defaulting those to GLOBAL would be wrong.
GLOBAL_LEGACY = {"moodle.live", "instructure.com", "blackboard.com", "knimbus.com"}


def geo_of(root: str, in_code: bool = False) -> str:
    if root in EXPLICIT_GEO:
        return EXPLICIT_GEO[root]
    for suf, g in sorted(GEO.items(), key=lambda x: -len(x[0])):
        if root.endswith(suf):
            return g
    if in_code and root not in GLOBAL_LEGACY:
        return "IN"
    return "GLOBAL"


# The Category column is free text and drifted over time; collapse to the
# canonical taxonomy actually used by reviewers.
CATEGORY_NORMALIZE = {
    "Fee Portal": "Fees", "Tuition/Fees": "Fees",
    "Examination Portal": "Exam/Results",
    "LMS/Moodle": "LMS",
    "Student Information System (SIS)/ERP": "Student Information System",
    "Central SSO": "SSO",
}


def path_of(url: str) -> str | None:
    try:
        s = urlsplit(url)
        p = (s.path or "") + (("#" + s.fragment) if s.fragment else "")
        return p if p and p != "/" else None
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------- #
# 1. saas_infra_whitelist                                                      #
# --------------------------------------------------------------------------- #
whitelist = []
for root, meta in sorted(mined.items()):
    if root in INFRA_EXCLUDE or root in NOT_A_STUDENT_PORTAL:
        continue
    in_code = bool(meta.get("in_code"))
    cat = meta.get("category")
    tpath = meta.get("tenant_path") or meta.get("canonical_path")
    hashr = bool(meta.get("hash_routed"))
    if not cat and root in CATEGORY:
        cat, tp2, hashr = CATEGORY[root]
        tpath = tpath or tp2
    ex = meta.get("examples") or []
    if not tpath and ex:
        tpath = path_of(ex[0])
    if not cat:
        blob = " ".join(ex).lower() + " " + root
        if re.search(r"/login/index\.php|moodle|/course|aula|ava\.|lms", blob):
            cat = "LMS"
        elif re.search(r"exam|result|marks", blob):
            cat = "Exam/Results"
        elif re.search(r"fee|pay|tuition", blob):
            cat = "Fees"
        elif re.search(r"biblio|library|libro", blob):
            cat = "Library"
        else:
            cat = "Student Portal"
    cat = CATEGORY_NORMALIZE.get(cat, cat)
    orgs = int(meta.get("orgs") or 0)
    whitelist.append({
        "root": root,
        "category": cat,
        "geo": geo_of(root, in_code),
        "tenant_path": tpath,
        "hash_routed": hashr or None,
        "institutions_observed": orgs,
        "confidence": "confirmed" if (in_code or orgs >= 5) else "candidate",
        "in_legacy_config": in_code,
        "evidence": ex[:2],
    })
whitelist.sort(key=lambda e: (-e["institutions_observed"], e["root"]))

# --------------------------------------------------------------------------- #
# 2. relevance_classification_keywords                                         #
#    Categories are the ones actually used by human reviewers across ~8,900     #
#    classified rows (counts noted), not a designed taxonomy.                   #
# --------------------------------------------------------------------------- #
relevance = {
    "_note": ("Categories observed in the human-reviewed Category column across "
              "~8,900 rows. `url` matches the URL/host, `text` matches visible "
              "page text/anchor text in any language, `platform` matches "
              "vendor/product fingerprints. A URL may carry several categories "
              "(the corpus uses 'Student Portal|LMS' style compounds)."),
    "Student Portal": {
        "observed_rows": 3564,
        "aliases": ["UMS", "MIS", "SIS", "ERP", "Student Information System",
                    "Self Service", "Academic Portal", "Campus Portal"],
        "url": ["student", "students", "estudiante", "alumno", "alumnos", "aluno",
                "discente", "portal", "myportal", "selfservice", "self-service",
                "studentportal", "ums", "mis", "sis", "erp", "academic",
                "academico", "académico", "acad", "campus", "onlineservices",
                "studentzone", "mystudent", "stud", "sturec", "autogestion",
                "guarani", "siu", "webaluno", "portaldoaluno", "alunoonline"],
        "text": ["student login", "student portal", "portal do aluno",
                 "área do aluno", "portal del estudiante", "acceso alumnos",
                 "autogestión", "student self service", "campus virtual",
                 "sistema académico", "sistema acadêmico", "student information",
                 "छात्र लॉगिन", "विद्यार्थी", "sinh viên", "mahasiswa",
                 "estudante", "matrícula", "enrolled student"],
        "platform": ["samarth", "mastersoft", "vmedulife", "camu", "linways",
                     "etlab", "digiicampus", "jacad", "totvs", "rm", "lyceum",
                     "tcsion", "ulife", "academic.lat"],
    },
    "LMS": {
        "observed_rows": 2806,
        "aliases": ["Learning Management System", "VLE", "e-Learning", "AVA"],
        "url": ["lms", "moodle", "canvas", "blackboard", "elearning", "e-learning",
                "learn", "learning", "classroom", "vle", "ava", "ead",
                "campusvirtual", "aulavirtual", "aula", "webaula", "cursos",
                "course", "d2l", "brightspace", "schoology", "sakai", "chamilo",
                "edmodo", "mrooms", "/login/index.php"],
        "text": ["moodle", "canvas", "blackboard", "e-learning", "ambiente virtual",
                 "aula virtual", "entorno virtual", "learning management",
                 "sala de aula", "mis cursos", "meus cursos", "my courses"],
        "platform": ["moodle", "instructure", "blackboard", "d2l", "chamilo",
                     "sakai", "mrooms", "arellanolms", "tssinclms"],
    },
    "SSO": {
        "observed_rows": 679,
        "aliases": ["Central SSO", "Single Sign-On", "Federated login", "IdP login"],
        "url": ["sso", "cas", "idp", "adfs", "shibboleth", "simplesaml", "oauth",
                "oauth2", "openid", "connect", "auth", "authenticate", "login",
                "signin", "accounts", "identity", "keycloak", "realms"],
        "text": ["single sign", "sign in with", "iniciar sesión", "central login",
                 "institutional login", "acesso unificado"],
        "platform": ["shibboleth", "cas", "adfs", "keycloak", "azure ad",
                     "okta", "onelogin", "openathens"],
        "caution": ("A tenant SSO login IS a valid portal when it fronts student "
                    "services, but a bare IdP entityID / SAML metadata URL is "
                    "NEVER a portal — see compliance_exclusion_blacklist."),
    },
    "Student Information System": {
        "observed_rows": 276,
        "aliases": ["SIS", "ERP", "UMS", "MIS", "Academic ERP"],
        "url": ["sis", "ums", "mis", "erp", "academia", "registrar", "records",
                "enrollment", "matricula", "matrícula", "gestion", "gestao"],
        "text": ["information system", "sistema de gestión", "sistema de gestão",
                 "university management", "management information system"],
        "platform": ["mastersoft", "iitms", "samarth", "beessoftware", "techne"],
    },
    "Exam/Results": {
        "observed_rows": 198,
        "aliases": ["Results Portal", "Examination Portal", "Hall Ticket"],
        "url": ["exam", "exams", "examination", "result", "results", "marks",
                "grade", "grades", "notas", "boletim", "hallticket", "admitcard",
                "resultado", "resultados", "gradesheet", "transcript"],
        "text": ["examination", "results", "hall ticket", "admit card",
                 "resultado", "boletim", "notas", "परिणाम", "kết quả"],
        "platform": ["contineo", "msbte", "nbuexams", "shekhauniexam", "jisexam"],
    },
    "Fees": {
        "observed_rows": 145,
        "aliases": ["Fee Portal", "Tuition/Fees", "Fee Payment"],
        "url": ["fee", "fees", "feepay", "tuition", "payment", "pagos", "pagamento",
                "mensalidade", "boleto", "onfees", "erpfees"],
        "text": ["pay fees", "fee payment", "pagar mensalidade", "pago de matrícula",
                 "tuition payment"],
        "platform": ["eduqfix", "onfees", "erpfees", "feepayr"],
        "caution": ("Institution fee portals behind a student login count. Pure "
                    "third-party payment/checkout GATEWAYS do not — see the "
                    "blacklist (upago.cl, easebuzz, okiedokiepay)."),
    },
    "Library": {
        "observed_rows": 545,
        "aliases": ["OPAC", "Digital Library", "Knowledge Resource Centre"],
        "url": ["library", "lib", "opac", "koha", "biblioteca", "biblio",
                "digilib", "elibrary", "e-library", "knimbus", "delnet",
                "remotexs", "myloft", "openathens", "libguides", "pergamum"],
        "text": ["library", "biblioteca", "catálogo", "catalogue", "opac",
                 "digital library", "e-resources"],
        "platform": ["koha", "pergamum", "knimbus", "delnet", "myloft",
                     "openathens", "elibro", "minhabiblioteca"],
        "caution": ("The corpus DOES accept library logins as student portals "
                    "(545 rows), but publisher/database SSO (Elsevier, Springer, "
                    "JSTOR, EBSCO) is rejected — it is not the institution's own."),
    },
    "Webmail": {
        "observed_rows": 48,
        "aliases": ["Student Email"],
        "url": ["webmail", "mail", "email", "roundcube", "horde", "zimbra",
                "squirrelmail", "rainloop", "owa"],
        "text": ["webmail", "correo", "correio", "student email"],
        "platform": ["roundcube", "zimbra", "horde", "squirrelmail"],
        "caution": ("Judged 'webmail' is DROPPED by current accept rules — human "
                    "review ruled staff/generic mail is not a student portal. "
                    "Kept here for classification, not for acceptance."),
    },
    "Admissions/Application": {
        "observed_rows": 4,
        "aliases": ["Applicant Portal", "Prospectus"],
        "url": ["admission", "admissions", "applicant", "apply", "application",
                "ucanapply", "enrollonline", "entrance", "prospectus", "inscricao",
                "inscripcion", "vestibular", "preinscripcion"],
        "text": ["apply now", "admission", "prospective student", "inscrição",
                 "inscripción", "vestibular"],
        "caution": "ALWAYS excluded — for PROSPECTIVE applicants, not enrolled students.",
    },
    "Other": {
        "observed_rows": 629,
        "aliases": [],
        "url": [], "text": [], "platform": [],
        "caution": "Reviewer bucket for valid-but-uncategorised student logins.",
    },
}

# --------------------------------------------------------------------------- #
# 3. compliance_exclusion_blacklist                                            #
#    Substring/regex triggers that reject a URL outright, grouped by reason.    #
#    Sources: agent/magic.py (_JUNK_PORTAL_RE, _SUBPAGE_RE, _GRIEVANCE_RE,      #
#    _CONTENT_PATH_RE, _WEBMAIL_*), scripts/_run_portals_review.py             #
#    (_PAYMENT_RE, _CONTENT_RE, _DOC_RE), scripts/idp_denylist.txt, and the     #
#    ~94 human review comments on column E.                                    #
# --------------------------------------------------------------------------- #
blacklist = {
    "_note": ("Case-insensitive substring match against the FULL final URL "
              "unless noted. `hard` = reject immediately, never judge. "
              "`soft` = reject unless another signal proves it is a student "
              "login. Applied AFTER client-side redirects are resolved, so a "
              "logout URL that lands on a login page is not lost."),
    "cms_and_admin": {
        "severity": "hard",
        "reason": "Site-admin backends, never a student login.",
        "match": ["wp-login", "wp-admin", "/administrator/", "/admin/login",
                  "/adminpanel", "cpanel", "whm", "plesk", "phpmyadmin",
                  "/typo3/", "/drupal/user/login", "/joomla/", "webmin",
                  "/manager/html", "/wp-json"],
    },
    "staff_faculty_hr": {
        "severity": "hard",
        "reason": "Employee-facing, not the general student body.",
        "match": ["staff-login", "staff_login", "stafflogin", "/staff/",
                  "faculty-login", "facultylogin", "/faculty/login", "/employee",
                  "employee-login", "hrms", "/hr/", "payroll", "recruitment",
                  "/teacher/login", "professor/login", "docente/login",
                  "/servidor/", "funcionario", "/rh/"],
    },
    "alumni_career_jobs": {
        "severity": "hard",
        "reason": "Alumni/donor and careers portals are not enrolled-student logins.",
        "match": ["alumni", "alumnus", "exalumno", "egresado", "ex-aluno",
                  "egresso", "donor", "giving", "career", "careers", "jobs",
                  "job-portal", "jobportal", "recruit", "placement", "vacancy",
                  "vacancies", "hiring", "/emprego", "bolsa-de-trabajo"],
    },
    "admissions": {
        "severity": "hard",
        "reason": "For PROSPECTIVE applicants, not enrolled students.",
        "match": ["admission", "admissions", "applicant", "ucanapply", "/apply",
                  "applynow", "entrance", "prospectus", "enrollonline",
                  "/inscricao", "/inscripcion", "vestibular", "preinscripcion",
                  "counselling", "/cap/", "admitcard-apply"],
    },
    "webmail": {
        "severity": "soft",
        "reason": "Generic/staff mail. Human review ruled these out as portals.",
        "match": ["webmail", "roundcube", "squirrelmail", "horde", "zimbra",
                  "rainloop", "afterlogic", "/owa", "outlook.office",
                  "mail.google.com", "/mail/login"],
    },
    "payment_gateways": {
        "severity": "hard",
        "reason": "Third-party checkout, not an institution login.",
        "match": ["easebuzz", "okiedokiepay", "upago.cl", "razorpay", "payu",
                  "billdesk", "ccavenue", "paytm", "stripe.com", "paypal.com",
                  "mercadopago", "pagseguro", "/checkout", "/makepayment",
                  "/payment/welcome", "/pagar", "cielo.com.br"],
    },
    "publisher_database_sso": {
        "severity": "hard",
        "reason": "Publisher platforms — not the institution's own login.",
        "match": ["elsevier", "sciencedirect", "springer", "link.springer",
                  "ioppublishing", "ebsco", "jstor", "proquest", "turnitin",
                  "scopus", "clarivate", "webofscience", "emerald", "wiley",
                  "sagepub", "taylorfrancis", "degruyter", "refread"],
    },
    "idp_metadata": {
        "severity": "hard",
        "reason": ("SAML entityID / metadata documents are machine endpoints, "
                   "never a human login. Source: scripts/idp_denylist.txt."),
        "match": ["/idp/shibboleth", "/simplesaml/saml2/idp/metadata",
                  "/metadata.php", "/saml2/idp/metadata", "/federationmetadata",
                  "/sso/saml/metadata", "entityid=", "/.well-known/openid-configuration"],
    },
    "auth_subpages": {
        "severity": "hard",
        "reason": ("Sub-pages of a login, not the entry point. NOTE: apply to "
                   "the RESOLVED url — some systems bounce through /logout to "
                   "reach /login (BUET's biis)."),
        "match": ["forgot", "forgotpassword", "reset-password", "resetpassword",
                  "recover", "/register", "signup", "sign-up", "changepassword",
                  "first-access", "primeiroacesso", "esqueci", "/activate",
                  "/verify-email"],
    },
    "content_and_docs": {
        "severity": "soft",
        "reason": "Manuals, help centres, news — describe a portal, are not one.",
        "match": ["manualdoaluno", "/docs/", "/doc/", "/ajuda", "/suporte",
                  "/support", "/help", "/faq", "/wiki", "/kb/", "tutorial",
                  "/noticias", "/news", "/blog", "/about", "/acerca", "/contato",
                  "/contact", "/downloads", "/gallery", "/circular"],
    },
    "grievance_gov": {
        "severity": "hard",
        "reason": "Complaint/government portals, not institution logins.",
        "match": ["grievanc", "/igram", "pgportal", "complaint", "rti-",
                  "bhashini", "/nic.in/", "swayam.gov", "aicte-india"],
    },
    "legal_pages": {
        "severity": "hard",
        "reason": "Policy pages harvested alongside a portal.",
        "match": ["/privacy", "privacy-policy", "/terms", "terms-of-service",
                  "termos-de-uso", "/tos", "/cgu", "/agb", "politica-de-privacidade",
                  "aviso-legal", "disclaimer", "cookies-policy", "lgpd"],
    },
    "staging_nonprod": {
        "severity": "hard",
        "reason": "Non-production hosts.",
        "match_host_label": ["uat", "qa", "test", "testing", "staging", "homolog",
                             "dev", "sandbox", "demo", "beta", "old", "backup"],
    },
    "social_appstore": {
        "severity": "hard",
        "reason": "Social links and mobile-app deep links.",
        "match": ["facebook.com", "twitter.com", "x.com/", "instagram.com",
                  "linkedin.com", "youtube.com", "play.google.com/store",
                  "apps.apple.com", "itunes.apple.com", "download.moodle.org",
                  "whatsapp.com", "t.me/"],
    },
    "infrastructure_hosts": {
        "severity": "hard",
        "reason": ("CDN/WAF/PaaS hosts — a portal may sit behind them, but the "
                   "root itself must never be whitelisted as a vendor."),
        "match": sorted(INFRA_EXCLUDE),
    },
    "not_student_login_vendors": {
        "severity": "hard",
        "reason": "Multi-institution vendors that are not an enrolled-student login.",
        "match": sorted(NOT_A_STUDENT_PORTAL),
        "detail": NOT_A_STUDENT_PORTAL,
    },
}

kb = {
    "$schema_version": "1.0",
    "name": "Genie-V3 static knowledge base",
    "generated_by": "scripts/_build_agent_kb.py",
    "provenance": {
        "corpora": [
            "CONFIDENTIAL_Provider Activation (2 spreadsheets)",
            "JulyBatch (9July / Portals / Portals TnC / LiveAll / Reported URLs)",
            "August4000 (all orgs / portals)",
            "Indian Universities (per-state tabs)",
            "Bangladesh / Indonesia / Argentina / Brazil / Mexico",
        ],
        "method": ("Portal URLs whose registrable root differs from the org's own "
                   "email domain, kept when observed for 2+ distinct institutions: "
                   "1,623 off-domain roots -> 456 multi-org -> curated here. "
                   "Categories are the human-reviewed Category column (~8,900 rows)."),
        "human_review_rows": 8900,
        "off_domain_roots_examined": 1623,
        "multi_org_roots": 456,
    },
    "counts": {
        "saas_infra_whitelist": len(whitelist),
        "confirmed": sum(1 for e in whitelist if e["confidence"] == "confirmed"),
        "candidate": sum(1 for e in whitelist if e["confidence"] == "candidate"),
        "categories": len([k for k in relevance if not k.startswith("_")]),
        "blacklist_groups": len([k for k in blacklist if not k.startswith("_")]),
    },
    "saas_infra_whitelist": whitelist,
    "relevance_classification_keywords": relevance,
    "compliance_exclusion_blacklist": blacklist,
}

out = ROOT / "agent_knowledge_base.json"
out.write_text(json.dumps(kb, indent=2, ensure_ascii=False) + "\n")
print(f"wrote {out}")
print(f"  whitelist            : {len(whitelist)} "
      f"({kb['counts']['confirmed']} confirmed / {kb['counts']['candidate']} candidate)")
print(f"  categories           : {kb['counts']['categories']}")
print(f"  blacklist groups     : {kb['counts']['blacklist_groups']} "
      f"({sum(len(v.get('match', []) or v.get('match_host_label', [])) for k, v in blacklist.items() if not k.startswith('_'))} terms)")
import collections
print("  by geo               :", dict(collections.Counter(e["geo"] for e in whitelist).most_common()))
print("  by category          :", dict(collections.Counter(e["category"] for e in whitelist).most_common()))

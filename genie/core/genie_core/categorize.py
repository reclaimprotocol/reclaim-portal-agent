"""Content-aware portal categorization.

The agent tends to label most discovered login pages "Student Portal". This
classifier looks at the actual page — <title>, <meta generator>, headings,
visible text, and login-form field names — plus the URL, and picks the most
specific category it can justify. URL tokens are a weak signal; on-page content
(especially the title and form fields) is the strong one.

Used by the batch re-categorizer (`genie/reclassify.py`) and by live discovery
to refine portals the agent left generic.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

try:
    import requests
    import urllib3
    urllib3.disable_warnings()  # many .ac.in hosts have broken certs
except Exception:  # pragma: no cover
    requests = None

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# Ordered most-specific → generic. Ties resolve to the earlier (more specific)
# entry. Each: (name, url_tokens, content_keywords, form_field_hints).
CATEGORIES: list[tuple[str, list[str], list[str], list[str]]] = [
    ("LMS / Moodle",
     ["moodle", "/lms", "lms.", "elearn", "e-learn", "learning", "vle", "/login/index.php", "lcms"],
     ["moodle", "powered by moodle", "learning management", "my courses", "course catalogue",
      "course catalog", "e-learning", "online courses", "cookies must be enabled"],
     ["course"]),
    ("ERP",
     ["erp", "samarth", "iitms", "mastersofterp", "icloudems", "corecampus", "core-campus",
      "camu", "digitaluniversity", "ecampus", "vidyavision", "academia.", "peoplesoft", "/sap"],
     ["enterprise resource", "campus management", "student information system",
      "college management system", "erp", "integrated university management"],
     []),
    ("Library",
     ["library", "lib.", "/lib", "opac", "koha", "dspace", "webopac", "knimbus", "remotexs", "libsys"],
     ["library", "opac", "online public access catalogue", "koha", "dspace", "digital library",
      "e-resources", "web opac", "institutional repository"],
     []),
    ("Examination Portal",
     ["exam", "result", "hallticket", "hall-ticket", "admitcard", "admit-card", "grade"],
     ["examination", "hall ticket", "admit card", "exam results", "revaluation", "exam form",
      "date sheet", "result portal", "semester result"],
     []),
    ("Admission Portal",
     ["admission", "apply", "prospectus", "entrance", "counsel", "onlineadmission"],
     ["admission", "apply online", "prospectus", "entrance exam", "online application",
      "register for admission", "admission portal", "new registration"],
     []),
    ("Fee Portal",
     ["fee", "fees", "payment", "epay", "onlinepay"],
     ["fee payment", "pay fees", "online payment", "pay your fees", "fee collection"],
     []),
    ("Hostel Portal",
     ["hostel"],
     ["hostel", "hostel management", "accommodation", "mess management"],
     []),
    ("Alumni Portal",
     ["alumni"],
     ["alumni", "alumni association", "alumni network"],
     []),
    ("Webmail",
     ["webmail", "roundcube", "zimbra", "/owa", "mail."],
     ["webmail", "roundcube", "zimbra", "outlook web", "sign in to your email"],
     []),
    ("Faculty/Staff Portal",
     ["faculty", "staff", "employee", "teacher"],
     ["faculty login", "staff login", "employee login", "employee self service", "faculty portal"],
     ["employee", "employeeid", "empcode", "staffid"]),
    ("Student Portal",
     ["student", "studentlogin", "student-login", "enroll", "learner"],
     ["student login", "student portal", "student sign in", "student dashboard", "student corner"],
     ["rollno", "roll", "enrollment", "enrolment", "studentid", "regno", "registrationno",
      "admissionno", "prn", "uid"]),
]

_TAGS = re.compile(r"<(script|style|noscript)[^>]*>.*?</\1>", re.S | re.I)
_STRIP = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def _extract(html: str) -> tuple[str, str, str, str]:
    """Return (title, generator, form-field-blob, visible-text) — all lowercased."""
    low = html.lower()
    m = re.search(r"<title[^>]*>(.*?)</title>", low, re.S)
    title = _WS.sub(" ", m.group(1)).strip() if m else ""
    g = re.search(r'<meta[^>]+name=["\']generator["\'][^>]+content=["\']([^"\']+)', low)
    generator = g.group(1) if g else ""
    fields = " ".join(re.findall(r'(?:name|id|placeholder|for)=["\']([^"\']+)["\']', low))
    fields = re.sub(r"[^a-z0-9 ]+", "", fields)
    text = _STRIP.sub(" ", _TAGS.sub(" ", low))
    text = _WS.sub(" ", text)[:25000]
    return title, generator, fields, text


def fetch_html(url: str, *, timeout: float = 12.0, user_agent: str = _UA) -> str:
    if requests is None:
        return ""
    try:
        r = requests.get(url, headers={"User-Agent": user_agent}, timeout=timeout,
                         verify=False, allow_redirects=True)
        ct = r.headers.get("content-type", "")
        if r.status_code < 400 and ("html" in ct or not ct):
            return r.text or ""
    except Exception:
        return ""
    return ""


def classify(url: str, html: str | None = None, *, fetch: bool = True,
             timeout: float = 12.0, user_agent: str = _UA) -> tuple[str, int, str]:
    """Return (category, score, evidence). score 0 = URL-only fallback guess."""
    parts = urlsplit(url if "://" in url else "http://" + url)
    urlblob = f"{parts.netloc} {parts.path} {parts.query}".lower()
    if html is None and fetch:
        html = fetch_html(url, timeout=timeout, user_agent=user_agent)
    title, generator, fields, text = _extract(html or "")

    best_name, best_score, best_ev = "", 0, ""
    for name, toks, kws, flds in CATEGORIES:
        score, ev = 0, []
        for t in toks:
            if t in urlblob:
                score += 3; ev.append(f"url:{t}")
        for k in kws:
            if k in title:
                score += 4; ev.append(f"title:{k}")
            elif k in text:
                score += 2; ev.append(f"text:{k}")
        for f in flds:
            if f in fields:
                score += 2; ev.append(f"field:{f}")
        if name == "LMS / Moodle" and "moodle" in generator:
            score += 4; ev.append("generator:moodle")
        if score > best_score:
            best_name, best_score, best_ev = name, score, ", ".join(ev[:5])

    if best_score <= 0:
        # nothing distinctive — decide student-login vs unknown from soft signals
        if ("student" in urlblob or any(x in fields for x in ("roll", "enroll", "regno", "prn"))
                or "student" in title):
            return "Student Portal", 0, "weak: student hint"
        return "Portal", 0, "no signal"
    return best_name, best_score, best_ev

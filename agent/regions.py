"""Geography-aware discovery rule packs.

When the agent detects a university's country (by academic TLD, e.g. ``.edu.ar``,
or an explicit country field), it activates a *region pack* that injects
country-specific discovery knowledge on top of the generic logic:

  * ``functional_labels``  — local-language subdomain labels that name a real
    portal (so a discovered ``autogestion.uni.edu.ar`` survives the
    English-centric functional-label filter).
  * ``subdomain_probes``   — the country analog of the India ``samarth`` tenant
    probes: ``{label}.{university-domain}{login_path}`` guesses for the local
    platforms, ordered by observed prevalence.
  * ``login_link_phrases`` — local-language anchor text for homepage
    student-link detection ("acceso alumnos", "campus virtual", …).
  * ``platform_fingerprints`` — HTML/URL regexes that name the platform and its
    category (SIU-Guaraní → Student Portal, Moodle → LMS/Moodle, …).

Adding a country later is just another ``RegionPack`` entry — the discovery
pipeline reads them generically.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class RegionPack:
    name: str
    country: str
    tld_suffixes: tuple[str, ...]
    functional_labels: frozenset[str]
    # (subdomain label, portal category, login path to probe)
    subdomain_probes: tuple[tuple[str, str, str], ...]
    login_link_phrases: tuple[str, ...] = ()
    login_path_tokens: tuple[str, ...] = ()
    # (platform name, compiled regex over HTML/URL, portal category)
    platform_fingerprints: tuple[tuple[str, "re.Pattern[str]", str], ...] = ()


# --------------------------------------------------------------------------- AR
# Data-driven: mined across the 125 universities on Wikipedia's list of
# Argentine universities (2026-07). Of 125 sites, HTML fingerprints found
# Moodle on 57 and SIU-Guaraní on 48; subdomain probes resolved+responded at:
#   campus 36 · guarani 34 · preinscripcion 30 · virtual/campusvirtual 26 ·
#   autogestion 25 · ingreso 19 · portal 19 · servicios/alumnos 15 · siu 14 ·
#   moodle 12 · aulavirtual 11 · estudiantes 10 · ead 8 · g3w 7 · sysacad 4
# SIU-Guaraní is Argentina's near-national student self-management system;
# Moodle ("campus/aula virtual") is the dominant LMS.
_ARGENTINA = RegionPack(
    name="argentina",
    country="Argentina",
    tld_suffixes=(".edu.ar", ".ar"),
    functional_labels=frozenset({
        # SIU-Guaraní (student self-management)
        "autogestion", "guarani", "siu", "g3w", "sig", "sysacad",
        # Moodle / campus virtual (LMS)
        "campus", "campusvirtual", "aulavirtual", "aula", "virtual",
        "moodle", "ead",
        # student / admission
        "alumnos", "estudiantes", "servicios", "portal",
        "preinscripcion", "ingreso",
    }),
    subdomain_probes=(
        # SIU-Guaraní — the app root is the login surface.
        ("autogestion", "Student Portal", "/"),
        ("guarani", "Student Portal", "/"),
        ("siu", "Student Portal", "/"),
        ("g3w", "Student Portal", "/"),
        # Moodle — standard login path.
        ("campus", "LMS/Moodle", "/login/index.php"),
        ("campusvirtual", "LMS/Moodle", "/login/index.php"),
        ("aulavirtual", "LMS/Moodle", "/login/index.php"),
        ("virtual", "LMS/Moodle", "/login/index.php"),
        ("moodle", "LMS/Moodle", "/login/index.php"),
        # student / admission portals
        ("alumnos", "Student Portal", "/"),
        ("estudiantes", "Student Portal", "/"),
        ("portal", "Student Portal", "/"),
        ("preinscripcion", "Student Portal", "/"),
        ("ingreso", "Student Portal", "/"),
    ),
    login_link_phrases=(
        "autogestion", "autogestión", "acceso alumnos", "acceso a alumnos",
        "campus virtual", "aula virtual", "siu guarani", "siu guaraní",
        "iniciar sesion", "iniciar sesión", "ingresar", "acceso",
        "alumnos", "preinscripcion", "preinscripción",
    ),
    login_path_tokens=("acceso", "ingresar", "autogestion"),
    platform_fingerprints=(
        ("SIU-Guaraní",
         re.compile(r"siu[\s-]*guaran|guaran[ií]|autogesti[oó]n|/g3w/", re.I),
         "Student Portal"),
        ("Moodle",
         re.compile(r"moodle|aula virtual|/login/index\.php", re.I),
         "LMS/Moodle"),
        ("SysAcad",
         re.compile(r"sysacad", re.I),
         "Student Portal"),
    ),
)


REGION_PACKS: tuple[RegionPack, ...] = (_ARGENTINA,)


# --------------------------------------------------------------------------- geo
# TLD → country. This is the ONE geography signal the pipeline uses: detect the
# university's country from its domain up front, then only run that country's
# logic (never Indian rules on a .kr site, never Argentine probes on a .br site).
# Country-code and common academic TLDs only; generic gTLDs (.edu/.com/.org/…)
# carry no geography, so they map to "" (unknown → run no country-specific rule).
_TLD_COUNTRY: dict[str, str] = {
    "in": "India", "ac.in": "India", "edu.in": "India",
    "ar": "Argentina", "edu.ar": "Argentina",
    "br": "Brazil", "edu.br": "Brazil",
    "kr": "South Korea", "ac.kr": "South Korea",
    "jp": "Japan", "ac.jp": "Japan",
    "cn": "China", "edu.cn": "China",
    "uk": "United Kingdom", "ac.uk": "United Kingdom",
    "au": "Australia", "edu.au": "Australia",
    "nz": "New Zealand", "ac.nz": "New Zealand",
    "za": "South Africa", "ac.za": "South Africa",
    "ng": "Nigeria", "edu.ng": "Nigeria",
    "ke": "Kenya", "ac.ke": "Kenya",
    "pk": "Pakistan", "edu.pk": "Pakistan",
    "bd": "Bangladesh", "ac.bd": "Bangladesh",
    "lk": "Sri Lanka", "ac.lk": "Sri Lanka",
    "id": "Indonesia", "ac.id": "Indonesia",
    "my": "Malaysia", "edu.my": "Malaysia",
    "ph": "Philippines", "edu.ph": "Philippines",
    "th": "Thailand", "ac.th": "Thailand",
    "vn": "Vietnam", "edu.vn": "Vietnam",
    "tw": "Taiwan", "edu.tw": "Taiwan",
    "hk": "Hong Kong", "edu.hk": "Hong Kong",
    "sg": "Singapore", "edu.sg": "Singapore",
    "mx": "Mexico", "edu.mx": "Mexico",
    "cl": "Chile", "co": "Colombia", "edu.co": "Colombia",
    "pe": "Peru", "edu.pe": "Peru", "uy": "Uruguay", "edu.uy": "Uruguay",
    "de": "Germany", "fr": "France", "es": "Spain", "it": "Italy",
    "pt": "Portugal", "nl": "Netherlands", "be": "Belgium", "ch": "Switzerland",
    "at": "Austria", "se": "Sweden", "no": "Norway", "fi": "Finland",
    "dk": "Denmark", "pl": "Poland", "cz": "Czechia", "gr": "Greece",
    "ie": "Ireland", "ru": "Russia", "ua": "Ukraine", "tr": "Turkey",
    "edu.tr": "Turkey", "ir": "Iran", "ac.ir": "Iran",
    "sa": "Saudi Arabia", "edu.sa": "Saudi Arabia",
    "ae": "United Arab Emirates", "ac.ae": "United Arab Emirates",
    "eg": "Egypt", "edu.eg": "Egypt", "il": "Israel", "ac.il": "Israel",
    "ca": "Canada", "ghana": "Ghana", "gh": "Ghana", "edu.gh": "Ghana",
}


def country_of_domain(domain: str) -> str:
    """The university's country inferred from its domain TLD, or "" if the TLD
    carries no geography (generic gTLD like .edu / .com / .org / .io). Matches
    the longest known suffix first so ``ac.uk`` beats ``uk``.

    This is the single source of truth for "what country is this?". Callers
    gate country-specific logic on it and compare two domains' countries to
    decide whether a portal is genuinely *foreign* to its university."""
    d = (domain or "").lower().strip().rstrip(".")
    if not d:
        return ""
    labels = d.split(".")
    # Try the two-label academic suffix (ac.in, edu.ar) then the ccTLD.
    for n in (2, 1):
        if len(labels) >= n:
            suf = ".".join(labels[-n:])
            if suf in _TLD_COUNTRY:
                return _TLD_COUNTRY[suf]
    return ""


def detect_region(domains: list[str] | tuple[str, ...],
                  country: str = "") -> RegionPack | None:
    """Return the region pack for these domains / country, or None.

    Matches by academic TLD suffix first (most reliable), then by an explicit
    country string. The first matching pack wins.
    """
    doms = [d.lower().strip().lstrip(".") for d in (domains or []) if d]
    cty = (country or "").strip().lower()
    for pack in REGION_PACKS:
        if any(d == suf.lstrip(".") or d.endswith(suf) for d in doms
               for suf in pack.tld_suffixes):
            return pack
        if cty and cty == pack.country.lower():
            return pack
    return None


def url_is_region_login_surface(url: str) -> tuple[str, str] | None:
    """If `url` is unmistakably a known regional platform's login surface,
    return (platform_name, category); else None.

    This is the region analog of `host_is_known_shared_platform`: these
    platforms render their login via JS / multi-step flows and often have no
    static password input, so matching URLs are exempted from the strict
    form-content gate (the hard verification gate — DNS/status/body — still
    applies). The signatures below are specific enough that they don't fire on
    unrelated sites:

      * SIU-Guaraní — Argentina/LatAm student self-management. Its hosts are
        literally named ``guarani``/``autogestion`` (Spanish, platform-specific)
        and its login lives at ``/acceso/login`` or ``/g3w``.
      * Moodle — the exact login path ``/login/index.php`` on a
        campus/virtual/aula host.
    """
    from urllib.parse import urlsplit
    parts = urlsplit(url)
    host = parts.netloc.lower().split(":")[0]
    path = parts.path.lower()
    labels = host.split(".")
    if "guarani" in host or "autogestion" in host:
        return ("SIU-Guaraní", "Student Portal")
    if "/g3w" in path or "/acceso/login" in path or path.endswith("/acceso"):
        return ("SIU-Guaraní", "Student Portal")
    if "/login/index.php" in path and any(
        l in ("campus", "campusvirtual", "aulavirtual", "aula", "virtual",
              "moodle", "eduvirtual") for l in labels
    ):
        return ("Moodle", "LMS/Moodle")
    return None


def region_probe_urls(pack: RegionPack,
                      domains: list[str] | tuple[str, ...]) -> list[dict]:
    """Build ``{label}.{domain}{login_path}`` probe descriptors for each owned
    domain. Returns dicts (url, category, reasoning) — the caller wraps them in
    its Candidate type. Validation drops any that NXDOMAIN / 404.
    """
    out: list[dict] = []
    seen: set[str] = set()
    for dom in domains:
        dom = (dom or "").lower().strip().lstrip(".")
        if not dom:
            continue
        for label, category, path in pack.subdomain_probes:
            url = f"https://{label}.{dom}{path}"
            if url in seen:
                continue
            seen.add(url)
            out.append({
                "url": url,
                "category": category,
                "reasoning": f"{pack.name} region probe: {label}.{dom}",
            })
    return out

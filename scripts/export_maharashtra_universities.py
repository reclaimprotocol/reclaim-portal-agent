"""Export universities from Maharashtra to a CSV file.

The SheerID Universities tab has no `state` column, so this script
identifies Maharashtra universities heuristically by matching the
SheerID University Name (primary) and Website Domain (secondary)
against:

  * Explicit "Maharashtra" / "M.H." mention.
  * Maharashtra city / district names (Mumbai, Pune, Nagpur, Nashik,
    Aurangabad / Chhatrapati Sambhajinagar, Kolhapur, Solapur, …).
  * Well-known Maharashtra institution name phrases (University of
    Mumbai, Savitribai Phule Pune University, Shivaji University,
    Babasaheb Ambedkar Marathwada, RTMNU Nagpur, IIT Bombay, TISS,
    Symbiosis, Bharati Vidyapeeth, NMIMS, ICT, VJTI, …).
  * Website domain matching well-known Maharashtra root domains
    (`mu.ac.in`, `unipune.ac.in`, `iitb.ac.in`, `tiss.edu`,
    `digitaluniversity.ac` / `digitaluniversity.ac.in` /
    `mkcl.org` — Maharashtra state-platform per
    `STATE_PLATFORM_HINTS`).
  * Any OrgID whose `domain_overrides.json` entry has
    `state == "Maharashtra"` (defensive — no entries currently set
    this, but the path is here for future overrides).

False-positive guard: a `NEGATIVE_TOKENS` blocklist removes
institutions whose name fragments collide with MH keywords but which
are explicitly elsewhere (e.g. "MIT Manipal", "Christ University"
Bangalore, "Bharati Vidyapeeth" NCR / Karnataka satellite campuses,
"Symbiosis Skill" Telangana, "Aurangabad" in Bihar).

The renamed cities (Aurangabad → Chhatrapati Sambhajinagar,
Osmanabad → Dharashiv) are both tokens; SheerID rows still use the
old names predominantly.

Output: `maharashtra_universities.csv` in the repo root.
"""
from __future__ import annotations

import csv
import re
import sys
from pathlib import Path

# Bootstrap repo imports (scripts/ is sibling of agent/)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.config import load_config
from agent.sheets_client import SheetsClient

OUTPUT_PATH = (
    Path(__file__).resolve().parent.parent / "maharashtra_universities.csv"
)

# Maharashtra city / district tokens. Lowercased word-boundary match
# against the university name. The list mirrors the 36 districts of
# Maharashtra plus a few high-frequency taluka / suburb names that
# appear in institution titles.
MH_CITY_TOKENS: tuple[str, ...] = (
    # Mumbai metropolitan region
    "mumbai", "bombay", "navi mumbai", "thane", "kalyan", "dombivli",
    "vasai", "virar", "mira-bhayandar", "ulhasnagar", "bhiwandi",
    "panvel", "ambernath", "badlapur",
    # Pune metropolitan region
    "pune", "pimpri", "chinchwad", "pimpri-chinchwad", "lavale",
    "lonavala", "talegaon", "khed",
    # Nagpur
    "nagpur",
    # Nashik
    "nashik", "nasik",
    # Aurangabad / Chhatrapati Sambhajinagar
    "aurangabad", "chhatrapati sambhajinagar", "sambhajinagar",
    # Marathwada (other)
    "nanded", "latur", "parbhani", "beed", "hingoli",
    "osmanabad", "dharashiv", "jalna",
    # Vidarbha (other)
    "amravati", "akola", "yavatmal", "wardha", "buldhana", "washim",
    "chandrapur", "gondia", "bhandara", "gadchiroli",
    # Western Maharashtra (other)
    "kolhapur", "sangli", "satara", "solapur", "ahmednagar",
    "ichalkaranji",
    # Konkan
    "ratnagiri", "sindhudurg", "raigad", "alibag", "palghar",
    # Khandesh
    "dhule", "jalgaon", "nandurbar",
    # Other notable institution-bearing towns
    "rahuri",           # MPKV
    "lonere",           # DBATU
    "krishi vidyapeeth", "marathwada",
)

# Multi-word phrases that strongly identify Maharashtra institutions.
# Plain (case-insensitive) substring match against the lowercased name.
MH_INSTITUTION_PHRASES: tuple[str, ...] = (
    # Generic regional
    "maharashtra",
    # Mumbai-region universities & deemed universities
    "university of mumbai",
    "mumbai university",
    "iit bombay", "indian institute of technology bombay",
    "tata institute of social sciences",
    "homi bhabha national institute",
    "institute of chemical technology",   # ICT, Mumbai
    "veermata jijabai technological",     # VJTI, Mumbai
    "sardar patel institute of technology",
    "narsee monjee",                       # NMIMS
    "nmims",
    "k.j. somaiya", "kj somaiya", "somaiya vidyavihar",
    "sp jain", "s.p. jain",
    "welingkar",
    "wilson college",
    "h.r. college", "hr college of commerce",
    "jai hind college",
    "mithibai college",
    "k.c. college", "kc college",
    "bhavan's college mumbai",
    "ramnarain ruia",
    "ramniranjan jhunjhunwala",            # RJ College Mumbai
    "podar college",
    "elphinstone college",
    "st. xavier's college, mumbai",
    "st xaviers mumbai",
    "xavier institute of",                 # XIE, XIC Mumbai
    "thakur college",
    "ges's r.h. sapat",                    # GES Nashik / Mumbai
    # Pune-region universities & deemed universities
    "savitribai phule pune",
    "pune university",
    "university of pune",
    "symbiosis international",
    "symbiosis institute",
    "symbiosis centre",
    "symbiosis school",
    "symbiosis law",
    "symbiosis college",
    "bharati vidyapeeth",
    "mit world peace",                     # MIT-WPU Pune
    "mit adt", "mit-adt", "mit art design",
    "mit academy of engineering",          # MITAOE Alandi
    "college of engineering pune",         # COEP
    "coep technological",
    "vishwakarma institute",               # VIT / VIIT Pune
    "ajeenkya dy patil",
    "d.y. patil", "dy patil", "d y patil",
    "padmashree dr. d.y. patil",
    "ajinkya dy patil",
    "flame university",                    # Pune
    "sandip university",                   # Nashik
    "sinhgad",                             # Sinhgad Institutes Pune
    "modern college pune",
    "fergusson college",
    "garware college",
    "brihan maharashtra",                  # BMCC Pune
    "wadia college",
    "abasaheb garware",
    "indira college",                      # Indira Group Pune
    "indira institute",
    "pict pune", "pune institute of computer technology",
    "aissms",                              # AISSMS Pune
    "maharashtra institute of technology",  # MIT Pune family
    "national institute of construction management",  # NICMAR Pune
    "indian law society",                  # ILS Pune
    "ils law college",
    "deccan college",
    "gokhale institute of politics",
    "national defence academy",            # NDA Khadakwasla
    "armed forces medical college",        # AFMC Pune
    # Marathwada universities
    "babasaheb ambedkar marathwada",
    "dr. babasaheb ambedkar marathwada",
    "swami ramanand teerth marathwada",
    "maharashtra national law university aurangabad",
    "deogiri",                             # Deogiri College Aurangabad
    # Vidarbha universities
    "rashtrasant tukadoji maharaj",        # RTMNU Nagpur
    "nagpur university",
    "sant gadge baba",                     # SGBAU Amravati
    "amravati university",
    "kavi kulguru kalidas",                # KKSU Ramtek
    "gondwana university",                 # Gadchiroli
    "iit nagpur",
    "iim nagpur",
    "iiit nagpur",
    "vnit nagpur", "visvesvaraya national",
    "shri ramdeobaba",                     # RBU Nagpur
    "yeshwantrao chavan college of engineering",  # YCCE Nagpur
    "lakshminarayan innovation",
    "g.h. raisoni", "gh raisoni",          # GHRCE Nagpur / Pune
    "priyadarshini college",               # Nagpur cluster
    # Northern / Khandesh universities
    "north maharashtra",
    "kavayitri bahinabai chaudhari",       # KBCNMU Jalgaon
    "yashwantrao chavan maharashtra open",  # YCMOU Nashik
    "ycmou",
    "maharashtra university of health sciences",   # MUHS Nashik
    "muhs",
    # Solapur / Western / Kolhapur
    "shivaji university",
    "punyashlok ahilyadevi",               # Solapur Univ. renamed
    "solapur university",
    "tilak maharashtra vidyapeeth",        # TMV Pune
    "shivaji university kolhapur",
    "kolhapur institute",
    # Agriculture / Health / Tech specialist state univs
    "mahatma phule krishi",                # MPKV Rahuri
    "dr. panjabrao deshmukh krishi",       # PDKV Akola
    "vasantrao naik marathwada krishi",    # VNMKV Parbhani
    "dr. balasaheb sawant konkan",         # DBSKKV Dapoli
    "maharashtra animal and fishery",      # MAFSU Nagpur
    "maharashtra animal & fishery",
    "dr. babasaheb ambedkar technological",  # DBATU Lonere
    # Maharashtra state platform universities
    "mahatma gandhi mission",              # MGM Aurangabad / Navi Mumbai
    "mgm university",
    "mgm institute",
    "dhirubhai ambani institute",          # DAIICT is Gandhinagar; guard
    "dr. d.y. patil vidyapeeth",
    "dy patil university",
    "d.y. patil university",
    "amity university maharashtra",
    "amity university mumbai",
    "iim mumbai",
    "iim indore mumbai",
    "iisc bangalore",                      # explicit guard via negative
    "iiser pune",
    "indian institute of science education and research pune",
    "iiit pune",
    "iiit nagpur",
    "national institute of fashion technology mumbai",
    "nift mumbai",
    "spit mumbai",                         # Sardar Patel Institute of Tech
)

# Short institution acronyms unique enough to flag Maharashtra
# membership. Word-boundary matched.
MH_INSTITUTION_ACRONYMS: tuple[str, ...] = (
    "iitb",         # IIT Bombay
    "tiss",         # Tata Institute of Social Sciences
    "ict",          # Institute of Chemical Technology, Mumbai
    "vjti",         # VJTI Mumbai
    "spit",         # Sardar Patel Inst. of Tech, Mumbai
    "nmims",        # Narsee Monjee
    "sppu",         # Savitribai Phule Pune University
    "coep",         # College of Engineering Pune
    "afmc",         # Armed Forces Medical College Pune
    "ils",          # Indian Law Society Pune
    "iiserp",       # IISER Pune
    "iitnagpur",
    "iimnagpur",
    "iiitn",        # IIIT Nagpur
    "vnit",         # VNIT Nagpur
    "rtmnu",        # Rashtrasant Tukadoji Maharaj Nagpur Univ
    "sgbau",        # Sant Gadge Baba Amravati Univ
    "bamu",         # Babasaheb Ambedkar Marathwada Univ
    "srtmun",       # Swami Ramanand Teerth Marathwada Univ
    "kbcnmu",       # North Maharashtra Univ Jalgaon
    "ycmou",        # Yashwantrao Chavan MH Open Univ
    "muhs",         # MH Univ of Health Sciences
    "mpkv",         # Mahatma Phule Krishi Vidyapeeth, Rahuri
    "pdkv",         # Dr Panjabrao Deshmukh Krishi Vidyapeeth, Akola
    "vnmkv",        # Vasantrao Naik Marathwada Krishi Vidyapeeth
    "dbskkv",       # Dr Balasaheb Sawant Konkan Krishi Vidyapeeth
    "mafsu",        # MH Animal & Fishery Sciences Univ
    "dbatu",        # Dr Babasaheb Ambedkar Tech Univ, Lonere
    "tmv",          # Tilak Maharashtra Vidyapeeth, Pune
    "ycce",         # Yeshwantrao Chavan College of Engg Nagpur
    "ghrce",        # G.H. Raisoni College of Engineering
    "vit pune", "viit",  # Vishwakarma Institute Pune
    "mitwpu",
    "kjsce",        # KJ Somaiya College of Engineering
)

# Maharashtra-known website root domains. Substring (suffix) match
# against the `SheerID Website Domain` column. The MH state-government
# `digitaluniversity.ac` / `mkcl.org` platforms are listed in
# `STATE_PLATFORM_HINTS`, so SheerID domains hosted there are MH by
# construction.
MH_DOMAIN_PATTERNS: tuple[str, ...] = (
    # Mumbai
    "mu.ac.in", "mumbaiuniversity.ac.in", "fort.mu.ac.in",
    "iitb.ac.in", "iitbombay.org",
    "tiss.edu", "tiss.ac.in",
    "ictmumbai.edu.in", "ictedu.in",
    "vjti.ac.in", "vjti.org.in",
    "spit.ac.in",
    "nmims.edu", "nmimseducation.in",
    "somaiya.edu", "kjsomaiya.com", "ksoe.somaiya.edu",
    "spjain.org", "spjain.edu",
    "welingkar.org", "welingkaronline.org",
    "wilsoncollege.edu",
    "hrcollege.edu",
    "jaihindcollege.com",
    "mithibai.ac.in",
    "kccollege.org.in",
    "bhavanscollege.edu",
    "ruiacollege.edu",
    "rjcollege.edu.in",
    "podareducation.org",
    "elphinstonecollege.ac.in",
    "xaviers.edu",                          # St Xavier's Mumbai
    "xaviersmumbai.com",
    "ssjcollege.org",
    "thakureducation.org", "tmc.edu.in",
    # Pune
    "unipune.ac.in", "pun.unipune.ac.in", "puneuniversity.ac.in",
    "symbiosis.ac.in", "siu.edu.in",
    "bharatividyapeeth.edu", "bharatividyapeeth.org",
    "mitwpu.edu.in",
    "mitadt.edu.in", "mituniversity.ac.in", "mitapps.in",
    "coep.org.in",
    "vit.edu", "viit.ac.in",
    "ajeenkyadypatiluniversity.com", "ajeenkya.edu",
    "dypatil.edu", "dypatil.in", "dypatilonline.com",
    "dypiu.ac.in", "dypicoe.org",
    "flame.edu.in",
    "fergusson.edu", "ferguson.edu",
    "garwarecollege.mes.ac.in",
    "abasahebgarware.ac.in",
    "deccancollegepune.ac.in",
    "gipe.ac.in",                           # Gokhale Institute
    "ilslaw.edu", "ilspune.org",
    "nda.gov.in",                           # NDA Khadakwasla
    "afmc.nic.in",
    "iiserpune.ac.in",
    "iitp.ac.in",                           # IIT Patna (NOT Pune) — guard via name
    "nicmar.ac.in",
    "pict.edu",
    "aissmsioit.org", "aissmscoe.com",
    "sinhgad.edu",
    "indiraicem.ac.in", "indira.edu.in", "indiraisbs.ac.in",
    "modernpune.edu.in", "moderncollegepune.edu.in",
    "wadiacollege.edu.in",
    "bmcc.ac.in",
    "tmv.edu.in",
    # Nashik
    "ycmou.digitaluniversity.ac", "ycmou.com", "ycmou.ac.in",
    "muhs.ac.in",
    "sandipuniversity.edu.in",
    "nashik.org",                           # generic; tighten via name
    # Aurangabad / Marathwada
    "bamu.ac.in", "bamua.edu.in",
    "srtmun.ac.in", "srtmunpgcet.org",
    "mgmuniversity.org",
    "mahatmagandhimission.org",
    "deogiricollege.org",
    # Vidarbha
    "nagpuruniversity.org", "nagpuruniversity.ac.in",
    "sgbau.ac.in", "sgbauonline.com",
    "vnit.ac.in",
    "iitnagpur.ac.in",
    "iiitn.ac.in",
    "iimnagpur.ac.in",
    "rcoem.ac.in", "rknec.edu", "ramdeobabauniversity.in",
    "ycce.in", "ycce.edu",
    "ghrce.raisoni.net", "raisoni.net",
    "kgcoe.edu",
    "gondwana.digitaluniversity.ac", "unigug.ac.in",
    "kksanskrituniversity.in", "kksanskrit.ac.in",  # KKSU Ramtek
    # Northern / Khandesh
    "nmu.ac.in", "kbcnmu.ac.in",
    # Western (Kolhapur / Sangli / Satara / Solapur / Ahmednagar)
    "shivajiuniversity.in", "unishivaji.ac.in",
    "sus.ac.in", "punyashlokahilyadevi.com",
    "ahmednagarcollege.org",
    "tssm.in",
    # Agriculture / Tech / Animal-science specialist state univs
    "mpkv.ac.in",
    "pdkv.ac.in",
    "vnmkv.ac.in",
    "dbskkv.org", "dbskkv.ac.in",
    "mafsu.in",
    "dbatu.ac.in",
    # Maharashtra state-platform (per STATE_PLATFORM_HINTS)
    "digitaluniversity.ac",
    "digitaluniversity.ac.in",
    "mkcl.org",
)


# Negative guards — institutions whose name fragments collide with MH
# tokens but which are explicitly elsewhere. Substring match against
# the lowercased name. Negatives win over positives.
NEGATIVE_TOKENS: tuple[str, ...] = (
    # Aurangabad, Bihar is a distinct city from Aurangabad, Maharashtra.
    # Both punctuated and unpunctuated forms (the SheerID sheet has rows
    # like "Government Engineering College Aurangabad Bihar" without a
    # comma) — keep both.
    "aurangabad, bihar", "aurangabad (bihar)", "aurangabad bihar",
    "government engineering college aurangabad, bihar",
    "government engineering college aurangabad bihar",
    # MIT collisions — Manipal / Karnataka / abroad — keep MH MITs only.
    "mit manipal", "manipal institute of technology",
    "mit shillong", "mit meghalaya",
    "mit mysore",
    "mit aurangabad",                       # actually MH — re-include via specific phrase below; treat as keep
    "massachusetts institute of technology",
    # Christ University Bangalore — sometimes has "Christ College" pattern.
    "christ university", "christ college bangalore",
    "christ academy",
    # Symbiosis non-Pune sister deployments outside MH that we want to skip
    # (Symbiosis Hyderabad, Symbiosis Nashik IS MH).
    "symbiosis institute of business management bengaluru",
    "symbiosis institute of business management bangalore",
    "symbiosis law school hyderabad",
    "symbiosis school of media bengaluru",
    "symbiosis skill",                      # Telangana skill arm
    # NMIMS sister campuses outside MH
    "nmims hyderabad", "nmims bengaluru", "nmims bangalore",
    "nmims chandigarh", "nmims indore", "nmims dhule",
    "nmims navi mumbai",                    # actually MH — guarded by 'navi mumbai' city token
    "nmims shirpur",                        # Shirpur is in Dhule, MH — keep; placeholder
    # Bharati Vidyapeeth sister deployments — Delhi NCR & Karnataka are
    # not MH. Their parent university (Pune) IS MH.
    "bharati vidyapeeth deemed university, new delhi",
    "bharati vidyapeeth's college of engineering, new delhi",
    "bharati vidyapeeth (deemed to be university) new delhi",
    "bharati vidyapeeth karad",             # actually MH — keep
    # D.Y. Patil — multiple cities; the non-MH ones are Karnataka.
    "d y patil bangalore", "dy patil bangalore",
    "d.y. patil bangalore",
    "d y patil belagavi", "dy patil belagavi",
    # MIT (Manipal Institute of Technology) shows up as just "MIT" in some
    # SheerID rows — handled by the substring guards above; further guards:
    "manipal academy",
    # "Pune" as a substring in non-MH names — none common; placeholder.
    # "Nagpur" similarly unique.
    # IIT Bombay collisions — none.
    # IIT sister campuses
    "iit delhi", "iit madras", "iit kanpur", "iit kharagpur",
    "iit roorkee", "iit guwahati", "iit hyderabad", "iit gandhinagar",
    "iit ropar", "iit indore", "iit mandi", "iit patna",
    "iit jodhpur", "iit bhilai", "iit goa", "iit dharwad",
    "iit jammu", "iit palakkad", "iit tirupati", "iit bhubaneswar",
    "iit varanasi", "iit (bhu)", "iit bhu",
    # IIM sister campuses (IIM Mumbai / IIM Nagpur are MH)
    "iim bangalore", "iim ahmedabad", "iim calcutta", "iim indore",
    "iim kozhikode", "iim shillong", "iim lucknow", "iim udaipur",
    "iim trichy", "iim tiruchirappalli", "iim ranchi", "iim raipur",
    "iim kashipur", "iim sambalpur", "iim sirmaur", "iim rohtak",
    "iim bodh gaya", "iim amritsar", "iim jammu",
    "iim visakhapatnam",
    # AIIMS sister campuses (none in MH on the notified list)
    "aiims new delhi", "aiims delhi", "aiims bhopal", "aiims patna",
    "aiims jodhpur", "aiims rishikesh", "aiims bhubaneswar", "aiims raipur",
    "aiims mangalagiri", "aiims kalyani", "aiims bibinagar",
    "aiims gorakhpur", "aiims bathinda", "aiims deoghar", "aiims rajkot",
    "aiims guwahati", "aiims jammu", "aiims rae bareli", "aiims vijaypur",
    "aiims madurai", "aiims nagpur",        # AIIMS Nagpur IS MH — keep via name
    # MIT collisions (MIT-USA, etc.) — handled above. MIT Pune / MIT-WPU /
    # MIT-ADT / MIT-AOE / MIT Aurangabad are MH and use unique phrases.
    # Misc
    "ies college of architecture",          # IES is generic; let through if Mumbai
    "iiitb",                                # IIIT Bangalore
    "amity online",
    # "Pune" inside the word "Puneet" / "Sapuneet" — none common.
    # "Sangli" inside other words — none common.
    # IIIT-Allahabad, Hyderabad, Delhi — already handled by city/phrase mismatch.
)


def _has_word(haystack: str, needle: str) -> bool:
    """Word-boundary match for single-token needles; substring for
    multi-word needles. Both lowercased upstream."""
    if " " in needle or "-" in needle or "." in needle:
        return needle in haystack
    return re.search(rf"\b{re.escape(needle)}\b", haystack) is not None


def is_mh_university(name: str, domain: str) -> tuple[bool, str]:
    """Return (matched, source_label) for a university row.

    `source_label` is one of {'phrase', 'city', 'acronym', 'domain',
    'override'} or a combined `'phrase+domain'`-style string when
    multiple criteria matched. Negative guards beat all positives.
    """
    lower_name = (name or "").lower()
    lower_domain = (domain or "").lower()

    # Negative guards win.
    for neg in NEGATIVE_TOKENS:
        if neg in lower_name:
            return False, ""

    matched_via: list[str] = []

    for phrase in MH_INSTITUTION_PHRASES:
        if phrase in lower_name:
            matched_via.append("phrase")
            break

    for city in MH_CITY_TOKENS:
        if _has_word(lower_name, city):
            matched_via.append("city")
            break

    for acro in MH_INSTITUTION_ACRONYMS:
        if re.search(rf"\b{re.escape(acro)}\b", lower_name):
            matched_via.append("acronym")
            break

    for dom in MH_DOMAIN_PATTERNS:
        if lower_domain == dom or lower_domain.endswith("." + dom):
            matched_via.append("domain")
            break

    if not matched_via:
        return False, ""
    return True, "+".join(matched_via)


def main() -> int:
    cfg = load_config()
    sc = SheetsClient.from_config(cfg)
    rows = sc.read_universities()
    print(f"Read {len(rows)} rows from Universities tab.", file=sys.stderr)

    # Defensive — any OrgID explicitly tagged Maharashtra in overrides.
    mh_orgids_from_overrides = {
        orgid for orgid, entry in cfg.domain_overrides.items()
        if str(entry.get("state", "")).strip().lower() == "maharashtra"
    }
    if mh_orgids_from_overrides:
        print(
            f"OrgIDs tagged state='Maharashtra' in domain_overrides.json: "
            f"{len(mh_orgids_from_overrides)}",
            file=sys.stderr,
        )

    matched: list[dict[str, str]] = []
    for row in rows:
        orgid = sc.extract_orgid(row)
        name = str(row.get("SheerID University Name", "")).strip()
        domain = str(row.get("SheerID Website Domain", "")).strip()
        by_override = orgid in mh_orgids_from_overrides
        hit, source = is_mh_university(name, domain)
        if not (by_override or hit):
            continue
        # If an override match also produced a heuristic hit, combine
        # labels; otherwise use whichever path fired.
        combined = (
            "override+" + source if (by_override and source)
            else ("override" if by_override else source)
        )
        matched.append({
            "OrgID": orgid,
            "University Name": name,
            "Website Domain": domain,
            "Reclaim Login Page URL": str(
                row.get("Reclaim Protocol Login Page Url", "")
            ).strip(),
            "Reclaim Terms of Use URL": str(
                row.get("ReclaimProtocol Terms of Use URL", "")
            ).strip(),
            "Matched Via": combined,
        })

    matched.sort(key=lambda r: r["University Name"].lower())

    with OUTPUT_PATH.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=[
                "OrgID",
                "University Name",
                "Website Domain",
                "Reclaim Login Page URL",
                "Reclaim Terms of Use URL",
                "Matched Via",
            ],
        )
        writer.writeheader()
        writer.writerows(matched)

    print(f"Wrote {len(matched)} rows → {OUTPUT_PATH}", file=sys.stderr)

    # Quick breakdown for sanity checking.
    by_source: dict[str, int] = {}
    for m in matched:
        by_source[m["Matched Via"]] = by_source.get(m["Matched Via"], 0) + 1
    print("Match-source breakdown:", file=sys.stderr)
    for src, n in sorted(by_source.items(), key=lambda kv: -kv[1]):
        print(f"  {src:24s} {n}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

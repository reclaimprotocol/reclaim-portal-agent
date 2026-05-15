"""Export universities from the Delhi NCR region to a CSV file.

The SheerID Universities tab has no `state` / `region` column, so this
script identifies Delhi NCR universities heuristically by matching the
SheerID University Name (primary) and Website Domain (secondary)
against:

  * Explicit "Delhi" / "NCR" / "National Capital Region" mention.
  * NCR city / district names (per the Indian government's NCR
    notification): Delhi (NCT) plus the NCR districts of Haryana
    (Gurugram, Faridabad, Sonipat, Rohtak, Jhajjar, Karnal, Panipat,
    Bhiwani, Charkhi Dadri, Mahendragarh, Rewari, Palwal, Nuh),
    Uttar Pradesh (Noida / Gautam Buddha Nagar, Ghaziabad, Meerut,
    Hapur, Baghpat, Bulandshahr, Muzaffarnagar, Shamli) and
    Rajasthan (Alwar, Bharatpur).
  * Well-known NCR institution name phrases (DU, JNU, IIT Delhi,
    IIIT-D, IGNOU, GGSIPU, AUD, AIIMS Delhi, DTU, NSUT, Jamia,
    Sharda, Galgotias, Bennett, Shiv Nadar, Amity Noida, BML Munjal,
    Manav Rachna, O.P. Jindal Global, Ashoka, …).
  * Website domain matching well-known NCR root domains
    (`du.ac.in`, `uod.ac.in`, `jnu.ac.in`, `iitd.ac.in`, …) — catches
    DU colleges (Hindu, Hansraj, Lady Shri Ram, …) whose names alone
    don't tag them as NCR.

False-positive guard: a `NEGATIVE_TOKENS` blocklist removes
institutions whose name fragments collide with NCR keywords but which
are explicitly elsewhere (e.g. "Amity University Madhya Pradesh",
"Sharda University Uzbekistan", "IIM Bangalore").

Aligarh is deliberately *not* in the city list — though sometimes
loosely associated with the NCR economic region, it is not part of the
notified NCR. AMU students who want a Delhi-NCR cohort should use the
UP exporter instead.

Output: `delhi_ncr_universities.csv` in the repo root.
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
    Path(__file__).resolve().parent.parent / "delhi_ncr_universities.csv"
)

# NCR city / district tokens. Lowercased word-boundary match against the
# university name. The list mirrors the Government of India's NCR
# notification (Delhi NCT + Haryana / UP / Rajasthan sub-districts).
NCR_CITY_TOKENS: tuple[str, ...] = (
    # Delhi NCT
    "delhi", "new delhi",
    # Haryana NCR districts
    "gurugram", "gurgaon", "faridabad", "sonipat", "sonepat",
    "rohtak", "jhajjar", "karnal", "panipat", "bhiwani",
    "charkhi dadri", "mahendragarh", "mahendergarh", "rewari", "palwal",
    "nuh", "mewat", "manesar",
    # UP NCR districts
    "noida", "greater noida", "gautam buddha nagar", "gautam buddh nagar",
    "ghaziabad", "meerut", "modipuram", "hapur", "baghpat", "bulandshahr",
    "muzaffarnagar", "shamli",
    # Rajasthan NCR districts
    "alwar", "bharatpur", "bhiwadi",
)

# Multi-word phrases that strongly identify Delhi NCR institutions.
# Plain (case-insensitive) substring match against the lowercased name.
NCR_INSTITUTION_PHRASES: tuple[str, ...] = (
    # Generic regional
    "delhi ncr", "delhi-ncr", "national capital region",
    # Central / Delhi-government universities
    "university of delhi",
    "delhi university",
    "jawaharlal nehru university",
    "jamia millia",
    "jamia hamdard",
    "indira gandhi national open",      # IGNOU
    "guru gobind singh indraprastha",   # GGSIPU
    "ambedkar university delhi",
    "delhi technological university",
    "netaji subhas university of technology",
    "indraprastha institute of information technology",  # IIIT-D
    "national law university delhi",
    "south asian university",
    "teri school",                      # TERI School of Advanced Studies
    "aiims new delhi", "aiims delhi",
    "national institute of fashion technology",  # NIFT (Delhi HQ)
    "iit delhi", "indian institute of technology delhi",
    "iim rohtak", "indian institute of management rohtak",
    # Private NCR universities
    "sharda university",                # Greater Noida
    "galgotias",                        # Greater Noida
    "bennett university",               # Greater Noida
    "shiv nadar",                       # Greater Noida
    "gautam buddha university",         # Greater Noida
    "amity university uttar pradesh",
    "amity noida", "amity institute noida",
    "amity university haryana",
    "amity university gurgaon",
    "amity university gurugram",
    "jaypee institute of information technology",  # JIIT Noida
    "noida international university",
    "g.l. bajaj", "gl bajaj",          # Greater Noida
    "monad university",                 # Hapur
    "shobhit university",               # Meerut / Modipuram
    "iimt university",                  # Meerut / Greater Noida
    "iimt college",
    "chaudhary charan singh university",  # Meerut (CCSU)
    "subharti university",
    "swami vivekanand subharti",
    "iftm university",                  # near Moradabad — not NCR; guarded below
    "ymca university of science",       # Faridabad
    "j.c. bose university of science",  # Faridabad (renamed YMCA)
    "manav rachna",                     # Faridabad
    "lingaya",                          # Faridabad / Lingaya's Vidyapeeth
    "o.p. jindal global",               # Sonipat
    "op jindal global",
    "jindal global university",
    "ashoka university",                # Sonipat
    "bml munjal",                       # Gurugram
    "ansal university",                 # Gurugram
    "g.d. goenka", "gd goenka",         # Gurugram (Sohna Road)
    "icfai university gurgaon",
    "icfai university gurugram",
    "icfai university haryana",
    "rishihood university",             # Sonipat
    "world university of design",       # Sonipat
    "k.r. mangalam", "kr mangalam",     # Gurugram (KRMU)
    "starex university",                # Gurugram
    "il&fs institute",                  # Gurugram
    "iilm university",                  # Gurugram
    "northcap university",              # Gurugram
    "the northcap university",
    "iibm institute",                   # Greater Noida
    "raj kumar goel",                   # Ghaziabad
    "sri sri university",               # not NCR; guarded below
    "ims engineering",                  # Ghaziabad
    "inderprastha engineering",         # Ghaziabad
    "iec college",                      # Greater Noida
    "abes engineering",                 # Ghaziabad
    "abes institute",                   # Ghaziabad
    "ajay kumar garg",                  # Ghaziabad (AKGEC)
    "krishna institute of engineering",  # KIET Ghaziabad
    "kiet group",
    "maharshi dayanand university",     # Rohtak
    "pt. bhagwat dayal sharma",         # Rohtak (PGIMS)
    "deenbandhu chhotu ram",            # Murthal, Sonipat (DCRUST)
    "guru jambheshwar university",      # Hisar — guarded out below
    "ch. bansi lal university",         # Bhiwani
    "chaudhary bansi lal",
    "pt. lakhmi chand state university",  # Sonipat (performing arts)
    "central university of haryana",    # Mahendragarh
    "chaudhary devi lal university",    # Sirsa — guarded out below
    "babu banarasi das",                # Lucknow — guarded out below
    "raja balwant singh",
    "raja mahendra pratap singh",
    "sgt university",                   # Gurugram
    "vivekananda institute of professional studies",  # Delhi (VIPS)
    "pearl academy",                    # Delhi
    "asian academy of film",            # Noida
    "isbf",                             # Indian School of Business and Finance, Delhi
    "indian school of business and finance",
    "national institute of food technology",  # Kundli, Sonipat (NIFTEM)
    "niftem",
    "fdci",
    "amity school",                     # umbrella for Amity institutes
)

# Short institution acronyms unique enough to flag NCR membership.
# Matched with word boundaries to avoid substring false-positives.
NCR_INSTITUTION_ACRONYMS: tuple[str, ...] = (
    "du",          # Delhi University (risky — guarded by negatives)
    "jnu",         # Jawaharlal Nehru University
    "jmi",         # Jamia Millia Islamia
    "iitd",        # IIT Delhi
    "iiitd",       # IIIT-Delhi
    "ignou",       # Indira Gandhi National Open University
    "ggsipu", "ipu",  # GGSIPU
    "aud",         # Ambedkar University Delhi
    "dtu",         # Delhi Technological University
    "nsut", "nsit",  # Netaji Subhas University / Inst. of Technology
    "ccsu",        # Chaudhary Charan Singh, Meerut
    "ggsip",
    "dceu",        # Delhi College of Engineering (legacy)
    "krmu",        # K.R. Mangalam University, Gurugram
    "jiit",        # JIIT Noida
    "gbu",         # Gautam Buddha University, Greater Noida
    "ymcaust",     # YMCA UST, Faridabad
    "dcrust",      # Deenbandhu Chhotu Ram, Murthal
    "mdu",         # Maharshi Dayanand University, Rohtak
    "pgims",       # PGIMS Rohtak
)

# NCR-known website root domains. Substring match against the
# `SheerID Website Domain` column. Catches DU colleges (Hindu College,
# Hansraj College, Lady Shri Ram, …) whose institution names alone
# don't surface a city/region keyword. Tracks the SheerID-listed
# domain only; subdomains and overrides aren't consulted here.
NCR_DOMAIN_PATTERNS: tuple[str, ...] = (
    # Delhi
    "du.ac.in", "uod.ac.in", "jnu.ac.in", "jmi.ac.in", "jamiahamdard.edu",
    "iitd.ac.in", "iiitd.ac.in", "ignou.ac.in", "ipu.ac.in",
    "aud.ac.in", "dtu.ac.in", "nsut.ac.in", "nsit.ac.in",
    "sau.int", "sau.ac.in",
    "teriuniversity.ac.in", "terisas.ac.in",
    "aiims.edu", "aiims.ac.in",
    "nludelhi.ac.in",
    "nift.ac.in",                      # NIFT (national; Delhi HQ)
    "ndim.edu.in",                     # New Delhi Institute of Management
    "ducic.ac.in",                     # DU CIC
    "lsr.edu.in", "lsr.du.ac.in",      # Lady Shri Ram
    "stephens.edu",                    # St. Stephen's
    "hindu.du.ac.in", "hinducollege.ac.in",
    "hansrajcollege.ac.in",
    "miranda.du.ac.in", "mirandahouse.ac.in",
    "vips.edu",
    "pearlacademy.com",
    # Haryana NCR
    "mdurohtak.ac.in",
    "dcrustm.ac.in", "dcrustm.org",
    "pgimsrohtak.ac.in",
    "jindalglobal.edu.in", "jgu.edu.in",
    "ashoka.edu.in",
    "bmu.edu.in",                       # BML Munjal
    "ansaluniversity.edu.in",
    "manavrachna.edu.in", "manavrachna.org",
    "ymcaust.ac.in", "jcboseust.ac.in",
    "lingayasvidyapeeth.edu.in",
    "krmangalam.edu.in",
    "ncuindia.edu",                     # NorthCap University
    "gdgoenka.com", "gdgoenkauniversity.com",
    "iilm.edu",
    "sgtuniversity.org",
    "amity.edu",                        # umbrella; combined with name check
    "cuh.ac.in",                        # Central University of Haryana
    # UP NCR
    "sharda.ac.in", "galgotiasuniversity.edu.in",
    "bennett.edu.in", "snu.edu.in",     # Shiv Nadar
    "gbu.ac.in",
    "jiit.ac.in",
    "ccsuniversity.ac.in", "ccsuforms.in",
    "subharti.org",
    "shobhituniversity.ac.in",
    "iimtindia.net", "iimtu.edu.in",
    "monad.edu.in",
    "noidauniversity.com",              # Noida International University
    "raj.ac.in",                        # Raj Kumar Goel — variant
    "rkgit.edu.in",
    "imsec.ac.in",                      # IMS Engineering, Ghaziabad
    "iperindia.edu.in",                 # Inderprastha
    "abes.ac.in", "abesit.in",
    "akgec.ac.in",
    "kiet.edu",
    "iecit.in", "iecgroup.in",
    "ggsipu.ac.in",
    "niftem.ac.in",
    # Rajasthan NCR
    "iitmuniversity.ac.in",             # IIMT-style; guarded
)


# Negative guards — institutions whose name fragments collide with NCR
# tokens but which are explicitly elsewhere. Substring match against
# the lowercased name. Negatives win over positives.
NEGATIVE_TOKENS: tuple[str, ...] = (
    # Amity sister campuses outside NCR (Amity NCR is in Noida / Gurugram)
    "amity university madhya pradesh", "amity university gwalior",
    "amity university mumbai", "amity university maharashtra",
    "amity university jaipur", "amity university rajasthan",
    "amity university chhattisgarh", "amity university raipur",
    "amity university ranchi", "amity university jharkhand",
    "amity university kolkata", "amity university punjab",
    "amity university dubai", "amity university london",
    "amity university online",
    # ICFAI sister campuses outside NCR
    "icfai university hyderabad", "icfai university dehradun",
    "icfai university jaipur", "icfai university tripura",
    "icfai university jharkhand", "icfai university sikkim",
    "icfai university nagaland", "icfai university meghalaya",
    "icfai university himachal pradesh", "icfai university raipur",
    # IIM sister campuses outside NCR (IIM Rohtak is the only NCR one)
    "iim bangalore", "iim ahmedabad", "iim calcutta", "iim indore",
    "iim kozhikode", "iim shillong", "iim lucknow", "iim udaipur",
    "iim trichy", "iim tiruchirappalli", "iim ranchi", "iim raipur",
    "iim kashipur", "iim nagpur", "iim sambalpur", "iim sirmaur",
    "iim bodh gaya", "iim amritsar", "iim mumbai", "iim jammu",
    "iim visakhapatnam",
    # IIT sister campuses outside NCR
    "iit bombay", "iit madras", "iit kanpur", "iit kharagpur",
    "iit roorkee", "iit guwahati", "iit hyderabad", "iit gandhinagar",
    "iit ropar", "iit indore", "iit mandi", "iit patna",
    "iit jodhpur", "iit bhilai", "iit goa", "iit dharwad",
    "iit jammu", "iit palakkad", "iit tirupati", "iit bhubaneswar",
    "iit varanasi", "iit (bhu)", "iit bhu",
    # AIIMS sister campuses
    "aiims bhopal", "aiims patna", "aiims jodhpur", "aiims rishikesh",
    "aiims bhubaneswar", "aiims raipur", "aiims nagpur", "aiims mangalagiri",
    "aiims kalyani", "aiims bibinagar", "aiims gorakhpur", "aiims bathinda",
    "aiims deoghar", "aiims rajkot", "aiims guwahati", "aiims jammu",
    "aiims rae bareli", "aiims vijaypur", "aiims madurai",
    # Sharda / Galgotias / Bennett / Shiv Nadar — sister deployments
    "sharda university uzbekistan",
    "galgotias college of engineering and technology punjab",
    "shiv nadar school",                # K-12 schools, not the university
    "shiv nadar foundation",
    # JNU look-alikes elsewhere — none we know of
    # IFTM is in Moradabad, not NCR — explicitly excluded
    "iftm university",
    # Sri Sri University is in Odisha
    "sri sri university",
    # Babu Banarasi Das is in Lucknow (UP) — not NCR
    "babu banarasi das",
    # Guru Jambheshwar is Hisar (Haryana, but NOT NCR)
    "guru jambheshwar",
    # Chaudhary Devi Lal University is Sirsa (Haryana, but NOT NCR)
    "chaudhary devi lal university",
    # JNTU Hyderabad / Kakinada / Anantapur
    "jntu hyderabad", "jntu kakinada", "jntu anantapur",
    "jntuh", "jntuk", "jntua",
    # "Delhi Public School" / "Delhi World" — K-12, occasionally listed
    "delhi public school", "delhi world school",
    # Karnal / Panipat / Sonipat universities that are agriculture / vet
    # specialists belong here too — keep them, they ARE NCR.
    # "NIIT University" is Neemrana, Rajasthan (NOT in NCR — Neemrana
    # is in Alwar district which IS NCR; keep). Leave commented as
    # documentation:
    # "niit university",
    # NSU / NSUT Andhra (different acronym collision — none currently)
    # "Indian Statistical Institute Delhi" exists; let it pass via "delhi"
    # NorthCap was IILM Gurgaon — keep
    # Manipal University Jaipur — not NCR (Jaipur isn't NCR)
    "manipal university jaipur",
    # "Pearl Academy" has campuses in Mumbai / Jaipur too; the Delhi
    # one is the original. Let phrase match through; sister campuses
    # without their own OrgID will dedup naturally.
    # CGC / Chandigarh Group of Colleges — Chandigarh, not NCR
    "chandigarh university", "chandigarh group of colleges",
)


def _has_word(haystack: str, needle: str) -> bool:
    """Word-boundary match for single-token needles; substring for
    multi-word needles. Both lowercased upstream."""
    if " " in needle or "-" in needle or "." in needle:
        return needle in haystack
    return re.search(rf"\b{re.escape(needle)}\b", haystack) is not None


def is_ncr_university(name: str, domain: str) -> tuple[bool, str]:
    """Return (matched, source_label) for a university row.

    `source_label` is one of {'phrase', 'city', 'acronym', 'domain'} or
    a combined `'phrase+domain'`-style string when multiple criteria
    matched. Negative guards beat all positives.
    """
    lower_name = (name or "").lower()
    lower_domain = (domain or "").lower()

    # Negative guards win.
    for neg in NEGATIVE_TOKENS:
        if neg in lower_name:
            return False, ""

    matched_via: list[str] = []

    for phrase in NCR_INSTITUTION_PHRASES:
        if phrase in lower_name:
            matched_via.append("phrase")
            break

    for city in NCR_CITY_TOKENS:
        if _has_word(lower_name, city):
            matched_via.append("city")
            break

    for acro in NCR_INSTITUTION_ACRONYMS:
        if re.search(rf"\b{re.escape(acro)}\b", lower_name):
            matched_via.append("acronym")
            break

    for dom in NCR_DOMAIN_PATTERNS:
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

    matched: list[dict[str, str]] = []
    for row in rows:
        orgid = sc.extract_orgid(row)
        name = str(row.get("SheerID University Name", "")).strip()
        domain = str(row.get("SheerID Website Domain", "")).strip()
        hit, source = is_ncr_university(name, domain)
        if not hit:
            continue
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
            "Matched Via": source,
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
        print(f"  {src:20s} {n}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

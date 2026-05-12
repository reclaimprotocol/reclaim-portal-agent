"""Export universities from Uttar Pradesh to a CSV file.

The SheerID Universities tab has no `state` column, so this script
identifies UP universities heuristically by matching the university
name against:

  * Explicit "Uttar Pradesh" / "U.P." mention.
  * Major UP city / district names (Lucknow, Kanpur, Varanasi, …).
  * Well-known UP institution name fragments (BHU, AMU, AKTU,
    Rohilkhand, Bundelkhand, Avadh, Purvanchal, …).
  * Any OrgID whose `domain_overrides.json` entry has `state ==
    "Uttar Pradesh"`.

False-positive guard: city names that are ambiguous across India
(e.g. "Agra" → only Agra, UP; but "Mathura" is unambiguous) are kept
because they are UP-specific districts. The heuristic errs on the
side of inclusion; review the CSV before using it downstream.

Output: `up_universities.csv` in the repo root.
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

OUTPUT_PATH = Path(__file__).resolve().parent.parent / "up_universities.csv"

# UP cities / districts / regions. Lowercased substring match against
# the university name. These are largely unique to UP; the few that
# overlap with other states (none in this list) are intentionally
# omitted.
UP_CITY_TOKENS: tuple[str, ...] = (
    "lucknow", "kanpur", "varanasi", "banaras", "allahabad", "prayagraj",
    "noida", "ghaziabad", "meerut", "aligarh", "agra", "mathura", "bareilly",
    "gorakhpur", "jhansi", "faizabad", "ayodhya", "sultanpur", "moradabad",
    "saharanpur", "muzaffarnagar", "raebareli", "rae bareli", "bulandshahr",
    "etawah", "pratapgarh", "pilibhit", "hathras", "mainpuri", "mirzapur",
    "rampur", "shahjahanpur", "sitapur", "unnao", "hapur", "hardoi",
    "lakhimpur", "banda", "chitrakoot", "mahoba", "jaunpur", "azamgarh",
    "ballia", "deoria", "basti", "sambhal", "amroha", "bijnor", "budaun",
    "kasganj", "etah", "firozabad", "auraiya", "farrukhabad", "ghazipur",
    "kannauj", "mau", "kabir nagar", "bhadohi", "shrawasti", "gonda",
    "bahraich", "balrampur", "siddharthnagar", "sonbhadra", "chandauli",
    "kushinagar", "maharajganj", "fatehpur", "kaushambi", "amethi",
    "barabanki", "ambedkar nagar", "greater noida",
)

# UP-specific institution name phrases. Plain (case-insensitive)
# substring match against the lowercased name. Multi-word phrases —
# unlikely to collide with non-UP names.
UP_INSTITUTION_PHRASES: tuple[str, ...] = (
    "uttar pradesh",
    "u.p.",
    "banaras hindu",
    "aligarh muslim",
    "abdul kalam technical",
    "deen dayal upadhyay",      # matches both "upadhyay" and "upadhyaya"
    "mjp rohilkhand",
    "rohilkhand",
    "bundelkhand",
    "purvanchal",
    "iit kanpur",
    "iit (bhu)", "iit bhu",
    "iim lucknow",
    "iiit allahabad",
    "iiit lucknow",
    "motilal nehru",
    "amity university uttar pradesh",
    "amity noida",
    "amity institute noida",
    "sharda university",
    "galgotias",
    "bennett university",
    "shiv nadar",
    "gla university",           # GLA, Mathura
    "integral university",      # Integral, Lucknow
    "shobhit university",
    "sanskriti university",     # Mathura
    "monad university",         # Hapur
    "swami vivekanand subharti",
    "subharti university",
    "gautam buddh", "gautam buddha",
    "khwaja moinuddin chishti",
    "rajiv gandhi national aviation",
    "babasaheb bhimrao ambedkar",
    "chhatrapati shahu ji maharaj",
    "chaudhary charan singh university",
    "veer bahadur singh purvanchal",
    "raja balwant singh",
    "mahatma gandhi kashi vidyapith",
    "kashi vidyapith",
    "sampurnanand sanskrit",
    "central university of allahabad",
    "university of allahabad",
    "university of lucknow",
    "lucknow university",
    "babu banarasi das",
    "noida international university",
    "jamia hamdard noida",
    "raj kumar goel",
    "sri ramswaroop",
    "manyawar kanshiram",
    "dr. shakuntala mishra",
    "shakuntala misra",
    "atal bihari vajpayee medical university",
    "iftm university",
    "iimt college",
    "iimt university",
    "rama university",
    "iec college",
    "raja mahendra pratap singh",
    "rajkiya engineering college, sonbhadra",
)

# UP-specific short institution acronyms. Matched with word boundaries
# so they don't false-positive as substrings (e.g. "bhu" inside
# "Chanderprabhu", "amu" inside "Jamui", "ccs" inside arbitrary names).
UP_INSTITUTION_ACRONYMS: tuple[str, ...] = (
    "bhu", "amu", "aktu", "ccsu", "csjm", "mnnit", "iiita", "iimtu",
    "bbau", "bbd", "ddu", "rmlau", "vbspu",
)

# Negative guards — institution names that look like they might match a
# UP city/keyword but are explicitly elsewhere. Substring match.
NEGATIVE_TOKENS: tuple[str, ...] = (
    "amity university madhya pradesh", "amity university gwalior",
    "amity university mumbai", "amity university maharashtra",
    "amity university jaipur", "amity university rajasthan",
    "amity university chhattisgarh", "amity university raipur",
    "amity university ranchi", "amity university jharkhand",
    "amity university kolkata", "amity university punjab",
    "amity university haryana", "amity university dubai",
    "amity university london",
    "iim bangalore", "iim ahmedabad", "iim calcutta", "iim indore",
    "iim kozhikode", "iim shillong",
    "sharda university uzbekistan",
    "galgotias college of engineering and technology punjab",
    "baba banda singh",          # Fatehgarh Sahib, Punjab
    "bhupal nobles",             # Udaipur, Rajasthan
    "government engineering college jamui",  # Bihar
    "government polytechnic, bhilihili, azamgarh",  # actually UP — let through
    "narayana cbse",             # Karnataka (Ramamurthy Nagar)
    "narayana group",            # Karnataka
    "ssm college of engineering (baramulla",  # J&K
    "pramukhswami medical college",  # Karamsad, Gujarat
    "potti sriramulu",           # Andhra Pradesh
    "chanderprabhu jain college",  # Delhi
    "patkar",                    # Mumbai
    "chikitsak samuhas",         # Mumbai
    "ims unison",                # Dehradun, Uttarakhand
    "physics wallah",            # not a university; coaching chain
    "babasaheb bhimrao ambedkar bihar",  # BRABU Muzaffarpur, Bihar
    "jntuh",                     # Jawaharlal Nehru Tech Univ Hyderabad
    "motilal nehru college",     # DU college in Delhi (not MNNIT)
)
# Note: NEGATIVE_TOKENS has higher priority than positive matches.
# Re-include "government polytechnic, bhilihili, azamgarh" was dropped
# by removing it — actually the line above lists it but we want it kept.
# Filter that out:
NEGATIVE_TOKENS = tuple(
    t for t in NEGATIVE_TOKENS
    if t != "government polytechnic, bhilihili, azamgarh"
)


def is_up_university(name: str) -> bool:
    if not name:
        return False
    lower = name.lower()
    # Negative guards win over positives.
    for neg in NEGATIVE_TOKENS:
        if neg in lower:
            return False
    # Multi-word institution phrases — substring match.
    for phrase in UP_INSTITUTION_PHRASES:
        if phrase in lower:
            return True
    # Short acronyms — word-boundary match.
    for acro in UP_INSTITUTION_ACRONYMS:
        if re.search(rf"\b{re.escape(acro)}\b", lower):
            return True
    # City / district tokens.
    for city in UP_CITY_TOKENS:
        if " " in city:
            if city in lower:
                return True
        elif re.search(rf"\b{re.escape(city)}\b", lower):
            return True
    return False


def main() -> int:
    cfg = load_config()
    sc = SheetsClient.from_config(cfg)
    rows = sc.read_universities()
    print(f"Read {len(rows)} rows from Universities tab.", file=sys.stderr)

    # Pre-load OrgIDs explicitly tagged as Uttar Pradesh in
    # domain_overrides.json.
    up_orgids_from_overrides = {
        orgid for orgid, entry in cfg.domain_overrides.items()
        if str(entry.get("state", "")).strip().lower() == "uttar pradesh"
    }
    print(
        f"OrgIDs tagged state='Uttar Pradesh' in domain_overrides.json: "
        f"{len(up_orgids_from_overrides)}",
        file=sys.stderr,
    )

    matched: list[dict[str, str]] = []
    for row in rows:
        orgid = sc.extract_orgid(row)
        name = str(row.get("SheerID University Name", "")).strip()
        domain = str(row.get("SheerID Website Domain", "")).strip()
        by_override = orgid in up_orgids_from_overrides
        by_name = is_up_university(name)
        if not (by_override or by_name):
            continue
        matched.append({
            "OrgID": orgid,
            "University Name": name,
            "Website Domain": domain,
            "Source": "override+name" if by_override and by_name
                      else ("override" if by_override else "name"),
        })

    matched.sort(key=lambda r: r["University Name"].lower())

    with OUTPUT_PATH.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh, fieldnames=["OrgID", "University Name", "Website Domain", "Source"]
        )
        writer.writeheader()
        writer.writerows(matched)

    print(f"Wrote {len(matched)} rows → {OUTPUT_PATH}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Rebuild the Odisha office-sheet tab from scratch (it was cleared).

Writes header + all institutions (49 universities + the college list in
/tmp/odisha_list.txt) with Name | City | Category | Website | Portals URL,
matching the other state tabs. Website resolved via OpenRouter/Gemini.
"""
import re
import sys

sys.path.insert(0, "scripts")
import _bootstrap  # noqa: F401

import requests

from agent.config import OPENROUTER_API_KEY, OPENROUTER_MODEL, load_config
from agent.sheets_client import SheetsClient

SHEET = "153Eg9KheygBagf_6-IG4CbiadvzBZ6r04QQhGVBWoSQ"
HEADER = ["University Name", "City", "Category", "Website", "Portals URL"]

UNIS = [
    ("IIT Bhubaneswar", "Bhubaneswar", "Institute of National Importance"),
    ("NIT Rourkela", "Rourkela", "Institute of National Importance"),
    ("IIM Sambalpur", "Sambalpur", "Institute of National Importance"),
    ("AIIMS Bhubaneswar", "Bhubaneswar", "Institute of National Importance"),
    ("NISER Bhubaneswar", "Bhubaneswar", "Institute of National Importance"),
    ("IISER Brahmapur", "Brahmapur", "Institute of National Importance"),
    ("Central University of Odisha", "Koraput", "Central University"),
    ("Central Sanskrit University (Puri)", "Puri", "Central University"),
    ("Berhampur University", "Brahmapur", "State University"),
    ("Biju Patnaik University of Technology", "Rourkela", "State University"),
    ("Dharanidhar University", "Keonjhar", "State University"),
    ("Fakir Mohan University", "Balasore", "State University"),
    ("Gangadhar Meher University", "Sambalpur", "State University"),
    ("IIIT Bhubaneswar", "Bhubaneswar", "State University"),
    ("Khallikote Unitary University", "Brahmapur", "State University"),
    ("Maa Manikeshwari University", "Bhawanipatna", "State University"),
    ("Madhusudan Law University", "Cuttack", "State University"),
    ("Maharaja Sriram Chandra Bhanja Deo University", "Baripada", "State University"),
    ("National Law University Odisha", "Cuttack", "State University"),
    ("Odia University", "Satyabadi", "State University"),
    ("Odisha State Open University", "Sambalpur", "State University"),
    ("Odisha University of Agriculture and Technology", "Bhubaneswar", "State University"),
    ("Odisha University of Health Sciences", "Bhubaneswar", "State University"),
    ("Odisha University of Technology and Research", "Bhubaneswar", "State University"),
    ("Rajendra University", "Balangir", "State University"),
    ("Rama Devi Women's University", "Bhubaneswar", "State University"),
    ("Ravenshaw University", "Cuttack", "State University"),
    ("Sambalpur University", "Burla", "State University"),
    ("Shree Jagannath Sanskrit University", "Puri", "State University"),
    ("Utkal University", "Bhubaneswar", "State University"),
    ("Utkal University of Culture", "Bhubaneswar", "State University"),
    ("Veer Surendra Sai University of Technology", "Burla", "State University"),
    ("Vikram Dev University", "Jeypore", "State University"),
    ("Indian Institute of Mass Communication (Dhenkanal)", "Dhenkanal", "Deemed University"),
    ("Kalinga Institute of Industrial Technology", "Bhubaneswar", "Deemed University"),
    ("Kalinga Institute of Social Sciences", "Bhubaneswar", "Deemed University"),
    ("Siksha 'O' Anusandhan", "Bhubaneswar", "Deemed University"),
    ("AIPH University", "Bhubaneswar", "Private University"),
    ("ASBM University", "Bhubaneswar", "Private University"),
    ("Birla Global University", "Bhubaneswar", "Private University"),
    ("C. V. Raman Global University", "Bhubaneswar", "Private University"),
    ("Centurion University of Technology & Management", "Bhubaneswar", "Private University"),
    ("DRIEMS University", "Cuttack", "Private University"),
    ("GIET University", "Gunupur", "Private University"),
    ("Jagadguru Kripalu University", "Cuttack", "Private University"),
    ("NIST University", "Brahmapur", "Private University"),
    ("Silicon University", "Bhubaneswar", "Private University"),
    ("Sri Sri University", "Cuttack", "Private University"),
    ("XIM University", "Bhubaneswar", "Private University"),
]

_DOMAIN_RE = re.compile(r"^(?=.{4,253}$)([a-z0-9-]+\.)+[a-z]{2,}$")


def extract_domain(w: str) -> str:
    w = (w or "").strip()
    w = re.sub(r"^[a-zA-Z]+://", "", w).split("/")[0].split("?")[0].strip().lower()
    if w.startswith("www."):
        w = w[4:]
    return w if _DOMAIN_RE.match(w) else ""


def resolve_website(name: str, city: str) -> str:
    if not (OPENROUTER_API_KEY and name):
        return ""
    where = f", {city}," if city else ""
    prompt = (
        f'What is the official website domain of "{name}"{where} Odisha, '
        f"India? Return ONLY the bare hostname (e.g. example.ac.in) — no "
        f"scheme, no path, no explanation. If unknown, reply NONE."
    )
    try:
        r = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}"},
            json={"model": OPENROUTER_MODEL,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=40,
        )
        text = r.json()["choices"][0]["message"]["content"].strip()
    except Exception as err:
        print(f"  ! lookup failed for {name!r}: {err}")
        return ""
    for tok in re.split(r"[\s,]+", text):
        d = extract_domain(tok)
        if d:
            return f"https://{d}"
    return ""


def main() -> None:
    cfg = load_config()
    s = SheetsClient(sheet_id=SHEET, universities_tab="Odisha", portals_tab="Odisha",
                     credentials_path=cfg.google_credentials_path,
                     token_path=cfg.google_token_path)

    institutions = list(UNIS)
    for line in open("/tmp/odisha_list.txt"):
        line = line.strip()
        if not line:
            continue
        p = [x.strip() for x in line.split("|")]
        institutions.append((p[0], p[1], p[2]))
    print(f"institutions to write: {len(institutions)}")

    matrix = [HEADER]
    for i, (name, city, cat) in enumerate(institutions, start=1):
        web = resolve_website(name, city)
        matrix.append([name, city, cat, web, ""])
        print(f"  {i:3}/{len(institutions)} {name[:40]:42} | {city:13} | {web}")

    n = len(matrix)
    s._execute_with_retry(
        lambda: s._service.spreadsheets().values().update(
            spreadsheetId=SHEET, range=f"Odisha!A1:E{n}",
            valueInputOption="USER_ENTERED", body={"values": matrix}).execute(),
        label=f"rebuild Odisha {n} rows")
    filled = sum(1 for r in matrix[1:] if r[3])
    print(f"\nDONE — wrote header + {n-1} institutions; websites for {filled}/{n-1}.")


if __name__ == "__main__":
    main()

"""One-off: backfill City/Category/Website on the Odisha office-sheet tab.

- Rows 1-49 (the universities) were name-only → fill City/Category from the
  ordered Wikipedia university list below.
- Every row → resolve the official Website via the OpenRouter/Gemini
  name->domain resolver (same mechanism discovery uses).
Writes columns B:D (City, Category, Website) in a single range update.
"""
import re
import sys
import time

sys.path.insert(0, "scripts")
import _bootstrap  # noqa: F401

import requests

from agent.config import OPENROUTER_API_KEY, OPENROUTER_MODEL, load_config
from agent.sheets_client import SheetsClient

SHEET = "153Eg9KheygBagf_6-IG4CbiadvzBZ6r04QQhGVBWoSQ"

# Ordered City | Category for existing rows 1..49 (matches the tab order).
UNI_CC = [
    ("Bhubaneswar", "Institute of National Importance"),
    ("Rourkela", "Institute of National Importance"),
    ("Sambalpur", "Institute of National Importance"),
    ("Bhubaneswar", "Institute of National Importance"),
    ("Bhubaneswar", "Institute of National Importance"),
    ("Brahmapur", "Institute of National Importance"),
    ("Koraput", "Central University"),
    ("Puri", "Central University"),
    ("Brahmapur", "State University"),
    ("Rourkela", "State University"),
    ("Keonjhar", "State University"),
    ("Balasore", "State University"),
    ("Sambalpur", "State University"),
    ("Bhubaneswar", "State University"),
    ("Brahmapur", "State University"),
    ("Bhawanipatna", "State University"),
    ("Cuttack", "State University"),
    ("Baripada", "State University"),
    ("Cuttack", "State University"),
    ("Satyabadi", "State University"),
    ("Sambalpur", "State University"),
    ("Bhubaneswar", "State University"),
    ("Bhubaneswar", "State University"),
    ("Bhubaneswar", "State University"),
    ("Balangir", "State University"),
    ("Bhubaneswar", "State University"),
    ("Cuttack", "State University"),
    ("Burla", "State University"),
    ("Puri", "State University"),
    ("Bhubaneswar", "State University"),
    ("Bhubaneswar", "State University"),
    ("Burla", "State University"),
    ("Jeypore", "State University"),
    ("Dhenkanal", "Deemed University"),
    ("Bhubaneswar", "Deemed University"),
    ("Bhubaneswar", "Deemed University"),
    ("Bhubaneswar", "Deemed University"),
    ("Bhubaneswar", "Private University"),
    ("Bhubaneswar", "Private University"),
    ("Bhubaneswar", "Private University"),
    ("Bhubaneswar", "Private University"),
    ("Bhubaneswar", "Private University"),
    ("Cuttack", "Private University"),
    ("Gunupur", "Private University"),
    ("Cuttack", "Private University"),
    ("Brahmapur", "Private University"),
    ("Bhubaneswar", "Private University"),
    ("Cuttack", "Private University"),
    ("Bhubaneswar", "Private University"),
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
    rows = s._get_values("Odisha", "2:100000")
    n = len(rows)
    print(f"Odisha data rows: {n}")
    out = []  # B,C,D per row
    for i, r in enumerate(rows):
        name = (r[0] if len(r) > 0 else "").strip()
        city = (r[1] if len(r) > 1 else "").strip()
        cat = (r[2] if len(r) > 2 else "").strip()
        web = (r[3] if len(r) > 3 else "").strip()
        if i < len(UNI_CC):
            city = city or UNI_CC[i][0]
            cat = cat or UNI_CC[i][1]
        if not web and name:
            web = resolve_website(name, city)
        out.append([city, cat, web])
        print(f"  {i+1:3}/{n} {name[:40]:42} | {city:14} | {web}")
    body = {"values": out}
    s._execute_with_retry(
        lambda: s._service.spreadsheets().values().update(
            spreadsheetId=SHEET, range=f"Odisha!B2:D{n+1}",
            valueInputOption="USER_ENTERED", body=body).execute(),
        label="update Odisha B:D")
    filled = sum(1 for o in out if o[2])
    print(f"\nDONE — updated {n} rows; websites resolved for {filled}/{n}.")


if __name__ == "__main__":
    main()

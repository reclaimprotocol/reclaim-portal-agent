#!/usr/bin/env python3
"""Scrape the full list of colleges affiliated to National University, Bangladesh
from Wikipedia and write NU_Bangladesh_colleges.csv.

The page has no website column; tables carry: College Code, Name, District,
Type, EIIN, Address. We parse every wikitable and normalise columns by header.
"""
from __future__ import annotations

import csv
import re

import _bootstrap  # noqa: F401
import requests
from bs4 import BeautifulSoup

URL = ("https://en.wikipedia.org/wiki/"
       "List_of_affiliated_colleges_and_institutions_to_the_National_University,_Bangladesh")
OUT = "NU_Bangladesh_colleges.csv"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# canonical output columns -> header keywords to match
COLS = {
    "code":     ["college code", "code"],
    "name":     ["college name", "name of", "institution", "name"],
    "district": ["district"],
    "type":     ["type"],
    "eiin":     ["eiin"],
    "address":  ["address", "location", "area"],
}


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s.replace("\xa0", " ")).strip()


def _map_headers(header_cells: list[str]) -> dict[str, int]:
    mapping = {}
    for idx, h in enumerate(header_cells):
        hl = h.lower()
        for canon, keys in COLS.items():
            if canon in mapping:
                continue
            if any(k in hl for k in keys):
                mapping[canon] = idx
                break
    return mapping


resp = requests.get(URL, headers={"User-Agent": UA}, timeout=30)
resp.raise_for_status()
soup = BeautifulSoup(resp.text, "html.parser")

rows_out = []
seen = set()
tables = soup.select("table.wikitable")
for t in tables:
    trs = t.find_all("tr")
    if not trs:
        continue
    header = [_clean(c.get_text(" ")) for c in trs[0].find_all(["th", "td"])]
    m = _map_headers(header)
    if "name" not in m:        # not a college table
        continue
    for tr in trs[1:]:
        cells = [_clean(c.get_text(" ")) for c in tr.find_all(["td", "th"])]
        if not cells:
            continue
        def get(k):
            i = m.get(k)
            return cells[i] if i is not None and i < len(cells) else ""
        name = get("name")
        if not name or name.lower() in ("name", "college name"):
            continue
        rec = (get("code"), name, get("district"), get("type"), get("eiin"), get("address"))
        key = (rec[1], rec[2], rec[4])  # name+district+eiin
        if key in seen:
            continue
        seen.add(key)
        rows_out.append(rec)

with open(OUT, "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["College Code", "College Name", "District", "Type", "EIIN", "Address"])
    w.writerows(rows_out)

print(f"tables parsed     : {len(tables)}")
print(f"college rows       : {len(rows_out)}  -> {OUT}")
print("sample:")
for r in rows_out[:8]:
    print("  ", r)

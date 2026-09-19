"""Genie-V3 service · the CSV contract.

INPUT — one row per organisation:

    orgId,website,current_portals,name,country

`current_portals` holds the portals you already have, separated by `|`
(semicolons, newlines and spaces also work; commas do NOT, because a URL may
legitimately contain one and the field is already inside a CSV).

Headers are resolved by NAME, with aliases, never by position — the same rule
the Sheets reader follows, and for the same reason: column layouts change and a
position-indexed reader silently reads the wrong one.

OUTPUT — one row per organisation ALWAYS, plus extra rows when an org has more
than one new portal:

    orgId,status,new_portal_url,category,system,tnc_url,tnc_reason,confidence,
    match_basis,http_status,suppressed_as_known,dead_known_portals,error

`status` is `new_found` | `none_found` | `failed`. The absence of a row is
never the answer — "we crawled it and found nothing new", "it errored" and "it
was never processed" have to be three distinguishable states, or a partial run
reads as a complete one.

`suppressed_as_known` and `dead_known_portals` are org-level facts repeated on
each of that org's rows, so the file stays flat and joins on orgId.
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from typing import Iterator

#: Accepted spellings per field. Mirrors the sheet reader's aliases so a tab
#: exported straight to CSV works with no editing.
_ALIASES: dict[str, tuple[str, ...]] = {
    "org_id": ("orgid", "org id", "organization id", "organisation id", "org_id", "id"),
    "website": ("website", "website domain", "email domains", "domains", "domain",
                "url", "site"),
    "current_portals": ("current_portals", "current portals", "portals",
                        "portal urls", "existing portals", "known portals"),
    "name": ("name", "org name", "organization name", "organisation name",
             "university name"),
    "country": ("country",),
}

_SPLIT = re.compile(r"[|;\n\r\t ]+")

OUTPUT_COLUMNS = [
    "orgId", "status", "new_portal_url", "category", "system", "tnc_url",
    "tnc_reason", "confidence", "match_basis", "http_status",
    "suppressed_as_known", "dead_known_portals", "error",
]

STATUS_NEW, STATUS_NONE, STATUS_FAILED = "new_found", "none_found", "failed"


@dataclass
class InputRow:
    org_id: str
    website: str
    current_portals: list[str] = field(default_factory=list)
    name: str = ""
    country: str = ""


class CsvContractError(ValueError):
    """The upload cannot be processed at all — wrong headers, or no rows."""


def _resolve(header: list[str]) -> dict[str, int]:
    """Map our field names to column indexes, case- and space-insensitively."""
    idx = {str(h or "").strip().lower(): i for i, h in enumerate(header)}
    found: dict[str, int] = {}
    for fieldname, names in _ALIASES.items():
        for n in names:
            if n in idx:
                found[fieldname] = idx[n]
                break
    return found


def split_portals(raw: str) -> list[str]:
    """Split the current_portals cell, preserving order and dropping blanks."""
    out, seen = [], set()
    for part in _SPLIT.split((raw or "").strip()):
        u = part.strip().strip(",")
        if u and u.lower() not in seen:
            seen.add(u.lower())
            out.append(u)
    return out


def parse_input(data: bytes) -> list[InputRow]:
    """Parse an uploaded CSV into rows, or raise CsvContractError.

    Decoding is utf-8-sig then latin-1: a sheet exported from Excel carries a
    BOM, which would otherwise make the first header literally '\\ufeffOrg ID'
    and silently fail to resolve.
    """
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("latin-1", errors="replace")

    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        raise CsvContractError("the uploaded file is empty") from None

    cols = _resolve(header)
    missing = [f for f in ("org_id", "website") if f not in cols]
    if missing:
        raise CsvContractError(
            f"required column(s) {missing} not found in header {header}. "
            f"orgId accepts {_ALIASES['org_id']}; website accepts {_ALIASES['website']}")

    def cell(r: list[str], key: str) -> str:
        i = cols.get(key)
        return str(r[i]).strip() if i is not None and i < len(r) else ""

    rows: list[InputRow] = []
    for raw in reader:
        if not any(str(c).strip() for c in raw):
            continue                                   # blank line
        org_id, website = cell(raw, "org_id"), cell(raw, "website")
        if not (org_id and website):
            continue                                   # unusable row
        rows.append(InputRow(
            org_id=org_id, website=website,
            current_portals=split_portals(cell(raw, "current_portals")),
            name=cell(raw, "name"), country=cell(raw, "country")))

    if not rows:
        raise CsvContractError("no usable rows — every row needs orgId and website")
    return rows


def open_results(path) -> None:
    """Create results.csv with just its header, so a run that finds nothing
    still yields a well-formed file rather than a zero-byte one."""
    with open(path, "w", newline="", encoding="utf-8") as fh:
        csv.DictWriter(fh, fieldnames=OUTPUT_COLUMNS).writeheader()


def append_results(path, rows: Iterator[dict] | list[dict]) -> None:
    """Append and flush per org, so results.csv is downloadable mid-run and a
    killed container keeps everything already finished."""
    rows = list(rows)
    if not rows:
        return
    with open(path, "a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=OUTPUT_COLUMNS, extrasaction="ignore")
        for r in rows:
            w.writerow({k: r.get(k, "") for k in OUTPUT_COLUMNS})
        fh.flush()

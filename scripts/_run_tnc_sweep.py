#!/usr/bin/env python3
"""Fill/validate T&C for already-portal-reviewed rows that still lack a T&C
verdict — e.g. the first review window that got resolve+dedup before T&C was
folded into the reviewer.

Targets Portals TnC rows where: E (portal review) is filled and is NOT a removal
note, the portal is real, and H (T&C review) is empty. For each, validate an
existing http T&C or fill one via the waterfall (find_tnc on the resolved
portal/D). Excludes orgs in the active review window (portals_review_window.json)
so it never races the running reviewer. In-place, resumable via its own cache.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts"))
try:
    from dotenv import load_dotenv; load_dotenv(ROOT / ".env")
except Exception:  # noqa: BLE001
    pass

import _bootstrap  # noqa: F401,E402
import agent.magic as M  # noqa: E402
from agent import magic_tnc as T  # noqa: E402
from agent.config import load_config  # noqa: E402
from agent.sheets_client import SheetsClient  # noqa: E402

SHEET = "1sDK_1VnRHIuUqBComrvwS1JvSmB_l0_4Rsf9rfezFNw"
TAB = "Portals TnC"
ACTIVE_WINDOW = ROOT / "portals_review_window.json"   # orgs the live reviewer owns
DONE_CACHE = ROOT / "tnc_sweep_done.json"
_REMOVAL = re.compile(r"remove|duplicate|junk|dead|block", re.I)


def _retry(fn, n=4):
    for i in range(n):
        try:
            return fn()
        except Exception:  # noqa: BLE001
            if i == n - 1:
                raise
            time.sleep(2 * (i + 1))


def _load(p):
    try:
        return set(json.loads(p.read_text()))
    except Exception:  # noqa: BLE001
        return set()


def _tnc(g, endpoint, dom, name, cache):
    proot = M._registrable_root(M._norm_host(endpoint)) or ""
    uroot = (M._registrable_root(M._norm_host("http://" + dom.split(",")[0].strip()))
             if dom else proot)
    g = (g or "").strip()
    if g.lower().startswith("http"):
        if not T._is_valid_tnc(g, proot, uroot):
            return g, "invalid T&C (junk/consent/app/law-landing) — replace"
        return g, "ok — valid policy page"
    try:
        res = T.find_tnc(endpoint, dom.split(",")[0].strip() if dom else "", name, "", cache=cache)
        items = res.get("tncs") or []
    except Exception as e:  # noqa: BLE001
        return "N/A", f"find_tnc error ({type(e).__name__})"
    if items:
        extra = f" (+{len(items)-1} more)" if len(items) > 1 else ""
        return items[0]["url"], f"auto-added T&C ({res.get('tnc_level','')}){extra}"
    return "N/A", "auto: no T&C found (waterfall)"


def _todo(svc):
    active = _load(ACTIVE_WINDOW)
    done = _load(DONE_CACHE)
    rows = _retry(lambda: svc.values().get(spreadsheetId=SHEET, range=f"'{TAB}'!A2:H").execute()).get("values", [])
    out = []
    for i, r in enumerate(rows, 2):
        r = (r + [""] * 8)[:8]
        oid, name, dom, portal, e, cat, g, h = [c.strip() if isinstance(c, str) else c for c in r]
        if (oid and oid not in active and e and not _REMOVAL.search(e)
                and portal and portal != "(none found)" and not h and f"{i}" not in done):
            out.append((i, name, dom, portal, g))
    return out, done


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report-remaining", action="store_true")
    args = ap.parse_args()
    sc = SheetsClient.from_config(load_config()); sc.sheet_id = SHEET
    svc = sc._service.spreadsheets()
    todo, done = _todo(svc)
    if args.report_remaining:
        print(len(todo)); return

    print(f"tnc-sweep: {len(todo)} rows need T&C (excludes active review window)", flush=True)
    cache: dict = {}
    for n, (row, name, dom, portal, g) in enumerate(todo, 1):
        gnew, hnote = _tnc(g, portal, dom, name, cache)
        data = [{"range": f"'{TAB}'!H{row}", "values": [[hnote]]}]
        if gnew and gnew != g:
            data.append({"range": f"'{TAB}'!G{row}", "values": [[gnew]]})
        _retry(lambda: svc.values().batchUpdate(spreadsheetId=SHEET,
               body={"valueInputOption": "RAW", "data": data}).execute())
        done.add(f"{row}")
        if n % 10 == 0 or n == len(todo):
            DONE_CACHE.write_text(json.dumps(sorted(done)))
        print(f"  [{n}/{len(todo)}] row{row} {name[:24]:24} -> {hnote[:40]}", flush=True)
    DONE_CACHE.write_text(json.dumps(sorted(done)))
    print("DONE", flush=True)


if __name__ == "__main__":
    main()

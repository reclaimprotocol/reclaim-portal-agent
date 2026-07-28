#!/usr/bin/env python3
"""Auto-review a window of orgs in the **Portals TnC** tab: check each portal URL
(is it a real student login?) and its T&C, filling a relevant T&C where missing.

  Portals TnC: A oid | B name | C domains | D portal | E portal-review | F cat | G tnc | H tnc-review

Per portal row (E empty) in the window:
  - fetch static -> JS-render; detect Cloudflare/WAF BLOCK pages, bare IdP hosts,
    search/listing sub-pages, dead hosts, and login affordance. Write E verdict;
    color red for clear removes.
  - T&C: if G is N/A/empty and the portal isn't a remove, run magic_tnc.find_tnc
    (the waterfall) and put the primary policy in G; validate an existing G URL.
    Write H verdict. (In-place only — no rows added/deleted, so it's resumable.)

Window = the next 30 distinct FULLY-UNREVIEWED orgs (no E on any of their rows),
frozen to a cache file so daemon restarts stay stable.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts"))
try:
    from dotenv import load_dotenv; load_dotenv(ROOT / ".env")
except Exception:  # noqa: BLE001
    pass

import _bootstrap  # noqa: F401,E402
import requests  # noqa: E402
import agent.magic as M  # noqa: E402
from agent import magic_tnc as T  # noqa: E402
from agent.stages.js_renderer import JSRenderer, _has_password_input, looks_like_block_page  # noqa: E402
from agent.config import load_config  # noqa: E402
from agent.sheets_client import SheetsClient  # noqa: E402

SHEET = "1sDK_1VnRHIuUqBComrvwS1JvSmB_l0_4Rsf9rfezFNw"
TAB = "Portals TnC"
WINDOW = 30
CACHE = ROOT / "portals_review_window.json"

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
_LOGIN_TXT = re.compile(r"log[\s\-]?in|sign[\s\-]?in|entrar|acess|ingresar|iniciar\s+sesi|"
                        r"usuario|contrase|password|clave", re.I)
_SUBPAGE = re.compile(r"/(ayuda|fecha_examen|menu|help|about|acerca|contacto|noticias|faq|"
                      r"preguntas|foro|forum|mod/forum|calendar)", re.I)
_jr = None


def _retry(fn, n=4):
    for i in range(n):
        try:
            return fn()
        except Exception:  # noqa: BLE001
            if i == n - 1:
                raise
            time.sleep(2 * (i + 1))


def _fetch(u):
    try:
        r = requests.get(u, headers=UA, timeout=15, verify=False, allow_redirects=True)
        return r.status_code, r.url, (r.text or "")
    except Exception:  # noqa: BLE001
        return 0, u, ""


def _render(u):
    global _jr
    if _jr is None:
        _jr = JSRenderer(timeout_seconds=30)
    return _jr.render(u)


def review_portal(url, root_hosts):
    host = M._norm_host(url); sp = urlsplit(url); path = sp.path.rstrip("/")
    if host.startswith("idp."):
        return "red", "bare IdP host, no login page — remove"
    if M._is_junk_portal(url):
        if _SUBPAGE.search(url):
            return "red", "internal sub-page, not a login — remove (keep portal root)"
        return "red", "junk (search/asset/idp-metadata/redirect) — remove"
    if path and _SUBPAGE.search(url) and host in root_hosts:
        return "red", "duplicate sub-page of the portal root — remove"
    st, final, html = _fetch(url)
    low = (html or "").lower()
    if _has_password_input(html) or (low.count("<form") and low.count("<input") >= 2):
        return "green", "ok — login form present"
    res = _render(url)
    if res.ok:
        h = res.html or ""; hl = h.lower()
        if looks_like_block_page(h):
            return "amber", "WAF/Cloudflare block page from our region — likely geo-blocked; verify via VPN"
        if _has_password_input(h) or (hl.count("<form") and hl.count("<input") >= 2) or _LOGIN_TXT.search(h):
            return "green", "ok — login present (JS-rendered)"
        return "amber", "live but no login element found — verify it's a login portal"
    if st in (401, 403, 429):
        return "red", f"returns {st} to bots and fails to render — likely blocked/dead, verify/remove"
    return "red", f"unreachable (status {st}, render failed) — remove"


def review_or_fill_tnc(g, portal, dom, name, cache):
    proot = M._registrable_root(M._norm_host(portal)) or ""
    uroot = (M._registrable_root(M._norm_host("http://" + dom.split(",")[0].strip()))
             if dom else proot)
    g = (g or "").strip()
    if g and g.lower().startswith("http"):
        if not T._is_valid_tnc(g, proot, uroot):
            return g, "red", "invalid T&C (junk/consent/app/law-landing) — replace"
        st, _, _ = _fetch(g)
        if st == 0 or st >= 400:
            return g, "amber", f"T&C url not reachable ({st}) — verify"
        return g, "green", "ok — valid policy page"
    # empty or N/A -> waterfall find_tnc
    try:
        res = T.find_tnc(portal, dom.split(",")[0].strip() if dom else "", name, "", cache=cache)
        items = res.get("tncs") or []
    except Exception as e:  # noqa: BLE001
        return "N/A", "amber", f"find_tnc error ({type(e).__name__})"
    if items:
        extra = f" (+{len(items)-1} more docs)" if len(items) > 1 else ""
        return items[0]["url"], "green", f"auto-added ({res.get('tnc_level','')}){extra}"
    return "N/A", "amber", "auto: no T&C found (waterfall)"


def _load_rows(svc):
    return _retry(lambda: svc.values().get(spreadsheetId=SHEET, range=f"'{TAB}'!A2:H").execute()).get("values", [])


def _window(rows):
    if CACHE.exists():
        try:
            return set(json.loads(CACHE.read_text()))
        except Exception:  # noqa: BLE001
            pass
    reviewed = set()
    for r in rows:
        oid = (r[0].strip() if r and r[0] else "")
        if oid and len(r) > 4 and (r[4].strip() if r[4] else ""):
            reviewed.add(oid)
    win, seen = [], set()
    for r in rows:
        oid = (r[0].strip() if r and r[0] else "")
        if oid and oid not in reviewed and oid not in seen:
            seen.add(oid); win.append(oid)
        if len(win) >= WINDOW:
            break
    CACHE.write_text(json.dumps(win))
    return set(win)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report-remaining", action="store_true")
    args = ap.parse_args()

    sc = SheetsClient.from_config(load_config()); sc.sheet_id = SHEET
    svc = sc._service.spreadsheets()
    rows = _load_rows(svc)
    window = _window(rows)
    root_hosts = {M._norm_host(r[3].strip()) for r in rows
                  if len(r) > 3 and r[3] and urlsplit(r[3].strip()).path.rstrip("/") == ""}

    todo = []
    for i, r in enumerate(rows, 2):
        oid = (r[0].strip() if r and r[0] else "")
        e = (r[4].strip() if len(r) > 4 and r[4] else "")
        portal = (r[3].strip() if len(r) > 3 and r[3] else "")
        if oid in window and not e and portal and portal != "(none found)":
            todo.append((i, r))

    if args.report_remaining:
        print(len(todo)); return

    print(f"Portals TnC review: window {len(window)} orgs | {len(todo)} portal rows to review", flush=True)
    gid = next(s["properties"]["sheetId"] for s in _retry(lambda: svc.get(spreadsheetId=SHEET).execute())["sheets"]
               if s["properties"]["title"] == TAB)
    RED = {"red": 0.85, "green": 0.55, "blue": 0.55}
    tcache: dict = {}
    for n, (row, r) in enumerate(todo, 1):
        r = (r + [""] * 8)[:8]
        oid, name, dom, portal, _, cat, g, _h = r
        pc, pf = review_portal(portal, root_hosts)
        if pc == "red":
            gnew, tf = g or "N/A", "(portal flagged for removal)"
        else:
            gnew, _tc, tf = review_or_fill_tnc(g, portal, dom, name, tcache)
        data = [{"range": f"'{TAB}'!E{row}", "values": [[pf]]},
                {"range": f"'{TAB}'!H{row}", "values": [[tf]]}]
        if gnew and gnew != g:
            data.append({"range": f"'{TAB}'!G{row}", "values": [[gnew]]})
        _retry(lambda: svc.values().batchUpdate(spreadsheetId=SHEET,
               body={"valueInputOption": "RAW", "data": data}).execute())
        if pc == "red":
            _retry(lambda: svc.batchUpdate(spreadsheetId=SHEET, body={"requests": [{"repeatCell": {
                "range": {"sheetId": gid, "startRowIndex": row - 1, "endRowIndex": row,
                          "startColumnIndex": 0, "endColumnIndex": 8},
                "cell": {"userEnteredFormat": {"backgroundColor": RED}},
                "fields": "userEnteredFormat.backgroundColor"}}]}).execute())
        print(f"  [{n}/{len(todo)}] row{row} {name[:22]:22} P[{pc}] {portal[:40]}", flush=True)
    print("DONE", flush=True)


if __name__ == "__main__":
    main()

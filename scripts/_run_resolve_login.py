#!/usr/bin/env python3
"""Resolve each Portals TnC portal to its EXACT login endpoint, and dedup portals
that lead to the same page.

For each portal (D) in the review window:
  1. render the page (the JS renderer auto-clicks login buttons, so same-page
     MODAL logins are captured);
  2. if the page has a login FORM / password field (inline or modal) -> it IS the
     login endpoint, keep D as-is (the user's stated exception);
  3. else if the page only LINKS to a login (a "Login"/"Entrar"/… hyperlink to
     another URL) -> follow it, verify that target has a login form, and REPLACE D
     with that exact login endpoint;
  4. after resolving all of an org's portals, collapse duplicates: rows whose
     resolved endpoint canonicalizes to the same URL are marked red "duplicate".

In-place (updates D / appends to E / colors dups red; never deletes) and
resumable via a processed-cache.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit, urljoin

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts"))
try:
    from dotenv import load_dotenv; load_dotenv(ROOT / ".env")
except Exception:  # noqa: BLE001
    pass

import _bootstrap  # noqa: F401,E402
import requests  # noqa: E402
import os  # noqa: E402
from bs4 import BeautifulSoup  # noqa: E402
import agent.magic as M  # noqa: E402
from agent import magic_tnc as T  # noqa: E402
from agent.stages.js_renderer import JSRenderer, _has_password_input, looks_like_block_page  # noqa: E402
from agent.config import load_config  # noqa: E402
from agent.sheets_client import SheetsClient  # noqa: E402

SHEET = "1sDK_1VnRHIuUqBComrvwS1JvSmB_l0_4Rsf9rfezFNw"
TAB = "Portals TnC"
WINDOW = int(os.getenv("MAGIC_REVIEW_WINDOW", "30"))    # distinct orgs per run
WINDOW_CACHE = ROOT / "portals_review_window.json"
DONE_CACHE = ROOT / "resolve_login_done.json"

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
# explicit login-ACTION anchors only — NOT broad nav words like "portal"/"aluno"
# (a "Student Portal" nav link pointing at the homepage must not count as login).
_LOGIN_A = re.compile(r"log[\s\-]?in\b|sign[\s\-]?in\b|logon\b|\bentrar\b|acessar|acesse|"
                      r"ingresar|iniciar\s*sesi|identifica[cç]|autentica[cç]|"
                      r"área\s*do\s*aluno|portal\s*do\s*aluno", re.I)
_LOGIN_URLISH = re.compile(r"login|signin|sign-in|logon|/sso|/cas|/entrar|identifica|"
                           r"autentica|/auth\b|oauth|shibboleth|/ingresar|iniciar", re.I)
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


def _has_form(html):
    low = (html or "").lower()
    return _has_password_input(html) or (low.count("<form") and low.count("<input") >= 2)


def _login_link(html, base):
    """Best login-link href on the page (absolute), or None."""
    try:
        soup = BeautifulSoup(html or "", "html.parser")
    except Exception:  # noqa: BLE001
        return None
    cands = []
    for a in soup.find_all("a", href=True):
        href = a["href"]; text = (a.get_text() or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:")):
            continue
        absu = urljoin(base, href)
        if _LOGIN_A.search(text) or _LOGIN_URLISH.search(href):
            cands.append(absu)
    if not cands:
        return None
    base_root = M._registrable_root(M._norm_host(base)) or ""
    # prefer: URL-shaped login token, then same registrable root, then shortest

    def score(u):
        return (0 if _LOGIN_URLISH.search(u) else 1,
                0 if (M._registrable_root(M._norm_host(u)) or "") == base_root else 1,
                len(u))
    return sorted(dict.fromkeys(cands), key=score)[0]


def _canon(u):
    sp = urlsplit((u or "").lower())
    return f"{sp.netloc}{sp.path.rstrip('/')}?{sp.query}".rstrip("?")


def resolve(url):
    """Return (endpoint, mode). mode: junk | blocked | same_page | resolved | dead | keep.

    Priority: (1) if the raw page already has a PASSWORD field it IS the login page
    — keep. (2) else FOLLOW a login/Entrar link to its exact endpoint and verify a
    password field there — this is what catches hub pages like /nossos-aprovados ->
    /identificacao. (3) else render (auto-clicks a login button) and if a password
    field appears it's a same-page MODAL — keep. A generic form/search box is NOT a
    login — only a password field counts."""
    if M._norm_host(url).startswith("idp.") or M._is_junk_portal(url):
        return url, "junk"
    sp = urlsplit(url)
    url_is_endpoint = bool(_LOGIN_URLISH.search(sp.path + "?" + (sp.query or "")))
    st, final, html = _fetch(url)
    base = final or url

    def _follow(link):
        if not link or _canon(link) == _canon(url):
            return None
        lp = urlsplit(link)
        login_urlish = bool(_LOGIN_URLISH.search(lp.path + "?" + (lp.query or "")))
        is_root = lp.path.rstrip("/") == ""      # a bare homepage is NEVER "the login endpoint"
        # a login-shaped URL is a strong enough signal on its own
        if login_urlish and not is_root:
            return (_fetch(link)[1] or link)
        if is_root:
            return None                          # don't collapse onto a homepage
        # non-root, non-login-URL: accept only if the target actually has a password form
        st2, f2, h2 = _fetch(link)
        if _has_password_input(h2):
            return f2 or link
        r2 = _render(link)
        h2r = r2.html if (r2 and r2.ok) else ""
        if _has_password_input(h2r):
            return f2 or link
        return None

    # 1. URL is already a dedicated login endpoint (…/login, /identificacao, /sso …) -> keep
    if url_is_endpoint:
        return url, "same_page"

    # 2. hub/marketing URL: PREFER a dedicated login/Entrar link over an embedded
    #    modal (sites often ship a hidden login modal in every page's HTML).
    ep = _follow(_login_link(html, base))
    if ep:
        return ep, "resolved"

    # 3. render (auto-clicks login buttons); link visible only after JS, then modal
    res = _render(url)
    rhtml = res.html if (res and res.ok) else ""
    if rhtml and looks_like_block_page(rhtml):
        return url, "blocked"
    ep = _follow(_login_link(rhtml, base))
    if ep:
        return ep, "resolved"
    # 4. no navigable login link anywhere -> a real on-page/modal password form is kept
    if _has_password_input(html) or _has_password_input(rhtml):
        return url, "same_page"
    if not rhtml and st in (0, 404, 500, 502, 503, 526):
        return url, "dead"
    return url, "keep"


def _load_rows(svc):
    return _retry(lambda: svc.values().get(spreadsheetId=SHEET, range=f"'{TAB}'!A2:H").execute()).get("values", [])


def _window(rows):
    """Reuse a frozen window cache, else build one: the next WINDOW distinct orgs
    that are FULLY unreviewed (no E on any of their rows)."""
    if WINDOW_CACHE.exists():
        try:
            return set(json.loads(WINDOW_CACHE.read_text()))
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
    WINDOW_CACHE.write_text(json.dumps(win))
    return set(win)


def _done():
    try:
        return set(json.loads(DONE_CACHE.read_text()))
    except Exception:  # noqa: BLE001
        return set()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report-remaining", action="store_true")
    args = ap.parse_args()

    sc = SheetsClient.from_config(load_config()); sc.sheet_id = SHEET
    svc = sc._service.spreadsheets()
    rows = _load_rows(svc)
    window = _window(rows)
    done = _done()

    todo = []  # (rownum, orgid, name, dom, portal, tnc)
    for i, r in enumerate(rows, 2):
        oid = (r[0].strip() if r and r[0] else "")
        portal = (r[3].strip() if len(r) > 3 and r[3] else "")
        name = (r[1].strip() if len(r) > 1 and r[1] else "")
        dom = (r[2].strip() if len(r) > 2 and r[2] else "")
        tnc = (r[6].strip() if len(r) > 6 and r[6] else "")
        if oid in window and portal and portal != "(none found)" and f"{i}" not in done:
            todo.append((i, oid, name, dom, portal, tnc))

    if args.report_remaining:
        print(len(todo)); return

    print(f"resolve-login: window {len(window)} orgs | {len(todo)} portal rows", flush=True)
    gid = next(s["properties"]["sheetId"] for s in _retry(lambda: svc.get(spreadsheetId=SHEET).execute())["sheets"]
               if s["properties"]["title"] == TAB)
    RED = {"red": 0.85, "green": 0.55, "blue": 0.55}
    WHITE = {"red": 1, "green": 1, "blue": 1}
    NOTE = {
        "junk": "junk / IdP / search page — remove",
        "blocked": "WAF block page — could not resolve endpoint; verify via VPN",
        "same_page": "login on same page (inline/modal) — kept",
        "dead": "unreachable / dead host — verify/remove",
        "keep": "kept — no separate login link found on page",
    }
    # a row is a true DUPLICATE only if the login endpoint AND the T&C match another
    # row of the same org — same portal with two different T&C docs is legitimate.
    seen: dict = {}   # (orgid, canon endpoint, canon tnc) -> first rownum
    tcache: dict = {}  # per-run uni/vendor T&C memo

    def _tnc(g, endpoint, dom, name):
        """Return (gnew, hnote): validate an existing T&C, or fill via waterfall."""
        proot = M._registrable_root(M._norm_host(endpoint)) or ""
        uroot = (M._registrable_root(M._norm_host("http://" + dom.split(",")[0].strip()))
                 if dom else proot)
        g = (g or "").strip()
        if g.lower().startswith("http"):
            if not T._is_valid_tnc(g, proot, uroot):
                return g, "invalid T&C (junk/consent/app/law-landing) — replace"
            return g, "ok — valid policy page"
        try:
            res = T.find_tnc(endpoint, dom.split(",")[0].strip() if dom else "", name, "", cache=tcache)
            items = res.get("tncs") or []
        except Exception as e:  # noqa: BLE001
            return "N/A", f"find_tnc error ({type(e).__name__})"
        if items:
            extra = f" (+{len(items)-1} more)" if len(items) > 1 else ""
            return items[0]["url"], f"auto-added T&C ({res.get('tnc_level','')}){extra}"
        return "N/A", "auto: no T&C found (waterfall)"

    for n, (row, oid, name, dom, portal, tnc) in enumerate(todo, 1):
        endpoint, mode = resolve(portal)
        if mode == "resolved" and _canon(endpoint) != _canon(portal):
            note = f"resolved to exact login endpoint: {endpoint}"
        else:
            note = NOTE.get(mode, "reviewed")
        key = (oid, _canon(endpoint), _canon(tnc))
        dup = key in seen
        if dup:
            note = f"duplicate — same endpoint ({endpoint}) + T&C as row {seen[key]} — remove"
        else:
            seen[key] = row
        red = dup or mode in ("junk", "dead")

        # T&C: skip for removal rows; else validate/fill against the resolved endpoint
        if red:
            gnew, hnote = tnc, "(row flagged for removal)"
        else:
            gnew, hnote = _tnc(tnc, endpoint, dom, name)

        data = []
        # write the resolved endpoint into D for every kept/resolved row so the
        # portal column always shows the ACTUAL endpoint the dedup keyed on
        # (junk/dead keep their original URL for the removal note).
        if mode not in ("junk", "dead") and _canon(endpoint) != _canon(portal):
            data.append({"range": f"'{TAB}'!D{row}", "values": [[endpoint]]})
        data.append({"range": f"'{TAB}'!E{row}", "values": [[note]]})
        if gnew and gnew != tnc:
            data.append({"range": f"'{TAB}'!G{row}", "values": [[gnew]]})
        data.append({"range": f"'{TAB}'!H{row}", "values": [[hnote]]})
        _retry(lambda: svc.values().batchUpdate(spreadsheetId=SHEET,
               body={"valueInputOption": "RAW", "data": data}).execute())
        _retry(lambda: svc.batchUpdate(spreadsheetId=SHEET, body={"requests": [{"repeatCell": {
            "range": {"sheetId": gid, "startRowIndex": row - 1, "endRowIndex": row,
                      "startColumnIndex": 0, "endColumnIndex": 8},
            "cell": {"userEnteredFormat": {"backgroundColor": RED if red else WHITE}},
            "fields": "userEnteredFormat.backgroundColor"}}]}).execute())
        done.add(f"{row}")
        if n % 10 == 0 or n == len(todo):
            DONE_CACHE.write_text(json.dumps(sorted(done)))
        print(f"  [{n}/{len(todo)}] row{row} [{mode}{' DUP' if dup else ''}] {portal[:36]} "
              f"{'-> '+endpoint[:42] if (mode=='resolved' and not dup) else ''}", flush=True)
    DONE_CACHE.write_text(json.dumps(sorted(done)))
    print("DONE", flush=True)


if __name__ == "__main__":
    main()

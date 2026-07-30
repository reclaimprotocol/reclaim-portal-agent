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
import os
import re
import signal
import sys
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit

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
WINDOW = int(os.getenv("MAGIC_REVIEW_WINDOW", "50"))
CACHE = ROOT / "portals_review_window.json"

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
_LOGIN_TXT = re.compile(r"log[\s\-]?in|sign[\s\-]?in|entrar|acess|ingresar|iniciar\s+sesi|"
                        r"usuario|contrase|password|clave", re.I)
_SUBPAGE = re.compile(r"/(ayuda|fecha_examen|menu|help|about|acerca|contacto|noticias|faq|"
                      r"preguntas|foro|forum|mod/forum|calendar)", re.I)
# Documentation / student-handbook sites — NOT a login portal (e.g.
# manualdoaluno.ebpos.com.br is a GitBook manual, not the login). They often
# mention "login/entrar/acesso" in prose, which used to false-green them. We try
# to follow their login link to the real endpoint; if none, they're removed.
_DOC_RE = re.compile(r"(?:^|//|\.)(?:manualdoaluno|docs?|ajuda|suporte|support|help|faq|wiki|kb|"
                     r"tutorial|guia|guide)\.|gitbook\.io|readthedocs|/(?:manual|ajuda|docs?|"
                     r"help|faq|suporte|wiki|tutoriais?)(?:/|$)", re.I)
# Login-ACTION anchors (text or href) — used to resolve a hub/manual page to the
# exact login endpoint. Explicit login actions only, not broad nav words.
_LOGIN_A = re.compile(r"log[\s\-]?in|sign[\s\-]?in|entrar|acessar|acesso\s+ao\s+portal|"
                      r"ingresar|iniciar\s+sesi|identifica|autentica|portal\s+do\s+aluno|"
                      r"[aá]rea\s+do\s+aluno|clicar\s+aqui|acesse\s+aqui|clique\s+aqui", re.I)


def _has_login_form(html: str) -> bool:
    h = (html or "").lower()
    return bool(_has_password_input(html) or (h.count("<form") and h.count("<input") >= 2))


def _login_endpoint(base_url: str, html: str):
    """Follow login-ACTION links on a hub/manual page to find the exact login
    endpoint (a page that actually exposes a password form). One hop, a few
    candidates; returns the resolved URL or None. Bounded by the per-row alarm."""
    from bs4 import BeautifulSoup  # local import; bs4 ships with the project
    try:
        soup = BeautifulSoup(html or "", "html.parser")
    except Exception:  # noqa: BLE001
        return None
    base_canon = urlsplit(base_url).geturl().split("#")[0].rstrip("/")
    cands, seen = [], set()
    for a in soup.find_all("a", href=True):
        txt = (a.get_text() or "").strip()
        href = urljoin(base_url, a["href"]).split("#")[0]
        if not href.lower().startswith("http") or href in seen:
            continue
        if _LOGIN_A.search(txt) or _LOGIN_A.search(href):
            seen.add(href); cands.append(href)
    for cand in cands[:5]:
        if cand.split("#")[0].rstrip("/") == base_canon or _DOC_RE.search(cand):
            continue  # no progress / another doc page
        st, fin, ch = _fetch(cand)
        if _has_login_form(ch):
            return fin or cand
        rr = _render(cand)
        if rr.ok and not looks_like_block_page(rr.html or "") and _has_login_form(rr.html or ""):
            return rr.final_url or cand
    return None
# Tenant-specific / SP-initiated SSO login = a REAL university student login, even
# though it lives on a provider host (Azure AD, ADFS). Distinct from a BARE
# consumer sign-in (login.microsoftonline.com/ with no tenant) which stays junk.
# Signals: an Azure AD tenant GUID in the path, an ADFS /adfs/ls/ endpoint, or an
# SP-initiated flow carrying SAMLRequest= / wtrealm= (the university is the SP).
# IdP *metadata* URLs (saml2/idp/metadata, /idp/shibboleth) carry none of these,
# so they're not rescued here — they stay junk.
_TENANT_SSO = re.compile(
    r"login\.microsoftonline\.com/[0-9a-f]{8}-[0-9a-f]{4}-|/adfs/ls/|"
    r"[?&]SAMLRequest=|[?&]wtrealm=|/o/oauth2/|/oauth2/authorize", re.I)
# Indian universities are OUT OF SCOPE — never review them, and if one is in the
# list it should be removed. Detected by an Indian ccTLD on the org's domain/portal.
def _is_indian(*fields: str) -> bool:
    for f in fields:
        for tok in (f or "").replace(",", " ").split():
            host = urlsplit(tok if "://" in tok else "http://" + tok).netloc or tok
            host = host.lower().strip().rstrip(".")
            if host.endswith(".in"):
                return True
    return False
# Webmail / email-login endpoints — broader than magic._is_webmail (which only
# catches an exact "webmail"/"mail" FIRST host label). Also catches hosts like
# webmail-seguro.com.br where "webmail" is embedded in the label, plus common
# webmail-app paths/ports. Email logins are not student academic portals.
_WEBMAIL_RE = re.compile(r"webmail|roundcube|squirrelmail|horde|zimbra|rainloop|afterlogic|"
                         r"/owa(?:/|$|\?)|:209[56]", re.I)
# ...UNLESS the endpoint is explicitly a STUDENT webmail ("for students only").
_STUDENT_HINT = re.compile(r"student|alumn|aluno|estudante|discente|scholar|matric", re.I)


def _is_webmail(url: str) -> bool:
    host = M._norm_host(url); first = host.split(".")[0]
    if "webmail" in host or first in M._WEBMAIL_LABELS or first.endswith("mail"):
        return True
    return bool(_WEBMAIL_RE.search(url or ""))


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
        r = requests.get(u, headers=UA, timeout=15, verify=False, allow_redirects=True,
                         proxies=M._proxies(u))
        return r.status_code, r.url, (r.text or "")
    except Exception:  # noqa: BLE001
        return 0, u, ""


def _render(u):
    global _jr
    if _jr is None:
        _jr = JSRenderer(timeout_seconds=int(os.getenv("MAGIC_RENDER_TIMEOUT", "20")))
    return _jr.render(u)


def _reset_renderer():
    """Abandon the current browser (used after a per-row timeout, since the
    interrupted render may have left it wedged). Best-effort close, then null so
    the next row spins up a fresh one. Catches BaseException because the bounded
    close below can itself trip the SIGALRM -> _RowTimeout (a BaseException); it
    must NOT escape this function."""
    global _jr
    old, _jr = _jr, None
    try:
        signal.alarm(5)
        if old is not None:
            old.close()
    except BaseException:  # noqa: BLE001  (incl. _RowTimeout from the guard alarm)
        pass
    finally:
        signal.alarm(0)


class _RowTimeout(BaseException):
    # BaseException (not Exception) so the broad `except Exception` blocks inside
    # the JS renderer / fetch helpers can't swallow it — it must propagate up to
    # the per-row handler that marks the row N/A.
    pass


def _on_alarm(signum, frame):  # noqa: ARG001
    raise _RowTimeout()


def review_portal(url, root_hosts):
    """Returns (color, note, resolved_url). resolved_url is a NEW exact login
    endpoint to write into col D when we followed a login link off a hub/manual
    page; None means keep the URL as-is."""
    host = M._norm_host(url); sp = urlsplit(url); path = sp.path.rstrip("/")
    if host.startswith("idp."):
        return "red", "bare IdP host, no login page — remove", None
    if _is_indian(url):
        return "red", "Indian university — out of scope, remove", None
    if _TENANT_SSO.search(url):
        return "green", "ok — tenant/federated SSO login (Azure AD/ADFS)", None
    if M._is_junk_portal(url):
        if _SUBPAGE.search(url):
            return "red", "internal sub-page, not a login — remove (keep portal root)", None
        return "red", "junk (search/asset/idp-metadata/redirect) — remove", None
    if _is_webmail(url) and not _STUDENT_HINT.search(url):
        return "red", "webmail/email login (not student-only) — remove", None
    if path and _SUBPAGE.search(url) and host in root_hosts:
        return "red", "duplicate sub-page of the portal root — remove", None

    st, final, html = _fetch(url)
    if _has_login_form(html):
        return "green", "ok — login form present", None
    res = _render(url)
    rhtml = res.html if (res and res.ok) else ""
    if rhtml and looks_like_block_page(rhtml):
        return "amber", "WAF/Cloudflare block page from our region — likely geo-blocked; verify via VPN", None
    if _has_login_form(rhtml):
        return "green", "ok — login form present (JS-rendered)", None

    # No login FORM on this page. Before giving up, try to RESOLVE the exact
    # login endpoint by following a login-action link (hub/manual pages only
    # LINK to the real login; the page itself isn't a login — the EBPÓS manual
    # case). Mere login TEXT is no longer enough to pass.
    ep = _login_endpoint(final or url, rhtml or html)
    if ep:
        return "green", "resolved to exact login endpoint (followed login link)", ep
    if _DOC_RE.search(final or url):
        return "red", "documentation/manual/help page, not a login — remove", None
    if res and res.ok:
        return "red", "no login form or login link found — not a login page, remove", None
    if st in (401, 403, 429):
        return "red", f"returns {st} to bots and fails to render — likely blocked/dead, verify/remove", None
    return "red", f"unreachable (status {st}, render failed) — remove", None


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
    # Window = next WINDOW orgs that have a REAL portal with at least one
    # not-yet-reviewed row. Orgs whose only entry is "(none found)" contribute
    # no reviewable rows, so they must NOT occupy window slots (else they'd sit
    # at the top of every window forever and starve real orgs). An org is "done"
    # only when every one of its real-portal rows has a col-E verdict.
    real_total: dict[str, int] = {}
    real_done: dict[str, int] = {}
    indian: set[str] = set()
    for r in rows:
        r = (r + [""] * 8)[:8]
        oid = (r[0].strip() if r[0] else "")
        dom = (r[2].strip() if r[2] else "")
        portal = (r[3].strip() if r[3] else "")
        e = (r[4].strip() if r[4] else "")
        if oid and _is_indian(dom, portal):
            indian.add(oid)   # out of scope — never window an Indian uni
        if oid and portal and portal != "(none found)":
            real_total[oid] = real_total.get(oid, 0) + 1
            if e:
                real_done[oid] = real_done.get(oid, 0) + 1
    win, seen = [], set()
    for r in rows:
        oid = (r[0].strip() if r and r[0] else "")
        if (oid and oid in real_total and oid not in seen and oid not in indian
                and real_done.get(oid, 0) < real_total[oid]):
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

    # Process at most BATCH rows per invocation, then exit(0). The driver loop
    # re-launches us with a FRESH browser process, recycling Chromium memory so
    # a long window can't build up until the OS kills the whole tree. Remaining
    # rows are picked up on the next invocation (idempotent — todo is empty-E).
    batch = int(os.getenv("MAGIC_REVIEW_BATCH", "40"))
    if batch > 0 and len(todo) > batch:
        print(f"(processing {batch} of {len(todo)} this invocation; driver will resume)", flush=True)
        todo = todo[:batch]

    print(f"Portals TnC review: window {len(window)} orgs | {len(todo)} portal rows to review", flush=True)
    gid = next(s["properties"]["sheetId"] for s in _retry(lambda: svc.get(spreadsheetId=SHEET).execute())["sheets"]
               if s["properties"]["title"] == TAB)
    RED = {"red": 0.85, "green": 0.55, "blue": 0.55}
    tcache: dict = {}
    # Hard per-row wall-clock cap: a single hanging/slow URL must never block the
    # whole batch. On timeout, mark the row N/A and move on (user rule 2026-07-30).
    row_timeout = int(os.getenv("MAGIC_ROW_TIMEOUT", "70"))
    signal.signal(signal.SIGALRM, _on_alarm)
    for n, (row, r) in enumerate(todo, 1):
        r = (r + [""] * 8)[:8]
        oid, name, dom, portal, _, cat, g, _h = r
        resolved = None
        try:
            signal.alarm(row_timeout)
            pc, pf, resolved = review_portal(portal, root_hosts)
            eff_portal = resolved or portal   # resolve T&C against the real endpoint
            if pc == "red":
                gnew, tf = g or "N/A", "(portal flagged for removal)"
            else:
                gnew, _tc, tf = review_or_fill_tnc(g, eff_portal, dom, name, tcache)
            signal.alarm(0)
        except _RowTimeout:
            signal.alarm(0)
            _reset_renderer()
            pc, pf = "amber", f"review timed out (>{row_timeout}s) — N/A, verify manually"
            gnew, tf = g or "N/A", "(review timed out)"
        data = [{"range": f"'{TAB}'!E{row}", "values": [[pf]]},
                {"range": f"'{TAB}'!H{row}", "values": [[tf]]}]
        if resolved and resolved != portal:
            data.append({"range": f"'{TAB}'!D{row}", "values": [[resolved]]})
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

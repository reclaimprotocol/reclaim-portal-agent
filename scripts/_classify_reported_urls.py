#!/usr/bin/env python3
"""Classify the JulyBatch **Reported URLs** tab (student-reported portal URLs).

Dedupes to unique URLs, sums the per-row reportsCount into a frequency, then runs
the agent's portal-validity rules over each unique URL in three stages:

  A  URL-only rules (free): malformed/junk/webmail/payment/doc/content/IdP,
     tenant-SSO and login-URL-shape greens.  (The out-of-scope "Indian" rule from
     _run_portals_review is deliberately NOT applied — all orgs are in scope.)
  B  threaded HTTP fetch: login-form detection, dead/blocked/404 hosts.
  C  JS render (serial) for whatever stage B left ambiguous, incl. following a
     login-action link to the exact endpoint.

Verdicts: KEEP (real student login) / REVIEW (needs eyes) / DROP (remove).
Resumable — every stage checkpoints to verdicts.json.

Usage:  .venv/bin/python scripts/_classify_reported_urls.py --stage all
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import re
import signal
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
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
import urllib3  # noqa: E402
urllib3.disable_warnings()

import agent.magic as M  # noqa: E402
import _run_portals_review as R  # noqa: E402 — reuse the reviewed rule set verbatim
from agent.config import load_config  # noqa: E402
from agent.sheets_client import SheetsClient  # noqa: E402

SHEET = "1sDK_1VnRHIuUqBComrvwS1JvSmB_l0_4Rsf9rfezFNw"
SRC_TAB = "Reported URLs"
STATE = ROOT / "reported_urls_verdicts.json"
RAW = ROOT / "reported_urls_raw.json"

IPV4 = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")
NUMERIC = re.compile(r"^\d+$")
SEARCH_RE = re.compile(r"(?:^|\.)(?:google|bing|duckduckgo|yandex|ask)\.[a-z.]+$", re.I)
SOCIAL_RE = re.compile(r"(?:^|\.)(?:facebook|fb|instagram|twitter|youtube|youtu\.be|linkedin|"
                       r"whatsapp|telegram|t\.me|pinterest|tiktok|reddit|quora)\.", re.I)
STORE_RE = re.compile(r"(?:^|\.)(?:play\.google|apps\.apple|itunes\.apple)\.", re.I)
ASSET_RE = re.compile(r"\.(?:pdf|jpe?g|png|gif|webp|docx?|xlsx?|pptx?|zip|rar|apk|mp4|mp3|csv)$", re.I)


def _norm(u: str) -> str:
    u = (u or "").strip()
    if not u:
        return ""
    s = urlsplit(u)
    host = (s.netloc or "").lower().rstrip(".")
    for port in (":80", ":443"):
        if host.endswith(port):
            host = host[: -len(port)]
    q = f"?{s.query}" if s.query else ""
    return f"{(s.scheme or 'http').lower()}://{host}{(s.path or '').rstrip('/')}{q}"


def stage_a(url: str):
    """URL-only verdict. Returns (verdict, reason) or (None, None) to continue."""
    s = urlsplit(url)
    host = (s.netloc or "").lower()
    if s.scheme not in ("http", "https"):
        return "DROP", "malformed: unsupported scheme"
    if not host:
        return "DROP", "malformed: no host"
    if "@" in host:
        return "DROP", "malformed: email address / credentials in host"
    if " " in url.strip():
        return "DROP", "malformed: whitespace — two URLs pasted together"
    if NUMERIC.match(host):
        return "DROP", "malformed: numeric host (roll number / ID typed as URL)"
    if IPV4.match(host.split(":")[0]):
        return "DROP", "invalid: raw IP address, not a named portal host"
    if host == "localhost" or host.endswith(".local"):
        return "DROP", "invalid: localhost / LAN-only host"
    if "." not in host:
        return "DROP", "malformed: hostname has no dot (partial text, not a URL)"
    if SEARCH_RE.search(host):
        return "DROP", "junk: search engine / search-redirect URL"
    if SOCIAL_RE.search(host):
        return "DROP", "junk: social media link"
    if STORE_RE.search(host):
        return "DROP", "junk: app-store link"
    if ASSET_RE.search(s.path or ""):
        return "DROP", "junk: file/asset link (pdf/image/doc)"
    if host.startswith("idp."):
        return "DROP", "bare IdP host — SAML metadata, never a login page"
    if R._TENANT_SSO.search(url):
        return "KEEP", "tenant/federated SSO login (Azure AD / ADFS / OAuth)"
    if M._is_junk_portal(url):
        if R._SUBPAGE.search(url):
            return "DROP", "internal sub-page, not a login"
        return "DROP", "junk (search/asset/IdP-metadata/redirect) per agent rule"
    if R._is_webmail(url) and not R._STUDENT_HINT.search(url):
        return "DROP", "webmail / email login, not a student academic portal"
    if R._PAYMENT_RE.search(url):
        return "DROP", "payment / checkout gateway, not a student login"
    return None, None


def _fetch(u):
    try:
        r = requests.get(u, headers=R.UA, timeout=15, verify=False,
                         allow_redirects=True, proxies=M._proxies(u))
        return r.status_code, r.url, (r.text or "")
    except Exception:  # noqa: BLE001
        return 0, u, ""


def stage_b(url: str):
    """HTTP-only verdict. AMBIGUOUS -> stage C."""
    st, final, html = _fetch(url)
    if html and R._has_login_form(html):
        return "KEEP", "login form present", final, st
    reachable = bool(st) and st != 0
    if reachable and R._LOGIN_URLISH.search(final or url):
        return "KEEP", "login endpoint (URL-shaped, reachable)", final, st
    if st == 404:
        return "DROP", "stale: 404 not found", final, st
    if st and st >= 500:
        return "AMBIGUOUS", f"server error {st}", final, st
    return "AMBIGUOUS", f"no login form via HTTP (status {st})", final, st


def stage_c(url: str, st: int):
    """JS render + login-link resolution. Mirrors review_portal's tail."""
    try:
        signal.alarm(int(os.getenv("ROW_TIMEOUT", "70")))
        res = R._render(url)
        rhtml = res.html if (res and res.ok) else ""
        if rhtml and R.looks_like_block_page(rhtml):
            return "REVIEW", "WAF/Cloudflare block from our region — verify via VPN", None
        if R._has_login_form(rhtml):
            return "KEEP", "login form present (JS-rendered)", None
        reachable = (st and st != 0) or bool(res and res.ok)
        if reachable and R._LOGIN_URLISH.search(url):
            return "KEEP", "login endpoint (URL-shaped, reachable)", None
        ep = R._login_endpoint(url, rhtml or "")
        if ep:
            return "KEEP", "resolved to exact login endpoint (followed login link)", ep
        if R._DOC_RE.search(url):
            return "DROP", "documentation / manual / help page, not a login", None
        if R._CONTENT_RE.search(url):
            return "DROP", "content page (library/news/info), not a login", None
        if reachable and R._PORTAL_HINT.search(url):
            return "REVIEW", "portal/LMS host — login form not auto-detected, verify manually", None
        if res and res.ok:
            return "DROP", "reachable but no login form or login link — not a login page", None
        if st in (401, 403, 429):
            return "DROP", f"blocked to bots ({st}) and render failed — likely dead", None
        return "DROP", f"unreachable (status {st}, render failed)", None
    except R._RowTimeout:
        R._reset_renderer()
        return "REVIEW", "render timed out — verify manually", None
    except Exception as e:  # noqa: BLE001
        return "REVIEW", f"render error ({type(e).__name__})", None
    finally:
        signal.alarm(0)


def load_rows():
    if RAW.exists():
        return json.loads(RAW.read_text())["rows"]
    cfg = load_config(); sc = SheetsClient.from_config(cfg); sc.sheet_id = SHEET
    rows = sc._get_values(f"'{SRC_TAB}'", "A1:F20000")
    RAW.write_text(json.dumps({"rows": rows}))
    return rows


def aggregate(rows):
    agg = {}
    for r in rows[1:]:
        _id, oid, url, first, last, cnt = (list(r) + [""] * 6)[:6]
        try:
            c = int(str(cnt).strip() or 0)
        except ValueError:
            c = 0
        k = _norm(url)
        if not k:
            continue
        e = agg.setdefault(k, {"url": (url or "").strip(), "rows": 0, "reports": 0,
                               "orgs": set(), "first": first or "", "last": last or ""})
        e["rows"] += 1
        e["reports"] += c
        e["orgs"].add(str(oid).strip())
        if first and (not e["first"] or first < e["first"]):
            e["first"] = first
        if last and last > e["last"]:
            e["last"] = last
    return agg


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", default="all", choices=["a", "b", "c", "all", "merge"])
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--limit", type=int, default=0, help="cap URLs per stage (debug)")
    ap.add_argument("--shard", type=int, default=0, help="stage C: this shard's index")
    ap.add_argument("--of", type=int, default=1, help="stage C: total shards")
    ap.add_argument("--report-remaining", action="store_true")
    args = ap.parse_args()

    # Stage C is serial per browser, so it runs sharded across processes: each
    # shard renders every --of'th ambiguous URL into its own state file, and
    # --stage merge folds them back into the main verdicts file.
    shard_path = ROOT / f"reported_urls_verdicts_shard{args.shard}.json"

    rows = load_rows()
    agg = aggregate(rows)
    state = json.loads(STATE.read_text()) if STATE.exists() else {}

    if args.report_remaining:
        todo = sum(1 for k in agg if state.get(k, {}).get("verdict") in (None, "AMBIGUOUS"))
        print(todo)
        return

    print(f"rows={len(rows)-1} unique={len(agg)} already-classified={len(state)}", flush=True)

    def save():
        STATE.write_text(json.dumps(state))

    if args.stage in ("a", "all"):
        n = 0
        for k in agg:
            if k in state:
                continue
            v, why = stage_a(k)
            if v:
                state[k] = {"verdict": v, "reason": why, "stage": "A"}
                n += 1
        save()
        print(f"stage A: {n} decided by URL-only rules", flush=True)

    if args.stage in ("b", "all"):
        todo = [k for k in agg if k not in state]
        if args.limit:
            todo = todo[: args.limit]
        print(f"stage B: {len(todo)} URLs over HTTP ({args.workers} workers)", flush=True)
        done = 0
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(stage_b, k): k for k in todo}
            for f in as_completed(futs):
                k = futs[f]
                try:
                    v, why, final, st = f.result()
                except Exception as e:  # noqa: BLE001
                    v, why, final, st = "AMBIGUOUS", f"fetch error ({type(e).__name__})", k, 0
                state[k] = {"verdict": v, "reason": why, "stage": "B", "status": st,
                            "final": final if final != k else ""}
                done += 1
                if done % 200 == 0:
                    save(); print(f"  ...{done}/{len(todo)}", flush=True)
        save()
        c = collections.Counter(v["verdict"] for v in state.values())
        print(f"stage B done: {dict(c)}", flush=True)

    if args.stage == "merge":
        n = 0
        for p in sorted(ROOT.glob("reported_urls_verdicts_shard*.json")):
            for k, v in json.loads(p.read_text()).items():
                state[k] = v
                n += 1
        save()
        print(f"merged {n} shard verdicts", flush=True)

    if args.stage in ("c", "all"):
        signal.signal(signal.SIGALRM, R._on_alarm)
        mine = json.loads(shard_path.read_text()) if shard_path.exists() else {}
        todo = [k for k, v in state.items() if v.get("verdict") == "AMBIGUOUS"]
        # Deterministic slice so shards never collide; already-rendered URLs in
        # this shard's own file are skipped, making each shard resumable.
        todo = [k for i, k in enumerate(todo) if i % args.of == args.shard and k not in mine]
        if args.limit:
            todo = todo[: args.limit]
        print(f"stage C shard {args.shard}/{args.of}: {len(todo)} URLs via JS render", flush=True)
        # If the headless browser dies, JSRenderer can spin below the interpreter
        # where SIGALRM never lands, wedging the process. Two defences: (1) mark
        # each URL attempted BEFORE rendering and checkpoint immediately, so a
        # killed process never retries the URL that hung it (it stays REVIEW and
        # the restart makes progress); (2) recycle the browser periodically.
        RECYCLE = int(os.getenv("RENDER_RECYCLE", "25"))
        for i, k in enumerate(todo, 1):
            st = state[k].get("status") or 0
            mine[k] = {**state[k], "verdict": "REVIEW", "stage": "C",
                       "reason": "render did not complete (browser hung) — verify manually"}
            shard_path.write_text(json.dumps(mine))
            v, why, ep = stage_c(k, st)
            mine[k] = {**state[k], "verdict": v, "reason": why, "stage": "C"}
            if ep:
                mine[k]["resolved"] = ep
            shard_path.write_text(json.dumps(mine))
            if i % RECYCLE == 0:
                R._reset_renderer()
                print(f"  ...{i}/{len(todo)} (browser recycled)", flush=True)
        shard_path.write_text(json.dumps(mine))
        print(f"shard {args.shard} done ({len(mine)} verdicts)", flush=True)
        return

    c = collections.Counter(v["verdict"] for v in state.values())
    print(f"FINAL {dict(c)}", flush=True)


if __name__ == "__main__":
    main()

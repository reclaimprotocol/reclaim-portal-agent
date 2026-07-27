#!/usr/bin/env python3
"""JS-render validation for the review-junk portal URLs (the affordance/dead
bucket from _audit_portals). Renders each URL in a headless browser and looks
for a real login affordance in the SETTLED DOM:

  KEEP  if rendered DOM has a password field, a real form (>=2 inputs), a
        login link/button (login/sign-in/entrar/acessar/iniciar sesión/acceso),
        or a login-named final URL that reached an input.
  NO_LOGIN   rendered fine but nothing login-shaped (candidate junk)
  UNREACHABLE render failed / timed out even in a browser

Only NO_LOGIN + UNREACHABLE remain deletion candidates; KEEP are rescued real
portals (SPA logins that a static fetch couldn't see). Resumable via a cache.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import re
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts"))
try:
    from dotenv import load_dotenv; load_dotenv(ROOT / ".env")
except Exception:  # noqa: BLE001
    pass

import _bootstrap  # noqa: F401,E402
from agent.stages.js_renderer import JSRenderer, _has_password_input  # noqa: E402

AUDIT = ROOT / "portal_audit_verdicts.json"
CACHE = ROOT / "jsvalidate_verdicts.json"

_LOGIN_TXT = re.compile(
    r"(log[\s\-]?in|sign[\s\-]?in|log[\s\-]?on|entrar|acess(ar|e|o)|"
    r"ingresar|inicia(r)?\s+sesi[oó]n|acceso|área do aluno|portal do aluno|"
    r"student\s+login|mi\s+cuenta|my\s+account|iniciar\s+sesi)", re.I)
_LOGIN_URL = re.compile(r"login|signin|sign-in|logon|/sso|/cas|oauth|saml|adfs|/auth", re.I)

_tls = threading.local()


def _renderer() -> JSRenderer:
    r = getattr(_tls, "r", None)
    if r is None:
        r = _tls.r = JSRenderer(timeout_seconds=25)
    return r


def _load(p: Path) -> dict:
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:  # noqa: BLE001
            return {}
    return {}


def _review_urls() -> list[str]:
    audit = _load(AUDIT)
    return [u for u, v in audit.items()
            if v.get("verdict") == "JUNK"
            and ("affordance" in v.get("reason", "") or "dead" in v.get("reason", ""))]


def _validate(url: str) -> dict:
    res = _renderer().render(url)
    if not res.ok:
        return {"verdict": "UNREACHABLE", "reason": res.error[:60]}
    html = (res.html or "")
    low = html.lower()
    forms = low.count("<form")
    inputs = low.count("<input")
    has_pwd = _has_password_input(html)
    login_txt = bool(_LOGIN_TXT.search(html))
    login_url = bool(_LOGIN_URL.search(res.final_url or url))
    if has_pwd:
        return {"verdict": "KEEP", "reason": "password field (rendered)"}
    if forms >= 1 and inputs >= 2:
        return {"verdict": "KEEP", "reason": "login form (rendered)"}
    if login_txt:
        return {"verdict": "KEEP", "reason": "login link/button (rendered)"}
    if login_url and inputs >= 1:
        return {"verdict": "KEEP", "reason": "login-named URL + input"}
    return {"verdict": "NO_LOGIN", "reason": "no login element after render"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report-remaining", action="store_true")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    urls = _review_urls()
    cache = _load(CACHE)
    todo = [u for u in urls if u not in cache]
    if args.report_remaining:
        print(len(todo)); return

    print(f"js-validate: {len(urls)} review URLs | cached {len(cache)} | to render {len(todo)}", flush=True)
    lock = threading.Lock(); done = 0
    with cf.ThreadPoolExecutor(max_workers=args.workers) as exe:
        futs = {exe.submit(_validate, u): u for u in todo}
        for fut in cf.as_completed(futs):
            u = futs[fut]
            try:
                cache[u] = fut.result()
            except Exception as e:  # noqa: BLE001
                cache[u] = {"verdict": "UNREACHABLE", "reason": f"err:{type(e).__name__}"}
            with lock:
                done += 1
                if done % 20 == 0:
                    CACHE.write_text(json.dumps(cache))
                    print(f"  {done}/{len(todo)} rendered", flush=True)
    CACHE.write_text(json.dumps(cache))
    from collections import Counter
    c = Counter(v["verdict"] for v in cache.values())
    print(f"DONE: {dict(c)}", flush=True)


if __name__ == "__main__":
    main()

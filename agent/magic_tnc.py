"""Magic T&C — find the governing Terms & Conditions / Privacy policy for a
student-login portal, the same rules-free way Magic finds the portal itself.

Reuses the classic waterfall LEVEL LADDER for priority (stop at first hit):

    exact         — a T&C link on the portal page itself
    parent_url    — walk up the portal's URL path
    parent_domain — the portal's registrable-root homepage (same institution)
    vendor        — the portal's root when it's a THIRD-PARTY host (Moodle,
                    a SIS vendor, …) → the vendor T&C governing the service
    uni_home      — the university's own homepage (linked or not)
    search        — LLM/web search for "<university> terms of use / privacy"

…but instead of India-tuned anchor rules, it (a) harvests candidate links by
MULTILINGUAL url/text shape, and (b) asks the LLM judge "is this really a
terms/privacy document?" — so it works globally. All heavy primitives (LLM
call, fetch, cache, registrable-root) are reused from `agent.magic`.

`find_tnc()` takes an optional `cache` dict so every portal of the same
university/vendor reuses the uni/vendor-level T&C (found once, applied to all).
"""
from __future__ import annotations

import json
import logging
import re
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

from agent import magic as M

logger = logging.getLogger("agent.magic.tnc")

# T&C by URL shape — universal + multilingual (pt/es/fr/de/it + generic).
_TNC_URL_RE = re.compile(
    r"term|/tos(?:[/?.]|$)|termos|condic|condi[cç][oõ]es|t[eé]rmin|"
    r"privac|privaci|datenschutz|/legal|/agb|/cgu|policy|policies|gdpr|lgpd|"
    r"aviso.?de.?privac|politica.?de.?privac|pol[ií]tica", re.I)

# T&C by anchor text — multilingual.
_TNC_TEXT_HINTS = (
    "terms", "terms of use", "terms of service", "terms & conditions",
    "conditions", "privacy", "privacy policy", "legal", "cookie",
    "termos de uso", "termos", "condições de uso", "política de privacidade",
    "términos", "términos y condiciones", "aviso de privacidad", "política de privacidad",
    "condiciones", "mentions légales", "confidentialité", "cgu",
    "nutzungsbedingungen", "datenschutz", "privacy e cookie", "termini",
    "이용약관", "개인정보", "利用規約", "プライバシー", "隐私", "条款",
)

_CONF = float(M._env("MAGIC_TNC_THRESHOLD", "GLOBAL_TNC_THRESHOLD", default="0.6"))
_MAX_CANDS = int(M._env("MAGIC_TNC_MAX_CANDIDATES", default="8"))


def _get(url: str):
    try:
        return requests.get(url, headers={"User-Agent": M.USER_AGENT},
                            timeout=M.HTTP_TIMEOUT, verify=False, allow_redirects=True)
    except Exception:  # noqa: BLE001
        return None


def _harvest_tnc_links(page_url: str) -> list[tuple[str, str]]:
    """Return (url, anchor_text) links on `page_url` that look like a T&C by URL
    shape or (multilingual) anchor text. Footer/legal links live here."""
    r = _get(page_url)
    if r is None or r.status_code >= 400 or not r.text:
        return []
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    try:
        soup = BeautifulSoup(r.text, "html.parser")
    except Exception:  # noqa: BLE001
        return []
    for a in soup.find_all("a", href=True):
        href = urljoin(r.url or page_url, a["href"])
        if not href.lower().startswith("http"):
            continue
        key = href.split("#")[0].rstrip("/")
        if key in seen:
            continue
        text = (a.get_text() or "").strip().lower()
        if _TNC_URL_RE.search(href.lower()) or any(h in text for h in _TNC_TEXT_HINTS):
            seen.add(key)
            out.append((href, text[:80]))
    return out


def _page_signals(url: str) -> dict:
    r = _get(url)
    if r is None:
        return {"url": url, "status": 0, "title": "", "text": "", "error": "fetch"}
    title, text = "", ""
    try:
        soup = BeautifulSoup(r.text or "", "html.parser")
        if soup.title and soup.title.string:
            title = soup.title.string.strip()[:160]
        for s in soup(["script", "style", "noscript"]):
            s.extract()
        text = re.sub(r"\s+", " ", soup.get_text(" ")).strip()[:800]
    except Exception:  # noqa: BLE001
        text = re.sub(r"<[^>]+>", " ", r.text or "")[:800]
    return {"url": r.url, "status": r.status_code, "title": title, "text": text}


def _judge_tnc(uni_name: str, cands: list[dict]) -> list[dict]:
    """Ask the LLM which candidates are genuine terms/privacy documents."""
    items = [{"i": i, "url": c["url"], "title": c["title"],
              "text": c["text"], "status": c["status"]} for i, c in enumerate(cands)]
    prompt = (
        f"For the university \"{uni_name}\", decide for EACH fetched page whether "
        f"it is a genuine legal TERMS or PRIVACY document — Terms of Use / Terms "
        f"of Service / Terms & Conditions, Privacy Policy, Cookie Policy, or the "
        f"local-language equivalent (Termos de Uso, Política de Privacidade, "
        f"Términos y Condiciones, Aviso de Privacidad, CGU, Nutzungsbedingungen, "
        f"이용약관, 利用規約, …). It must be the actual policy text/landing, not a "
        f"generic homepage, news, login, or a dead page (status>=400).\n"
        f"Items:\n{json.dumps(items, ensure_ascii=False)}\n\n"
        f"Return ONLY a JSON array, one object per item, same order:\n"
        f'[{{"i":0,"is_tnc":true,"type":"Terms|Privacy|Cookie|Legal|Other",'
        f'"confidence":0.0-1.0}}]'
    )
    verdicts = M._extract_json(M._chat(prompt, model=M.MAGIC_MODEL))
    by_i = {}
    if isinstance(verdicts, list):
        by_i = {v.get("i"): v for v in verdicts if isinstance(v, dict)}
    out = []
    for i, c in enumerate(cands):
        v = by_i.get(i, {})
        out.append({**c, "is_tnc": bool(v.get("is_tnc")),
                    "type": str(v.get("type", "") or ""),
                    "confidence": float(v.get("confidence", 0) or 0)})
    return out


_TYPE_ORDER = {"terms": 0, "privacy": 1, "cookie": 2, "legal": 3}


def _pick_all(uni_name: str, links: list[tuple[str, str]]) -> list[dict]:
    """Fetch + judge candidate links; return ALL genuine T&C docs (a page often
    has BOTH a Terms and a Privacy doc — capture both), Terms-first, deduped."""
    if not links:
        return []
    cands = [_page_signals(u) for (u, _t) in links[:_MAX_CANDS]]
    judged = _judge_tnc(uni_name, [c for c in cands if c.get("status") and c["status"] < 400])
    # Keep only core governing docs (Terms / Privacy / Cookie); drop generic
    # "Legal"/"Other" (e.g. an anti-harassment policy PDF) so the output is the
    # clean Terms + Privacy pair the sheet expects.
    _ALLOWED = {"terms", "privacy", "cookie"}
    valid = [c for c in judged if c["is_tnc"] and c["confidence"] >= _CONF
             and (c.get("type") or "").lower() in _ALLOWED]
    valid.sort(key=lambda c: _TYPE_ORDER.get((c.get("type") or "").lower(), 4))
    seen, out = set(), []
    for c in valid:
        k = M._clean_url(c["url"]).split("#")[0].rstrip("/")
        if k in seen:
            continue
        seen.add(k)
        out.append({"url": M._clean_url(c["url"]), "type": c.get("type", "")})
    return out[:3]


def _parent_paths(url: str) -> list[str]:
    p = urlsplit(url)
    segs = [s for s in p.path.split("/") if s]
    out = []
    for k in range(len(segs) - 1, -1, -1):
        out.append(f"{p.scheme}://{p.netloc}/" + "/".join(segs[:k]) + ("/" if k else ""))
    seen, uniq = set(), []
    for u in out:
        if u not in seen:
            seen.add(u); uniq.append(u)
    return uniq[:3]


def find_tnc(portal_url: str, uni_domain: str, uni_name: str,
             country: str = "", cache: dict | None = None) -> dict:
    """Find the governing T&C for `portal_url`. Returns
    {"tnc_level", "tncs":[{url,type}...], "tnc_url", "tnc_type"} where `tncs`
    holds ALL docs at the winning level (typically Terms + Privacy). `tnc_url`
    is the primary (Terms preferred). {"tnc_level":"N/A", "tncs":[]} if none.

    `cache` (per-run dict) memoises uni/vendor-level results so every portal of
    the same institution reuses the T&C found once."""
    cache = cache if cache is not None else {}
    phost = M._norm_host(portal_url)
    proot = M._registrable_root(phost) or phost
    uroot = M._registrable_root(M._norm_host("http://" + uni_domain)) or uni_domain
    is_vendor = proot != uroot

    def _result(level, items):
        return {"tnc_level": level if items else "N/A", "tncs": items,
                "tnc_url": items[0]["url"] if items else "",
                "tnc_type": items[0]["type"] if items else ""}

    # Level 1-2: on/around the portal page itself (per-portal, not cached).
    for level, page in [("exact", portal_url)] + [("parent_url", p) for p in _parent_paths(portal_url)]:
        items = _pick_all(uni_name, _harvest_tnc_links(page))
        if items:
            return _result(level, items)

    # Level 3: the portal's OWN registrable-root homepage — "parent_domain" when
    # that's the university's domain, or "vendor" for a third-party host
    # (samarth.edu.in, a SIS vendor, …). Cached by the PORTAL root (proot), NOT
    # the uni root — so a uni-domain portal returning N/A can never poison a
    # sibling vendor portal (IGNOU: gradecard.ignou.ac.in N/A must not block
    # ignou.samarth.edu.in from checking samarth.edu.in).
    rk = f"tnc-root:{proot}"
    if rk not in cache:
        cache[rk] = _pick_all(uni_name, _harvest_tnc_links(f"https://{proot}/"))
    if cache[rk]:
        return _result("vendor" if is_vendor else "parent_domain", cache[rk])

    # Level 4-5: the university's own homepage + search — cached by the UNI root.
    ck = f"tnc-uni:{uroot}"
    if ck in cache:
        c = cache[ck]
        return _result(c["level"], c["items"]) if c else _result("N/A", [])
    unidom = M._norm_host("http://" + uni_domain)
    ladder = [("uni_home", f"https://{uroot}/")] if uroot != proot else []
    if unidom and unidom not in (proot, uroot):
        ladder.append(("uni_home", f"https://{unidom}/"))
    for level, page in ladder:
        items = _pick_all(uni_name, _harvest_tnc_links(page))
        if items:
            cache[ck] = {"level": level, "items": items}
            return _result(level, items)

    # search — ask the model for the university's T&C + Privacy URLs.
    urls = M._extract_json(M._chat(
        f"Give the official Terms of Use AND Privacy Policy URLs for the "
        f"university \"{uni_name}\" (domain {uni_domain}). Return ONLY a JSON "
        f"array of URLs.", model=M.MAGIC_MODEL))
    if isinstance(urls, list):
        cands = [(u, "") for u in urls if isinstance(u, str) and u.startswith("http")]
        items = _pick_all(uni_name, cands)
        if items:
            cache[ck] = {"level": "search", "items": items}
            return _result("search", items)

    cache[ck] = None
    return _result("N/A", [])

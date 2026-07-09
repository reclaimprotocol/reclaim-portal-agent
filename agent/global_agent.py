"""Global, rules-free student-portal discovery — an LLM-judge architecture.

This module is DELIBERATELY INDEPENDENT of `agent/stages/discovery*.py` (the
~8,900-line rule pipeline with samarth / state-platform / shortname / geography
heuristics tuned for India). It does not import or depend on any of that
portal-recognition logic. It works for ANY university in ANY country by:

  1. HARVEST — gather candidate URLs from generic, country-agnostic routes:
       * what an LLM already knows (Gemini),
       * real web-search results (multilingual query variants),
       * the university's OWN site (homepage links + sitemap),
       * common portal subdomains that actually resolve (DNS).
  2. FETCH — pull each candidate and extract language-agnostic signals:
       final URL, redirect chain, HTTP status, <title>, meta, forms,
       password fields, a snippet of visible text, and platform fingerprints.
  3. JUDGE — an LLM (gemini-2.5-flash by default) decides, per candidate:
       is this a STUDENT LOGIN portal for THIS university? category?
       confidence? No hardcoded rules — the model reads the page like a human.
  4. CONSOLIDATE — keep confident, on-institution portals; collapse redirect
       chains / duplicates.

Only pure I/O primitives are reused from the old module (the OpenRouter HTTP
call shape and the DuckDuckGo fetcher) — never any recognition rule.
"""
from __future__ import annotations

import concurrent.futures as _cf
import hashlib
import json
import logging
import os
import random
import re
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests
import urllib3
from bs4 import BeautifulSoup

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
logger = logging.getLogger("agent.global")

# --- config (read directly; no rule-module dependency) ---------------------
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
# The judge is a cheap, fast, strongly-multilingual model. Swap without code
# changes via JUDGE_MODEL. Default to Gemini 2.5 Flash.
JUDGE_MODEL = os.getenv("JUDGE_MODEL", "google/gemini-2.5-flash")
# The URL-suggestion pass can use the same or a different model.
SUGGEST_MODEL = os.getenv("GLOBAL_SUGGEST_MODEL", JUDGE_MODEL)

USER_AGENT = os.getenv(
    "GENIE_UA",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
)
HTTP_TIMEOUT = float(os.getenv("GLOBAL_HTTP_TIMEOUT", "12"))
CONFIDENCE_THRESHOLD = float(os.getenv("GLOBAL_JUDGE_THRESHOLD", "0.6"))
MAX_CANDIDATES = int(os.getenv("GLOBAL_MAX_CANDIDATES", "45"))
FETCH_WORKERS = int(os.getenv("GLOBAL_FETCH_WORKERS", "12"))
# Link-follow bounds — the dominant latency source on link-heavy homepages.
FOLLOW_MAX_PAGES = int(os.getenv("GLOBAL_FOLLOW_MAX_PAGES", "12"))
FOLLOW_MAX_LINKS = int(os.getenv("GLOBAL_FOLLOW_MAX_LINKS", "20"))

# Common portal subdomain labels — a country-AGNOSTIC recall aid (not a
# recognition rule; every candidate still faces the judge). Covers SIS/ERP,
# LMS, SSO, and self-service across vendors and languages.
_SUBDOMAIN_WORDLIST = (
    "portal", "my", "myportal", "student", "students", "estudiante", "aluno",
    "alumno", "alumnos", "login", "signin", "sso", "auth", "cas", "idp",
    "shibboleth", "adfs", "account", "accounts", "webmail", "mail",
    "lms", "moodle", "canvas", "elearning", "learn", "learning", "classroom",
    "erp", "sis", "sims", "academic", "academics", "acad", "campus",
    "campusvirtual", "aulavirtual", "virtual", "online", "eduweb", "edusys",
    "exam", "exams", "results", "result", "fees", "fee", "library", "lib",
    "hostel", "selfservice", "self-service", "vle", "connect", "one", "app",
    "apps", "gateway", "id", "identity", "autogestion", "guarani", "siu",
)

# Multilingual anchor/URL hints used ONLY to prioritise which of a homepage's
# links are worth fetching (a recall filter, not a verdict). If a link screams
# "login/portal" in any of these tongues, fetch it early.
_LINK_HINTS = (
    "login", "log in", "log-in", "signin", "sign in", "sign-in", "portal",
    "student", "webmail", "e-learning", "elearning", "lms", "moodle", "canvas",
    "sso", "my account", "myaccount", "self service", "selfservice", "erp",
    "acceso", "ingresar", "iniciar sesion", "alumno", "autogestion",
    "portal do aluno", "aluno", "connexion", "se connecter", "anmelden",
    "einloggen", "accedi", "entrar", "masuk", "登录", "登入", "ログイン",
    "로그인", "登錄", "تسجيل الدخول", "вход", "войти",
)


# --------------------------------------------------------------------------- #
#  Disk cache — per-domain harvest + per-URL judge verdicts.                   #
#  Fixes three things at once: latency (repeat runs skip the network/LLM),     #
#  run-to-run variance (same cached candidate set + verdicts each time), and   #
#  rate-limit pressure (fewer OpenRouter calls). TTL-bounded.                  #
# --------------------------------------------------------------------------- #
_CACHE_ENABLED = os.getenv("GLOBAL_CACHE", "1").strip().lower() in ("1", "true", "yes", "on")
_CACHE_DIR = Path(os.getenv("GLOBAL_CACHE_DIR", "")
                  or (Path(__file__).resolve().parents[1] / ".cache" / "global_agent"))
_CACHE_TTL = float(os.getenv("GLOBAL_CACHE_TTL", str(7 * 86400)))  # 7 days


def _cache_get(key: str):
    if not _CACHE_ENABLED:
        return None
    p = _CACHE_DIR / (hashlib.sha1(key.encode("utf-8")).hexdigest() + ".json")
    try:
        if p.exists() and (time.time() - p.stat().st_mtime) < _CACHE_TTL:
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    return None


def _cache_put(key: str, value) -> None:
    if not _CACHE_ENABLED:
        return
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        p = _CACHE_DIR / (hashlib.sha1(key.encode("utf-8")).hexdigest() + ".json")
        p.write_text(json.dumps(value), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def _cached(key: str, produce):
    """Return cached value for key, else call produce(), cache, and return it.
    NEVER caches an empty/falsy result — a transient throttle/timeout that
    returns [] must not poison the cache for 7 days (that silently drops real
    portals like cmsys/edisciplinas on every later run). Empty → retry next run.
    """
    hit = _cache_get(key)
    if hit:  # truthy only — empty list/None is treated as a miss
        return hit
    val = produce()
    if val:
        _cache_put(key, val)
    return val


@dataclass
class Candidate:
    url: str
    provenance: str                 # where it came from
    anchor_text: str = ""
    # filled by fetch:
    final_url: str = ""
    status: int = 0
    title: str = ""
    meta: str = ""
    has_password: bool = False
    form_count: int = 0
    redirect_chain: list[str] = field(default_factory=list)
    text_snippet: str = ""
    fingerprints: list[str] = field(default_factory=list)
    error: str = ""
    # filled by judge:
    is_portal: bool = False
    central: bool = False
    category: str = ""
    belongs: bool = False
    confidence: float = 0.0
    reason: str = ""
    judged: bool = False   # True only if the LLM actually returned a verdict


# --------------------------------------------------------------------------- #
#  OpenRouter (generic chat call — the only thing borrowed in spirit)          #
# --------------------------------------------------------------------------- #
def _chat(prompt: str, *, model: str, timeout: float = 60.0, retries: int = 4) -> str:
    """OpenRouter chat call with retry+backoff on rate limits / transient
    errors. CRITICAL: without this, a throttled judge call returns '' and the
    candidate is silently dropped — under batch load whole universities came
    back with 0 portals purely from 429s, not from bad judging."""
    if not OPENROUTER_API_KEY:
        return ""
    for attempt in range(retries):
        try:
            r = requests.post(
                OPENROUTER_URL,
                headers={
                    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://github.com/reclaimprotocol",
                },
                json={"model": model,
                      "messages": [{"role": "user", "content": prompt}]},
                timeout=timeout,
            )
            status = r.status_code
            data = r.json()
        except Exception as err:  # noqa: BLE001 — network/timeout: retry
            status, data = 0, None
            if attempt == retries - 1:
                logger.warning("openrouter call failed after %d tries: %s",
                               retries, err)
        else:
            err_obj = data.get("error") if isinstance(data, dict) else None
            err_code = (err_obj or {}).get("code") if isinstance(err_obj, dict) else None
            rate_limited = status == 429 or err_code == 429 or (
                status in (500, 502, 503, 529))
            if not err_obj and not rate_limited:
                try:
                    return data["choices"][0]["message"]["content"].strip()
                except (KeyError, IndexError, TypeError, AttributeError):
                    pass  # malformed — retry
            if err_obj and not rate_limited:
                logger.warning("openrouter error (no retry): %s", err_obj)
                return ""
        # backoff before the next attempt (exponential + jitter)
        if attempt < retries - 1:
            time.sleep(min(20.0, 2.0 * (2 ** attempt)) + random.uniform(0, 1.0))
    logger.warning("openrouter: giving up after %d attempts (model=%s)", retries, model)
    return ""


def _extract_json(text: str):
    """Pull the first JSON array/object out of an LLM reply (tolerant of code
    fences / stray prose)."""
    if not text:
        return None
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?|```$", "", t, flags=re.MULTILINE).strip()
    for opener, closer in (("[", "]"), ("{", "}")):
        i, j = t.find(opener), t.rfind(closer)
        if i != -1 and j != -1 and j > i:
            try:
                return json.loads(t[i:j + 1])
            except json.JSONDecodeError:
                continue
    return None


def _norm_host(url: str) -> str:
    h = (urlsplit(url).netloc or "").lower().split(":")[0]
    return h[4:] if h.startswith("www.") else h


_SESSION_RE = re.compile(r";jsessionid=[^/?#]*", re.I)


def _clean_url(url: str) -> str:
    """Strip volatile session tokens (;jsessionid=…) so stored/deduped URLs are
    stable across runs (otherwise the same portal looks different each fetch)."""
    return _SESSION_RE.sub("", url or "")


# Path tokens that are generic login plumbing, not a distinct system name.
_GENERIC_PATH_SEGS = frozenset({
    "", "login", "signin", "sign-in", "logon", "auth", "sso", "cas", "account",
    "accounts", "user", "users", "oauth2", "oauth", "adfs", "saml2", "idp",
    "connect", "session", "index.php", "index.jsp", "index.html", "home",
    "home_login", "default", "portal", "main", "ls", "authorize", "identifier",
})
# Login SUB-pages that are never the portal entry point itself.
_SUBPAGE_RE = re.compile(
    r"forgot|reset|recover|register|signup|sign-up|logout|change.?password|"
    r"first.?access|primeiro.?acesso|esqueci", re.I)


def _is_login_subpage(url: str) -> bool:
    return bool(_SUBPAGE_RE.search(urlsplit(url).path))


_RANK_PATH_HINTS = ("login", "signin", "sso", "portal", "account", "auth",
                    "moodle", "canvas", "cas", "oauth", "adfs", "saml")


def _candidate_rank(c: "Candidate") -> tuple:
    """Sort key: portal-ish candidates first (so the judge cap keeps the likely
    portals). 0 = promising, 1 = not."""
    u = (c.final_url or c.url).lower()
    host = urlsplit(u).netloc.split(":")[0]
    label = host.split(".", 1)[0] if "." in host else host
    hinted = (any(t in label for t in _SUBDOMAIN_WORDLIST)
              or any(h in u for h in _RANK_PATH_HINTS))
    return (0 if hinted else 1, len(u))


def _distinguishing_segment(url: str) -> str:
    """First path segment that names a distinct system (skips generic login
    plumbing). '' for a pure login/root URL — so login-path variants on one
    host collapse together, while /jupiterweb, /apolo stay distinct."""
    for seg in urlsplit(url).path.strip("/").split("/"):
        s = seg.lower()
        if s and s not in _GENERIC_PATH_SEGS:
            return s
    return ""


# --------------------------------------------------------------------------- #
#  HARVEST                                                                     #
# --------------------------------------------------------------------------- #
def _llm_suggest(name: str, domain: str, country: str) -> list[str]:
    """Ask the model for candidate portal URLs from its own knowledge. Pure
    recall — every URL still gets fetched + judged."""
    prompt = (
        f"List the STUDENT-facing login/portal URLs for the university "
        f"\"{name}\" (official website domain: {domain}"
        f"{', country: ' + country if country else ''}). "
        f"Include every kind a student would log into: student information "
        f"system / ERP / self-service, LMS (Moodle/Canvas/Blackboard/etc.), "
        f"single-sign-on (SSO/CAS/Shibboleth/ADFS), exam/results, fees, "
        f"library, webmail, and the local-language equivalents. "
        f"Prefer real login endpoints on the university's own domains. "
        f"Return ONLY a JSON array of URL strings, no prose, no markdown."
    )
    urls = _extract_json(_chat(prompt, model=SUGGEST_MODEL))
    out = []
    if isinstance(urls, list):
        for u in urls:
            if isinstance(u, str) and u.strip().lower().startswith("http"):
                out.append(u.strip())
    return out


def _web_search(name: str, domain: str, country: str) -> list[str]:
    """Multilingual web-search harvest via the DDG fetcher (pure I/O reuse)."""
    from agent.stages.discovery_rules import _ddg_html_search  # I/O primitive
    queries = [
        f"{name} student login portal",
        f"{name} student portal",
        f"{domain} login",
        f"site:{domain} login",
        f"{name} LMS moodle canvas login",
        f"{name} student information system login",
    ]
    urls: list[str] = []
    seen: set[str] = set()
    for q in queries:
        try:
            for u in _ddg_html_search(q, http_timeout=HTTP_TIMEOUT,
                                      user_agent=USER_AGENT, max_results=10):
                if u not in seen:
                    seen.add(u)
                    urls.append(u)
        except Exception as err:  # noqa: BLE001 — search is best-effort
            logger.debug("web search %r failed: %s", q, err)
    return urls


def _own_site_links(domain: str) -> list[tuple[str, str]]:
    """Fetch the homepage + sitemap and return (url, anchor_text) links that
    look portal-ish (multilingual hints) plus a sample of other on-site links.
    Recall filtering only — the judge decides."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    base = f"https://{domain}/"
    try:
        r = requests.get(base, headers={"User-Agent": USER_AGENT},
                         timeout=HTTP_TIMEOUT, verify=False, allow_redirects=True)
        soup = BeautifulSoup(r.text or "", "html.parser")
        for a in soup.find_all("a", href=True):
            href = urljoin(r.url or base, a["href"])
            if not href.lower().startswith("http"):
                continue
            text = (a.get_text() or "").strip().lower()
            blob = (href.lower() + " " + text)
            hinted = any(h in blob for h in _LINK_HINTS)
            if href in seen:
                continue
            # Keep hinted links from anywhere; keep a few same-registrable links.
            if hinted:
                seen.add(href)
                out.append((href, text[:80]))
    except Exception as err:  # noqa: BLE001
        logger.debug("homepage fetch failed for %s: %s", domain, err)

    # sitemap.xml — cheap extra recall.
    try:
        sm = requests.get(f"https://{domain}/sitemap.xml",
                          headers={"User-Agent": USER_AGENT},
                          timeout=HTTP_TIMEOUT, verify=False)
        if sm.status_code == 200 and "<" in sm.text:
            for loc in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", sm.text)[:2000]:
                if any(h in loc.lower() for h in _LINK_HINTS) and loc not in seen:
                    seen.add(loc)
                    out.append((loc, "sitemap"))
    except Exception as err:  # noqa: BLE001
        logger.debug("sitemap fetch failed for %s: %s", domain, err)
    return out


def _registrable_root(domain: str) -> str:
    """Crude registrable root: last 2 labels, or 3 for 2-level ccTLDs
    (ac.in / edu.br / …)."""
    labels = domain.split(".")
    if len(labels) >= 3 and labels[-2] in (
            "ac", "edu", "co", "com", "gov", "org", "net", "res"):
        return ".".join(labels[-3:])
    if len(labels) >= 2:
        return ".".join(labels[-2:])
    return domain


def _certspotter(domain: str) -> set[str]:
    """SSLMate Certspotter CT API — a second, more reliable CT source than
    crt.sh (free, no key). Returns subdomain hosts of `domain`."""
    hosts: set[str] = set()
    try:
        r = requests.get(
            "https://api.certspotter.com/v1/issuances",
            params={"domain": domain, "include_subdomains": "true",
                    "expand": "dns_names"},
            headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT,
        )
        if r.status_code == 200:
            for cert in r.json():
                for name in cert.get("dns_names", []):
                    n = name.lower().lstrip("*.")
                    if n.endswith("." + domain) and n != domain:
                        hosts.add(n)
    except Exception:  # noqa: BLE001
        pass
    return hosts


def _ct_candidates(domain: str) -> list[str]:
    """Multi-source certificate-transparency subdomain enumeration. Surfaces
    institution-specific hosts a wordlist can't guess (cmsys, idol, edurec, …).
    Unions crt.sh (via the old pipeline's harvester) + Certspotter — crt.sh
    alone is too flaky. Queries the host AND its registrable root."""
    root = _registrable_root(domain)
    hosts: set[str] = set()
    try:
        from agent.stages.discovery_rules import crt_sh_subdomains
    except Exception:  # noqa: BLE001
        crt_sh_subdomains = None
    targets = {domain, root}
    with _cf.ThreadPoolExecutor(max_workers=4) as exe:
        futs = []
        for tgt in targets:
            if crt_sh_subdomains is not None:
                futs.append(exe.submit(lambda t=tgt: set(crt_sh_subdomains(t))))
            futs.append(exe.submit(_certspotter, tgt))
        for f in futs:
            try:
                hosts |= (f.result() or set())
            except Exception:  # noqa: BLE001
                pass
    # Big domains yield 100+ subdomains — prioritise portal/login/LMS-ish hosts
    # (by leftmost label) and cap, so CT doesn't crowd out other sources or the
    # judge budget. Non-portal-ish hosts still get a smaller allowance.
    hint_tokens = set(_SUBDOMAIN_WORDLIST) | {
        "idol", "cmsys", "edurec", "yscec", "learn", "vle", "ecampus"}

    def _portalish(h: str) -> bool:
        lbl = h.split(".", 1)[0]
        return any(t in lbl for t in hint_tokens)

    ranked = sorted(hosts, key=lambda h: (0 if _portalish(h) else 1, len(h)))
    capped = ranked[:35]
    return [f"https://{h}/" for h in capped]


def _subdomain_candidates(domain: str) -> list[str]:
    """DNS-resolve common portal subdomains of the registrable domain; keep the
    ones that exist. Country-agnostic recall."""
    root = _registrable_root(domain)
    found: list[str] = []

    def _resolve(label: str) -> str | None:
        host = f"{label}.{root}"
        try:
            socket.gethostbyname(host)
            return host
        except Exception:  # noqa: BLE001
            return None

    with _cf.ThreadPoolExecutor(max_workers=FETCH_WORKERS) as exe:
        for host in exe.map(_resolve, _SUBDOMAIN_WORDLIST):
            if host:
                found.append(f"https://{host}/")
    return found


def harvest(name: str, domain: str, country: str) -> list[Candidate]:
    """Run all harvest routes concurrently and merge into a deduped candidate
    list (capped)."""
    cands: dict[str, Candidate] = {}

    def add(url: str, prov: str, anchor: str = "") -> None:
        u = url.strip()
        if not u.lower().startswith("http"):
            return
        key = u.rstrip("/")
        if key not in cands:
            cands[key] = Candidate(url=u, provenance=prov, anchor_text=anchor)

    with _cf.ThreadPoolExecutor(max_workers=5) as exe:
        f_llm = exe.submit(_cached, f"llm:{name}|{domain}",
                           lambda: _llm_suggest(name, domain, country))
        f_web = exe.submit(_cached, f"web:{name}|{domain}",
                           lambda: _web_search(name, domain, country))
        f_site = exe.submit(_cached, f"site:{domain}", lambda: _own_site_links(domain))
        f_sub = exe.submit(_cached, f"sub:{domain}", lambda: _subdomain_candidates(domain))
        f_ct = exe.submit(_cached, f"ct:{domain}", lambda: _ct_candidates(domain))
        for u in _safe(f_llm):
            add(u, "llm-suggest")
        for u in _safe(f_web):
            add(u, "web-search")
        for (u, t) in _safe(f_site):
            add(u, "own-site", t)
        for u in _safe(f_sub):
            add(u, "subdomain-dns")
        for u in _safe(f_ct):
            add(u, "ct-log")

    out = list(cands.values())
    logger.info("harvest: %d unique candidates (llm/web/site/subdomain/ct)", len(out))
    return out[:MAX_CANDIDATES]


def _followup_links(fetched: list[Candidate], domain: str,
                    already: set[str]) -> list[Candidate]:
    """One-hop link-follow: from pages that responded, harvest outbound
    login/portal-hinted links (the myaces -> ADFS SSO / portal -> LMS case).
    Country-agnostic; the judge still decides. Only follows links whose text or
    URL carries a login hint, capped."""
    new: dict[str, Candidate] = {}
    # Follow only the most promising pages, and cap TOTAL new links (not
    # per-page) so a link-heavy homepage can't explode the fetch/judge budget.
    for c in fetched[:FOLLOW_MAX_PAGES]:
        if len(new) >= FOLLOW_MAX_LINKS:
            break
        if c.error or (not c.text_snippet and not c.title):
            continue
        try:
            r = requests.get(c.final_url or c.url, headers={"User-Agent": USER_AGENT},
                             timeout=HTTP_TIMEOUT, verify=False, allow_redirects=True)
            soup = BeautifulSoup(r.text or "", "html.parser")
        except Exception:  # noqa: BLE001
            continue
        for a in soup.find_all("a", href=True):
            if len(new) >= FOLLOW_MAX_LINKS:
                break
            href = urljoin(r.url or c.url, a["href"])
            if not href.lower().startswith("http"):
                continue
            key = href.rstrip("/")
            if key in already or key in new:
                continue
            text = (a.get_text() or "").strip().lower()
            blob = href.lower() + " " + text
            if not any(h in blob for h in _LINK_HINTS):
                continue
            new[key] = Candidate(url=href, provenance=f"link-follow<-{_norm_host(c.url)}",
                                 anchor_text=text[:80])
    return list(new.values())


def _safe(fut):
    try:
        return fut.result() or []
    except Exception as err:  # noqa: BLE001
        logger.debug("harvest route failed: %s", err)
        return []


# --------------------------------------------------------------------------- #
#  FETCH signals                                                               #
# --------------------------------------------------------------------------- #
_FP_PATTERNS = (
    ("Moodle", re.compile(r"moodle|/login/index\.php", re.I)),
    ("Canvas", re.compile(r"instructure|canvas", re.I)),
    ("Blackboard", re.compile(r"blackboard|/webapps/", re.I)),
    ("Shibboleth", re.compile(r"shibboleth|/idp/|simplesaml", re.I)),
    ("CAS", re.compile(r"/cas/login|jasig|apereo", re.I)),
    ("ADFS", re.compile(r"/adfs/|wa=wsignin", re.I)),
    ("OAuth/OIDC", re.compile(r"/oauth2?/authorize|/connect/authorize|response_type=", re.I)),
    ("SIU-Guarani", re.compile(r"siu|guaran|autogesti", re.I)),
)


def fetch_signals(c: Candidate) -> Candidate:
    try:
        r = requests.get(c.url, headers={"User-Agent": USER_AGENT},
                         timeout=HTTP_TIMEOUT, verify=False, allow_redirects=True)
        c.status = r.status_code
        c.final_url = r.url
        c.redirect_chain = [h.headers.get("location", h.url) for h in r.history][:6]
        html = r.text or ""
    except Exception as err:  # noqa: BLE001
        c.error = f"{type(err).__name__}: {err}"
        return c
    low = html.lower()
    c.has_password = 'type="password"' in low or "type='password'" in low
    c.form_count = low.count("<form")
    try:
        soup = BeautifulSoup(html, "html.parser")
        if soup.title and soup.title.string:
            c.title = soup.title.string.strip()[:200]
        md = soup.find("meta", attrs={"name": "description"})
        if md and md.get("content"):
            c.meta = md["content"].strip()[:300]
        for s in soup(["script", "style", "noscript"]):
            s.extract()
        c.text_snippet = re.sub(r"\s+", " ", soup.get_text(" ")).strip()[:700]
    except Exception:  # noqa: BLE001
        c.text_snippet = re.sub(r"<[^>]+>", " ", html)[:700]
    blob = c.final_url + " " + low
    c.fingerprints = [name for name, rx in _FP_PATTERNS if rx.search(blob)]
    return c


# --------------------------------------------------------------------------- #
#  JUDGE (the LLM is the arbiter — no rules)                                   #
# --------------------------------------------------------------------------- #
def _judge_batch(name: str, domain: str, batch: list[Candidate]) -> None:
    items = []
    for i, c in enumerate(batch):
        items.append({
            "i": i, "url": c.url, "final_url": c.final_url, "status": c.status,
            "redirected": c.redirect_chain, "title": c.title, "meta": c.meta,
            "has_password_field": c.has_password, "forms": c.form_count,
            "platform_fingerprints": c.fingerprints,
            "provenance": c.provenance, "anchor_text": c.anchor_text,
            "text": c.text_snippet, "fetch_error": c.error,
        })
    prompt = (
        f"You are verifying CENTRAL student LOGIN portals for the university "
        f"\"{name}\" (official domain: {domain}). You will receive fetched web "
        f"pages. For EACH item decide, like a human who reads the page in any "
        f"language, whether it is a login used by the GENERAL STUDENT BODY of "
        f"the whole university — i.e. a student information system / ERP / "
        f"self-service, an LMS (Moodle/Canvas/Blackboard/etc.), a CENTRAL "
        f"SSO/CAS/Shibboleth/ADFS/OAuth login that fronts student services, "
        f"exam/results, tuition/fees, the main library, or student webmail. A "
        f"JavaScript app with an empty body, or an SSO redirect "
        f"(adfs/oauth/cas/shibboleth), still COUNTS if the URL/title/redirect/"
        f"fingerprints indicate a central student login — you do NOT require a "
        f"visible password field.\n"
        f"Set is_portal=FALSE for logins that are NOT for the general student "
        f"body, even if they are on the university's domain and have a login "
        f"form, specifically:\n"
        f"  - a single research lab / research group / institute / centre tool "
        f"(e.g. an astronomy-group or engineering-lab app),\n"
        f"  - one department's private internal app,\n"
        f"  - developer/IT infrastructure (gitlab, jenkins, jira, grafana, "
        f"VPN/ssl-vpn gateways, admin consoles),\n"
        f"  - e-commerce/shops, intramural/club sign-ups, event/conference "
        f"sites, alumni/donor logins,\n"
        f"  - staff/faculty/admin-only logins, HR/payroll,\n"
        f"  - the plain homepage, news/marketing, admissions/application forms "
        f"for PROSPECTIVE applicants, dead pages (fetch_error or status>=400).\n"
        f"When unsure whether a niche subdomain serves ALL students, give it a "
        f"LOW confidence (<0.5) rather than 1.0.\n"
        f"'belongs' = for THIS university (its own domain, or a vendor tenant "
        f"clearly branded/scoped to it).\n\n"
        f"Items:\n{json.dumps(items, ensure_ascii=False)}\n\n"
        f"Return ONLY a JSON array, one object per item, same order:\n"
        f'[{{"i":0,"is_portal":true,"belongs":true,"central_student":true,'
        f'"category":"Student Portal|LMS|SSO|Library|Webmail|Exam/Results|Fees|'
        f'Other","confidence":0.0-1.0,"reason":"short"}}]'
    )
    verdicts = _extract_json(_chat(prompt, model=JUDGE_MODEL))
    if not isinstance(verdicts, list):
        logger.warning("judge returned no parseable verdicts for a batch")
        return
    by_i = {v.get("i"): v for v in verdicts if isinstance(v, dict)}
    for i, c in enumerate(batch):
        if i not in by_i:
            continue  # no verdict for this item — leave unjudged (don't cache)
        v = by_i[i]
        c.judged = True
        c.is_portal = bool(v.get("is_portal"))
        # central_student defaults to True when the model omits it (older
        # replies) so we don't silently drop everything.
        c.central = bool(v.get("central_student", True))
        c.belongs = bool(v.get("belongs"))
        c.category = str(v.get("category", "") or "")
        try:
            c.confidence = float(v.get("confidence", 0) or 0)
        except (TypeError, ValueError):
            c.confidence = 0.0
        c.reason = str(v.get("reason", "") or "")[:300]


def _judge_cache_key(domain: str, c: Candidate) -> str:
    return f"judge:{domain}:{c.final_url or c.url}"


def judge(name: str, domain: str, cands: list[Candidate], batch_size: int = 10) -> None:
    # Apply cached verdicts first; only LLM-judge the URLs we haven't seen.
    todo: list[Candidate] = []
    for c in cands:
        v = _cache_get(_judge_cache_key(domain, c))
        if isinstance(v, dict):
            c.judged = True
            c.is_portal = bool(v.get("is_portal"))
            c.central = bool(v.get("central", True))
            c.belongs = bool(v.get("belongs"))
            c.category = v.get("category", "") or ""
            c.confidence = float(v.get("confidence", 0) or 0)
            c.reason = v.get("reason", "") or ""
        else:
            todo.append(c)
    logger.info("judge: %d cached, %d to judge", len(cands) - len(todo), len(todo))
    if not todo:
        return
    # Fewer, larger batches at low concurrency — keeps us under OpenRouter's
    # rate limit (bursty concurrency silently 429'd whole universities).
    batches = [todo[i:i + batch_size] for i in range(0, len(todo), batch_size)]
    workers = int(os.getenv("GLOBAL_JUDGE_WORKERS", "2"))
    with _cf.ThreadPoolExecutor(max_workers=max(1, workers)) as exe:
        list(exe.map(lambda b: _judge_batch(name, domain, b), batches))
    for c in todo:
        if not c.judged:
            continue  # batch failed/throttled for this one — don't cache a false verdict
        _cache_put(_judge_cache_key(domain, c), {
            "is_portal": c.is_portal, "central": c.central, "belongs": c.belongs,
            "category": c.category, "confidence": c.confidence, "reason": c.reason,
        })


# --------------------------------------------------------------------------- #
#  ORCHESTRATE                                                                 #
# --------------------------------------------------------------------------- #
def discover(name: str, domain: str, country: str = "") -> list[dict]:
    """Full rules-free discovery. Returns accepted portals as dicts."""
    domain = _norm_host("http://" + domain) if "://" not in domain else _norm_host(domain)
    logger.info("global-agent: discovering %s (%s)", name, domain)

    cands = harvest(name, domain, country)
    if not cands:
        return []

    with _cf.ThreadPoolExecutor(max_workers=FETCH_WORKERS) as exe:
        cands = list(exe.map(fetch_signals, cands))
    alive = [c for c in cands if not c.error and c.status and c.status < 500]
    logger.info("fetch: %d/%d candidates alive", len(alive), len(cands))

    # One-hop link-follow from live pages (portal hubs link to the real login /
    # SSO). Fetch the new links and add them to the judged set.
    seen_keys = {(c.url.rstrip("/")) for c in cands}
    followups = _followup_links(alive, domain, seen_keys)
    if followups:
        with _cf.ThreadPoolExecutor(max_workers=FETCH_WORKERS) as exe:
            followups = list(exe.map(fetch_signals, followups))
        new_alive = [c for c in followups if not c.error and c.status and c.status < 500]
        logger.info("link-follow: +%d links, %d alive", len(followups), len(new_alive))
        alive += new_alive

    # Cap the judged set on first runs (repeat runs are cache-cheap). Rank
    # portal-ish candidates first so the cap never drops the likely portals.
    judge_max = int(os.getenv("GLOBAL_JUDGE_MAX", "50"))
    if len(alive) > judge_max:
        alive.sort(key=_candidate_rank)
        logger.info("judge cap: %d -> %d candidates (portal-ish first)",
                    len(alive), judge_max)
        alive = alive[:judge_max]

    judge(name, domain, alive)

    accepted = [c for c in alive
                if c.is_portal and c.central and c.belongs
                and c.confidence >= CONFIDENCE_THRESHOLD
                and not _is_login_subpage(c.final_url or c.url)]
    # Dedup: one entry per (host, distinguishing-path-segment). Pure login-path
    # variants on a host (/, /login, /users/login, /login/index.php) collapse to
    # one, but genuinely distinct systems on a shared host survive (USP's
    # uspdigital.usp.br/jupiterweb vs /apolo vs /mercurioweb).
    best: dict[tuple[str, str], Candidate] = {}
    for c in sorted(accepted, key=lambda x: -x.confidence):
        u = _clean_url(c.final_url or c.url)
        key = (_norm_host(u), _distinguishing_segment(u))
        if key not in best:
            best[key] = c
    out = [{
        "url": _clean_url(c.final_url or c.url), "category": c.category or "Student Portal",
        "confidence": round(c.confidence, 2), "provenance": c.provenance,
        "reason": c.reason,
    } for c in sorted(best.values(), key=lambda x: -x.confidence)]
    logger.info("global-agent: %d portals accepted (from %d judged)", len(out), len(alive))
    return out

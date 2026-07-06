"""Rule mining from human feedback (Levels 1 & 2 of Genie 'training').

This is NOT machine learning — it aggregates the confirm/dispute log into
*proposed global rules* that a human reviews before they take effect:

  Level 1 (host):    a host disputed across several universities and never
                     confirmed anywhere  ->  propose a global host rule.
  Level 2 (pattern): a URL-path token (e.g. 'tenders', 'recruitment') that
                     recurs in disputes across universities and never appears
                     in a confirmed portal  ->  propose a global pattern rule.

The "never confirmed anywhere" guard is what makes this safe for shared
vendors: if a host/token is legitimately a student portal for *some* org, it
will have a confirmation and we won't propose blocking it.

Approved rules are applied as a post-filter over the agent's output in
`discover.py` (deny = drop, flag = keep+warn) — reversible, DB-driven, and
without touching the core agent's hand-tuned scoring.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

from . import db

# --- thresholds (tuned for high-precision proposals; humans review anyway) ---
HOST_MIN_SUPPORT = 3      # disputes
HOST_MIN_ORGS = 2         # distinct universities
PATTERN_MIN_SUPPORT = 4   # disputes
PATTERN_MIN_ORGS = 3      # distinct universities

# tokens too generic to be useful negative signals
_STOP = {
    "login", "logon", "signin", "sign", "portal", "portals", "student", "students",
    "index", "home", "main", "default", "php", "aspx", "asp", "jsp", "html", "htm",
    "www", "http", "https", "http", "com", "org", "net", "edu", "ac", "in", "co",
    "user", "users", "account", "auth", "app", "apps", "web", "site", "page", "view",
    "college", "university", "institute", "campus", "online", "welcome", "erp", "lms",
}
_TOKEN_RE = re.compile(r"[a-z][a-z]{2,}")  # alpha tokens, len>=3


def _host(url: str) -> str:
    return db._host(url)


def path_tokens(url: str) -> set[str]:
    """Meaningful alpha tokens from a URL's path/query (minus the stoplist)."""
    try:
        p = urlparse(url if "://" in url else "http://" + url)
        blob = f"{p.path} {p.query}".lower()
    except Exception:
        blob = (url or "").lower()
    return {t for t in _TOKEN_RE.findall(blob) if t not in _STOP}


def _load_feedback() -> tuple[list[dict], list[dict]]:
    with db.connect() as c:
        rows = c.execute("SELECT orgid, url, verdict, host FROM feedback").fetchall()
    disputes = [dict(r) for r in rows if r["verdict"] == "wrong"]
    confirms = [dict(r) for r in rows if r["verdict"] == "confirmed"]
    return disputes, confirms


def mine_rules(now: str = "") -> dict:
    """Aggregate feedback into proposed rules. Idempotent: refreshes counts on
    existing rules and preserves any human status/action. Returns a summary."""
    disputes, confirms = _load_feedback()

    # ---- Level 1: hosts ----
    host_orgs: dict[str, set[str]] = {}
    host_support: dict[str, int] = {}
    host_examples: dict[str, list[str]] = {}
    for d in disputes:
        h = d.get("host") or _host(d["url"])
        if not h:
            continue
        host_orgs.setdefault(h, set()).add(d["orgid"])
        host_support[h] = host_support.get(h, 0) + 1
        host_examples.setdefault(h, []).append(d["url"])
    confirmed_hosts: dict[str, int] = {}
    for cf in confirms:
        h = cf.get("host") or _host(cf["url"])
        if h:
            confirmed_hosts[h] = confirmed_hosts.get(h, 0) + 1

    # ---- Level 2: path tokens ----
    tok_orgs: dict[str, set[str]] = {}
    tok_support: dict[str, int] = {}
    tok_examples: dict[str, list[str]] = {}
    for d in disputes:
        for t in path_tokens(d["url"]):
            tok_orgs.setdefault(t, set()).add(d["orgid"])
            tok_support[t] = tok_support.get(t, 0) + 1
            tok_examples.setdefault(t, []).append(d["url"])
    confirmed_toks: dict[str, int] = {}
    for cf in confirms:
        for t in path_tokens(cf["url"]):
            confirmed_toks[t] = confirmed_toks.get(t, 0) + 1

    new = updated = 0

    for h, support in host_support.items():
        orgs = len(host_orgs[h])
        confs = confirmed_hosts.get(h, 0)
        if support >= HOST_MIN_SUPPORT and orgs >= HOST_MIN_ORGS and confs == 0:
            r = db.upsert_rule(rule_type="host", pattern=h, support=support, orgs=orgs,
                               confirms=confs, examples=host_examples[h], now=now)
            new += r == "new"; updated += r == "updated"

    for t, support in tok_support.items():
        orgs = len(tok_orgs[t])
        confs = confirmed_toks.get(t, 0)
        if support >= PATTERN_MIN_SUPPORT and orgs >= PATTERN_MIN_ORGS and confs == 0:
            r = db.upsert_rule(rule_type="pattern", pattern=t, support=support, orgs=orgs,
                               confirms=confs, examples=tok_examples[t], now=now)
            new += r == "new"; updated += r == "updated"

    return {"proposed_new": new, "refreshed": updated,
            "disputes_scanned": len(disputes), "confirms_scanned": len(confirms)}


def apply_rules(url: str, active: list[dict]) -> tuple[str, str]:
    """Match a URL against active global rules. Returns (action, pattern):
    action is '' (no match), 'flag', or 'deny'. 'deny' wins over 'flag'."""
    host = _host(url)
    toks = path_tokens(url)
    hit_action, hit_pattern = "", ""
    for r in active:
        matched = False
        if r["rule_type"] == "host":
            p = r["pattern"]
            matched = host == p or host.endswith("." + p)
        elif r["rule_type"] == "pattern":
            matched = r["pattern"] in toks
        if not matched:
            continue
        if r["action"] == "deny":
            return "deny", r["pattern"]
        if not hit_action:
            hit_action, hit_pattern = "flag", r["pattern"]
    return hit_action, hit_pattern

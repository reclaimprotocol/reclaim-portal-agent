"""Country-aware residential-proxy routing for target-site fetches.

Geo/WAF-blocked university portals (Cloudflare "you have been blocked", regional
locks) can't be reached from our India egress. This module builds a residential
proxy endpoint whose EXIT COUNTRY is chosen per-URL, so a fetch to `*.edu.br`
goes out through a Brazil IP, `*.edu.ng` through Nigeria, etc.

Country is derived from the URL's ccTLD (br/ng/mx/…), falling back to the org's
domains/country hint. India (`in`) and country-less gTLDs (`.com`/`.edu`/…) use a
DIRECT connection (no proxy) — we're already in India and generic TLDs aren't
geo-locked to a country we can target.

Off unless configured. Env:
  USE_PROXY=1
  RESIDENTIAL_PROXY_GATEWAY=gw.provider.com:7000
  RESIDENTIAL_PROXY_USER=<account/customer id>
  RESIDENTIAL_PROXY_PASS=<password>
  RESIDENTIAL_PROXY_USER_TEMPLATE={user}-country-{cc}   # provider-specific; {cc}=iso2
Everything returns None when unconfigured, so callers fall back to direct.
"""
from __future__ import annotations

import os
from urllib.parse import urlsplit

# org COUNTRY NAME -> ISO2 (used when a country hint is available, e.g. discovery)
_NAME2CC = {
    "brazil": "br", "mexico": "mx", "méxico": "mx", "argentina": "ar", "chile": "cl",
    "colombia": "co", "peru": "pe", "perú": "pe", "ecuador": "ec", "bolivia": "bo",
    "uruguay": "uy", "paraguay": "py", "venezuela": "ve", "dominican republic": "do",
    "guatemala": "gt", "honduras": "hn", "el salvador": "sv", "nicaragua": "ni",
    "costa rica": "cr", "panama": "pa", "nigeria": "ng", "kenya": "ke", "ghana": "gh",
    "south africa": "za", "egypt": "eg", "philippines": "ph", "indonesia": "id",
    "bangladesh": "bd", "pakistan": "pk", "malaysia": "my", "vietnam": "vn",
    "thailand": "th", "india": "in",
}
# ccTLDs we treat as country codes (2-letter country tld). Excludes generic tlds.
_SKIP_CC = {"in"}   # we are IN India — India orgs use direct connection


def _cctld(host: str) -> str | None:
    host = (host or "").lower().strip().rstrip(".")
    if not host:
        return None
    tld = host.split(".")[-1]
    if len(tld) == 2 and tld.isalpha():
        return tld
    return None


def country_code(country_name: str = "", *hosts: str) -> str | None:
    """Best ISO2 exit country: explicit name first, else any host's ccTLD."""
    if country_name:
        cc = _NAME2CC.get(country_name.strip().lower())
        if cc:
            return cc
    for h in hosts:
        for token in str(h).replace(",", " ").split():
            host = urlsplit(token if "://" in token else "http://" + token).netloc or token
            cc = _cctld(host)
            if cc:
                return cc
    return None


def _cfg():
    if os.getenv("USE_PROXY", "0").strip().lower() not in ("1", "true", "yes", "on"):
        return None
    gw = os.getenv("RESIDENTIAL_PROXY_GATEWAY", "").strip()
    user = os.getenv("RESIDENTIAL_PROXY_USER", "").strip()
    pw = os.getenv("RESIDENTIAL_PROXY_PASS", "").strip()
    if not (gw and user and pw):
        return None
    tmpl = os.getenv("RESIDENTIAL_PROXY_USER_TEMPLATE", "{user}-country-{cc}")
    return gw, user, pw, tmpl


def _creds(cc: str):
    """(gateway, username-with-country, password) or None if not routable."""
    if not cc or cc in _SKIP_CC:
        return None
    cfg = _cfg()
    if not cfg:
        return None
    gw, user, pw, tmpl = cfg
    return gw, tmpl.format(user=user, cc=cc), pw


def requests_proxies(country_name: str = "", *hosts: str):
    """`proxies=` dict for `requests`, or None for direct."""
    c = _creds(country_code(country_name, *hosts))
    if not c:
        return None
    gw, u, pw = c
    url = f"http://{u}:{pw}@{gw}"
    return {"http": url, "https": url}


def playwright_proxy(country_name: str = "", *hosts: str):
    """`proxy=` dict for Playwright `new_context`, or None for direct."""
    c = _creds(country_code(country_name, *hosts))
    if not c:
        return None
    gw, u, pw = c
    return {"server": f"http://{gw}", "username": u, "password": pw}


def active_country(country_name: str = "", *hosts: str) -> str | None:
    """The exit country that would be used (None = direct). For logging."""
    return country_code(country_name, *hosts) if _creds(country_code(country_name, *hosts)) else None

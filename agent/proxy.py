"""Country-aware residential-proxy routing for target-site fetches.

Geo/WAF-blocked university portals (Cloudflare "you have been blocked", regional
locks) can't be reached from our India egress. This module builds a residential
proxy endpoint whose EXIT COUNTRY is chosen per-URL, so a fetch to `*.edu.br`
goes out through a Brazil IP, `*.edu.ng` through Nigeria, etc.

Country is derived from the URL's ccTLD (br/ng/mx/…), falling back to the org's
domains/country hint. Two cases use a DIRECT connection: domains in our OWN
egress country (PROXY_HOME_CC, default `in`), because a direct connection is
already a local one; and country-less gTLDs (`.com`/`.edu`/…), which aren't
geo-locked to a country we could target anyway.

Set PROXY_HOME_CC to match where this actually runs. It defaults to `in` for the
office; a US-hosted deploy must set `us`, or Indian universities are fetched
direct from the US — see the note on PROXY_HOME_CC below.

Off unless configured. Env:
  USE_PROXY=1
  PROXY_HOME_CC=in                                   # where WE egress from
  RESIDENTIAL_PROXY_GATEWAY=gw.provider.com:7000     # host:port, NO scheme
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
    # August4000 batch geographies — needed when an org's domains carry no
    # ccTLD (.com/.org), so the country NAME is the only exit hint we have.
    "ukraine": "ua", "morocco": "ma", "kazakhstan": "kz", "tanzania": "tz",
    "algeria": "dz", "zambia": "zm", "azerbaijan": "az", "sri lanka": "lk",
    "saudi arabia": "sa", "jamaica": "jm", "turkey": "tr", "türkiye": "tr",
    "nepal": "np", "uganda": "ug", "ethiopia": "et", "rwanda": "rw",
    "cameroon": "cm", "senegal": "sn", "ivory coast": "ci", "tunisia": "tn",
    "jordan": "jo", "iraq": "iq", "uzbekistan": "uz", "georgia": "ge",
}
# ccTLDs we treat as country codes (2-letter country tld). Excludes generic tlds.
#
#: The country this agent EGRESSES from. Domains in that country skip the proxy,
#: because a direct connection is already a local one.
#:
#: This was hardcoded to "in" on the assumption the agent always runs from the
#: office in India. Hosting it broke that assumption SILENTLY: from a US server
#: every `.ac.in` and `.edu.in` university was still fetched direct — the one
#: routing decision that used to be right became the one that is most wrong, and
#: the logs still said "via direct" as though nothing had changed.
#:
#: Measured on the deployed service: buet.ac.bd returned no portals from a US
#: exit and the correct BIIS portal through a Bangladesh one. The crawl did not
#: fail in the first case — it returned a DIFFERENT page, with enough links to
#: pass the filter and none of them the portal. Nothing in the output said the
#: geography was wrong. India is the largest slice of the org list (542
#: institutions share samarth.edu.in alone), so getting this backwards is
#: expensive and invisible.
#:
#: Defaults to "in" so an office run is unchanged. Set PROXY_HOME_CC=us on a
#: US-hosted deploy, and Indian orgs then route through an India exit.
PROXY_HOME_CC = (os.getenv("PROXY_HOME_CC", "in") or "").strip().lower()

#: Empty PROXY_HOME_CC means "nowhere is local" — proxy every ccTLD we can.
_SKIP_CC = {PROXY_HOME_CC} if PROXY_HOME_CC else set()


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

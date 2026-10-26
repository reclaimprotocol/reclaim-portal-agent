"""Country-aware residential-proxy routing for target-site fetches.

Geo/WAF-blocked university portals (Cloudflare "you have been blocked", regional
locks) can't be reached from our India egress. This module builds a residential
proxy endpoint whose EXIT COUNTRY is chosen per-URL, so a fetch to `*.edu.br`
goes out through a Brazil IP, `*.edu.ng` through Nigeria, etc.

In dynamic mode, country is derived from the organisation country hint and then
the portal URL's ccTLD (br/ng/mx/…). This also works for country-less portal
domains (`.com`/`.edu`/…) when the sheet supplies the organisation country.
There is no hardcoded India route. Operators may optionally bypass their real
egress country, or force one fixed proxy country for diagnostics.

Off unless configured. Env:
  USE_PROXY=1
  PROXY_GEO_MODE=dynamic                             # dynamic | fixed | off
  PROXY_GEO_CC=                                      # required in fixed mode
  PROXY_HOME_CC=in                                   # where WE egress from
  PROXY_BYPASS_HOME=0                                # 1 = direct for HOME_CC
  RESIDENTIAL_PROXY_GATEWAY=gw.provider.com:7000     # host:port, NO scheme
  RESIDENTIAL_PROXY_USER=<account/customer id>
  RESIDENTIAL_PROXY_PASS=<password>
  RESIDENTIAL_PROXY_USER_TEMPLATE={user}-country-{cc}   # Bright Data style

Optional Grass fallback for countries unavailable from the primary provider:
  PROXY_PROVIDER=auto                         # auto | brightdata | grass
  GRASS_PROXY_GATEWAY=proxy.grass.io:80
  GRASS_PROXY_USER=<subaccount username>
  GRASS_PROXY_PASS=<subaccount password>
  GRASS_PROXY_COUNTRIES=ir,iq,sy,ru,cu,kp

Grass is always used in ``direct`` mode. Chromium receives a stable per-host
session ID, as Grass requires browsers to keep all parallel requests on one
exit IP. Smart mode is intentionally unsupported because Grass documents that
it terminates TLS and must not be combined with a real browser.
Everything returns None when unconfigured, so callers fall back to direct.
"""
from __future__ import annotations

import hashlib
import os
import re
import secrets
from urllib.parse import quote, urlsplit

# org COUNTRY NAME -> ISO2 (used when a country hint is available, e.g. discovery)
_NAME2CC = {
    "brazil": "br", "mexico": "mx", "méxico": "mx", "argentina": "ar", "chile": "cl",
    "colombia": "co", "peru": "pe", "perú": "pe", "ecuador": "ec", "bolivia": "bo",
    "uruguay": "uy", "paraguay": "py", "venezuela": "ve", "dominican republic": "do",
    "guatemala": "gt", "honduras": "hn", "el salvador": "sv", "nicaragua": "ni",
    "costa rica": "cr", "panama": "pa", "nigeria": "ng", "kenya": "ke", "ghana": "gh",
    "south africa": "za", "egypt": "eg", "philippines": "ph", "indonesia": "id",
    "bangladesh": "bd", "pakistan": "pk", "malaysia": "my", "vietnam": "vn",
    "thailand": "th", "india": "in", "japan": "jp", "taiwan": "tw",
    "hong kong": "hk", "united arab emirates": "ae", "uae": "ae",
    "russia": "ru", "russian federation": "ru", "france": "fr",
    "united kingdom": "gb", "uk": "gb", "england": "gb",
    "united states": "us", "united states of america": "us", "usa": "us",
    # August4000 batch geographies — needed when an org's domains carry no
    # ccTLD (.com/.org), so the country NAME is the only exit hint we have.
    "ukraine": "ua", "morocco": "ma", "kazakhstan": "kz", "tanzania": "tz",
    "algeria": "dz", "zambia": "zm", "azerbaijan": "az", "sri lanka": "lk",
    "saudi arabia": "sa", "jamaica": "jm", "turkey": "tr", "türkiye": "tr",
    "nepal": "np", "uganda": "ug", "ethiopia": "et", "rwanda": "rw",
    "cameroon": "cm", "senegal": "sn", "ivory coast": "ci", "tunisia": "tn",
    "jordan": "jo", "iraq": "iq", "iran": "ir", "islamic republic of iran": "ir",
    "uzbekistan": "uz", "georgia": "ge",
}

_GRASS_PROCESS_SESSION = secrets.token_hex(6)
_DEFAULT_GRASS_COUNTRIES = "ir,iq,sy,ru,cu,kp"
# ccTLDs we treat as country codes (2-letter country tld). Excludes generic tlds.
#
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
        name = country_name.strip().lower().replace("_", " ")
        # Accept ISO2 directly so callers do not need a country-name table.
        if len(name) == 2 and name.isalpha():
            return name
        cc = _NAME2CC.get(name)
        if cc:
            return cc
    for h in hosts:
        for token in str(h).replace(",", " ").split():
            host = urlsplit(token if "://" in token else "http://" + token).netloc or token
            cc = _cctld(host)
            if cc:
                return cc
    return None


def route_country(country_name: str = "", *hosts: str) -> str | None:
    """Return the configured proxy exit country for this portal.

    ``dynamic`` (default) follows the organisation/portal location. ``fixed``
    uses ``PROXY_GEO_CC`` for every target, and ``off`` disables geo proxying
    without requiring credentials to be removed. The machine's home country is
    bypassed only when ``PROXY_BYPASS_HOME=1`` is explicitly configured.
    """
    mode = (os.getenv("PROXY_GEO_MODE", "dynamic") or "dynamic").strip().lower()
    if mode in {"off", "none", "direct", "disabled"}:
        return None
    if mode in {"fixed", "force", "forced"}:
        cc = (os.getenv("PROXY_GEO_CC", "") or "").strip().lower()
        if len(cc) != 2 or not cc.isalpha():
            return None
    else:
        # Treat target/portal/auto as aliases for dynamic mode so config reads
        # naturally and older deployments do not fail on a harmless synonym.
        cc = country_code(country_name, *hosts)

    if not cc:
        return None
    bypass_home = (os.getenv("PROXY_BYPASS_HOME", "0") or "0").strip().lower()
    home_cc = (os.getenv("PROXY_HOME_CC", "") or "").strip().lower()
    if bypass_home in {"1", "true", "yes", "on"} and home_cc == cc:
        return None
    return cc


def _proxy_enabled() -> bool:
    return os.getenv("USE_PROXY", "0").strip().lower() in ("1", "true", "yes", "on")


def _bright_cfg():
    if not _proxy_enabled():
        return None
    gw = os.getenv("RESIDENTIAL_PROXY_GATEWAY", "").strip()
    user = os.getenv("RESIDENTIAL_PROXY_USER", "").strip()
    pw = os.getenv("RESIDENTIAL_PROXY_PASS", "").strip()
    if not (gw and user and pw):
        return None
    tmpl = os.getenv("RESIDENTIAL_PROXY_USER_TEMPLATE", "{user}-country-{cc}")
    return gw, user, pw, tmpl


def _grass_cfg():
    if not _proxy_enabled():
        return None
    gateway = (os.getenv("GRASS_PROXY_GATEWAY", "proxy.grass.io:80") or "").strip()
    user = (os.getenv("GRASS_PROXY_USER", "") or "").strip()
    password = (os.getenv("GRASS_PROXY_PASS", "") or "").strip()
    if not (gateway and user and password):
        return None
    template = os.getenv(
        "GRASS_PROXY_USER_TEMPLATE",
        "username-{user}-direct-country-{cc}{session}",
    )
    return gateway, user, password, template


def _grass_countries() -> set[str]:
    raw = os.getenv("GRASS_PROXY_COUNTRIES", _DEFAULT_GRASS_COUNTRIES)
    return {item.strip().lower() for item in raw.split(",")
            if len(item.strip()) == 2 and item.strip().isalpha()}


def _provider_order(cc: str) -> tuple[str, ...]:
    """Configured providers in retry order for ``cc``.

    Countries explicitly assigned to Grass do not try Bright Data because that
    list exists for geographies the primary provider does not serve. Everywhere
    else Bright Data is primary and Grass is the transport-failure fallback.
    Forced provider modes intentionally disable cross-provider failover.
    """
    selected = (os.getenv("PROXY_PROVIDER", "auto") or "auto").strip().lower()
    if selected in {"grass", "grassnetwork"}:
        return ("grass",) if _grass_cfg() else ()
    if selected in {"brightdata", "bright_data", "bright"}:
        return ("brightdata",) if _bright_cfg() else ()
    if cc in _grass_countries() and _grass_cfg():
        return ("grass",)
    providers: list[str] = []
    if _bright_cfg():
        providers.append("brightdata")
    if _grass_cfg():
        providers.append("grass")
    return tuple(providers)


def _provider(cc: str) -> str | None:
    providers = _provider_order(cc)
    return providers[0] if providers else None


def _grass_session(cc: str, hosts: tuple[str, ...]) -> str:
    host_key = "|".join(str(host).strip().lower() for host in hosts if host)
    digest = hashlib.sha256(
        f"{_GRASS_PROCESS_SESSION}|{cc}|{host_key}".encode()).hexdigest()[:16]
    return digest


def _creds(
    cc: str,
    *hosts: str,
    browser: bool = False,
    provider: str | None = None,
):
    """(provider, gateway, routed username, password), or None."""
    if not cc:
        return None
    provider = provider or _provider(cc)
    if provider == "grass":
        cfg = _grass_cfg()
        if not cfg:
            return None
        gateway, user, password, template = cfg
        if user.startswith("username-"):
            user = user.removeprefix("username-")
        session = (f"-session-{_grass_session(cc, hosts)}" if browser else "")
        routed_user = template.format(
            user=user, mode="direct", cc=cc.upper(), session=session)
        return provider, gateway, routed_user, password
    cfg = _bright_cfg()
    if provider != "brightdata" or not cfg:
        return None
    gateway, user, password, template = cfg
    return provider, gateway, template.format(user=user, cc=cc), password


def provider_order(country_name: str = "", *hosts: str) -> tuple[str, ...]:
    """Public provider retry order for this target."""
    cc = route_country(country_name, *hosts)
    return _provider_order(cc) if cc else ()


def requests_proxy_candidates(country_name: str = "", *hosts: str):
    """Ordered ``(provider, requests proxies)`` candidates for failover."""
    cc = route_country(country_name, *hosts)
    output = []
    for provider in (_provider_order(cc) if cc else ()):
        creds = _creds(cc, *hosts, provider=provider)
        if not creds:
            continue
        name, gateway, user, password = creds
        url = f"http://{quote(user, safe='')}:{quote(password, safe='')}@{gateway}"
        output.append((name, {"http": url, "https": url}))
    return output


def playwright_proxy_candidates(country_name: str = "", *hosts: str):
    """Ordered ``(provider, Playwright proxy config)`` candidates."""
    cc = route_country(country_name, *hosts)
    output = []
    for provider in (_provider_order(cc) if cc else ()):
        creds = _creds(cc, *hosts, browser=True, provider=provider)
        if not creds:
            continue
        name, gateway, user, password = creds
        output.append((name, {
            "server": f"http://{gateway}", "username": user, "password": password,
        }))
    return output


_PROXY_FAILURE_RE = re.compile(
    r"proxyerror|proxy error|proxy connection|proxyconnect|clienthttpproxyerror|"
    r"tunnel connection|err_tunnel|err_proxy|\b407\b|proxy authentication|"
    r"cannot connect to host .*proxy|connection reset by proxy",
    re.I,
)


def is_proxy_failure(error: object) -> bool:
    """True only for errors that identify the proxy transport itself."""
    return bool(_PROXY_FAILURE_RE.search(str(error or "")))


def requests_proxies(country_name: str = "", *hosts: str):
    """`proxies=` dict for `requests`, or None for direct."""
    candidates = requests_proxy_candidates(country_name, *hosts)
    return candidates[0][1] if candidates else None


def playwright_proxy(country_name: str = "", *hosts: str):
    """`proxy=` dict for Playwright `new_context`, or None for direct."""
    candidates = playwright_proxy_candidates(country_name, *hosts)
    return candidates[0][1] if candidates else None


def active_country(country_name: str = "", *hosts: str) -> str | None:
    """The exit country that would be used (None = direct). For logging."""
    cc = route_country(country_name, *hosts)
    return cc if _creds(cc, *hosts) else None


def active_provider(country_name: str = "", *hosts: str) -> str | None:
    """Selected provider for this target, or None for a direct connection."""
    cc = route_country(country_name, *hosts)
    creds = _creds(cc, *hosts)
    return creds[0] if creds else None

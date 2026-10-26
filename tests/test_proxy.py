from __future__ import annotations

from agent import proxy


def _configure(monkeypatch) -> None:
    monkeypatch.setenv("USE_PROXY", "1")
    monkeypatch.setenv("RESIDENTIAL_PROXY_GATEWAY", "proxy.example:1234")
    monkeypatch.setenv("RESIDENTIAL_PROXY_USER", "customer-zone")
    monkeypatch.setenv("RESIDENTIAL_PROXY_PASS", "secret")
    monkeypatch.delenv("RESIDENTIAL_PROXY_USER_TEMPLATE", raising=False)
    monkeypatch.delenv("PROXY_GEO_MODE", raising=False)
    monkeypatch.delenv("PROXY_GEO_CC", raising=False)
    monkeypatch.delenv("PROXY_BYPASS_HOME", raising=False)
    monkeypatch.delenv("PROXY_PROVIDER", raising=False)
    monkeypatch.delenv("GRASS_PROXY_USER", raising=False)
    monkeypatch.delenv("GRASS_PROXY_PASS", raising=False)


def _configure_grass(monkeypatch) -> None:
    monkeypatch.setenv("GRASS_PROXY_GATEWAY", "proxy.grass.io:80")
    monkeypatch.setenv("GRASS_PROXY_USER", "grass-subaccount")
    monkeypatch.setenv("GRASS_PROXY_PASS", "grass-secret")


def test_country_code_accepts_name_iso2_and_cctld() -> None:
    assert proxy.country_code("Brazil", "vendor.com") == "br"
    assert proxy.country_code("IQ", "vendor.com") == "iq"
    assert proxy.country_code("", "https://portal.example.edu.jp/login") == "jp"
    assert proxy.country_code("Hong Kong", "vendor.com") == "hk"
    assert proxy.country_code("Iran", "vendor.com") == "ir"


def test_dynamic_geo_uses_portal_location_even_for_home_country(monkeypatch) -> None:
    _configure(monkeypatch)
    monkeypatch.setenv("PROXY_HOME_CC", "in")

    assert proxy.active_country("Brazil", "portal.vendor.com") == "br"
    assert proxy.active_country("India", "portal.vendor.com") == "in"
    cfg = proxy.playwright_proxy("Brazil", "portal.vendor.com")
    assert cfg is not None
    assert cfg["username"].endswith("-country-br")


def test_home_country_bypass_is_explicit(monkeypatch) -> None:
    _configure(monkeypatch)
    monkeypatch.setenv("PROXY_HOME_CC", "in")
    monkeypatch.setenv("PROXY_BYPASS_HOME", "1")

    assert proxy.active_country("India", "example.ac.in") is None
    assert proxy.active_country("Brazil", "example.edu.br") == "br"


def test_fixed_geo_overrides_portal_location(monkeypatch) -> None:
    _configure(monkeypatch)
    monkeypatch.setenv("PROXY_GEO_MODE", "fixed")
    monkeypatch.setenv("PROXY_GEO_CC", "us")

    assert proxy.active_country("Brazil", "example.edu.br") == "us"


def test_geo_mode_off_disables_proxy(monkeypatch) -> None:
    _configure(monkeypatch)
    monkeypatch.setenv("PROXY_GEO_MODE", "off")

    assert proxy.requests_proxies("Brazil", "example.edu.br") is None


def test_auto_uses_grass_for_bright_data_excluded_country(monkeypatch) -> None:
    _configure(monkeypatch)
    _configure_grass(monkeypatch)

    assert proxy.active_provider("Iran", "portal.example.com") == "grass"
    cfg = proxy.playwright_proxy("Iran", "portal.example.com")
    assert cfg is not None
    assert cfg["server"] == "http://proxy.grass.io:80"
    assert cfg["username"].startswith(
        "username-grass-subaccount-direct-country-IR-session-")


def test_grass_browser_session_is_sticky_per_host(monkeypatch) -> None:
    _configure(monkeypatch)
    _configure_grass(monkeypatch)

    first = proxy.playwright_proxy("Iraq", "portal.example.edu.iq")
    second = proxy.playwright_proxy("Iraq", "portal.example.edu.iq")
    other = proxy.playwright_proxy("Iraq", "lms.example.edu.iq")

    assert first is not None and second is not None and other is not None
    assert first["username"] == second["username"]
    assert first["username"] != other["username"]


def test_auto_keeps_bright_data_for_supported_country(monkeypatch) -> None:
    _configure(monkeypatch)
    _configure_grass(monkeypatch)

    assert proxy.active_provider("Brazil", "portal.example.edu.br") == "brightdata"
    cfg = proxy.playwright_proxy("Brazil", "portal.example.edu.br")
    assert cfg is not None
    assert cfg["server"] == "http://proxy.example:1234"


def test_auto_orders_grass_after_bright_data_for_transport_failover(monkeypatch) -> None:
    _configure(monkeypatch)
    _configure_grass(monkeypatch)

    assert proxy.provider_order("Brazil", "portal.example.edu.br") == (
        "brightdata", "grass")
    candidates = proxy.playwright_proxy_candidates(
        "Brazil", "portal.example.edu.br")
    assert [provider for provider, _cfg in candidates] == [
        "brightdata", "grass"]


def test_excluded_country_does_not_retry_unsupported_bright_data(monkeypatch) -> None:
    _configure(monkeypatch)
    _configure_grass(monkeypatch)

    assert proxy.provider_order("Iran", "portal.example.com") == ("grass",)


def test_only_proxy_transport_errors_trigger_provider_failover() -> None:
    assert proxy.is_proxy_failure("407 Proxy Authentication Required")
    assert proxy.is_proxy_failure("ERR_TUNNEL_CONNECTION_FAILED")
    assert proxy.is_proxy_failure("ProxyError: unable to connect")
    assert not proxy.is_proxy_failure("HTTP 403 from the university")
    assert not proxy.is_proxy_failure("HTTP 500 from the target application")


def test_forced_grass_routes_all_countries(monkeypatch) -> None:
    _configure(monkeypatch)
    _configure_grass(monkeypatch)
    monkeypatch.setenv("PROXY_PROVIDER", "grass")

    assert proxy.active_provider("Brazil", "portal.example.edu.br") == "grass"

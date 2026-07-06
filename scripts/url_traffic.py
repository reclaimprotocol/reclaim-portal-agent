#!/usr/bin/env python3
"""Gauge how heavily-used a URL / domain (e.g. a student login portal) is, via
three pluggable providers:

  * openpagerank — domain authority 0-10 + global rank + referring domains
                   (free key, env OPR_API_KEY; new Keywords Everywhere API).
                   Covers low/obscure domains (Common Crawl web graph).
  * cloudflare   — Radar DNS-usage rank bucket (free token, env
                   CLOUDFLARE_API_TOKEN). Subdomain-aware; reaches low domains.

There is NO free source of absolute monthly *visit counts* for third-party
domains — that's only available from paid panel data (SimilarWeb / Semrush).
openpagerank/cloudflare give relative popularity signals for free.

Keys are read from --api-key, the matching env var, or the repo-root .env.

Usage:
  python scripts/url_traffic.py --provider ahrefs --url https://erp.aktu.ac.in/
  python scripts/url_traffic.py --provider cloudflare --url a.ac.in --url b.edu.in
  python scripts/url_traffic.py --provider openpagerank --file urls.txt --out traffic.csv
"""
from __future__ import annotations

import csv
import os
import time
from datetime import date
from urllib.parse import urlparse

import click
import requests

try:  # load keys from the repo-root .env (OPR_API_KEY / CLOUDFLARE_API_TOKEN)
    from pathlib import Path
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except Exception:
    pass

OPR_ENDPOINT = "https://openpagerank.keywordseverywhere.com/v1/domains/bulk"
CF_RADAR_ENDPOINT = "https://api.cloudflare.com/client/v4/radar/ranking/domain/{domain}"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
# Multi-part public suffixes so we reduce a host to its registrable domain.
_MULTI_TLDS = (
    "ac.in", "edu.in", "co.in", "gov.in", "org.in", "net.in", "nic.in", "res.in",
    "ac.bd", "edu.bd", "gov.bd", "com.bd", "ac.uk", "edu.au", "edu.pk", "edu.np",
)


def host_of(url: str) -> str:
    u = url.strip()
    if "://" not in u:
        u = "http://" + u
    host = (urlparse(u).netloc or "").lower().split(":")[0]
    return host[4:] if host.startswith("www.") else host


def registrable(host: str) -> str:
    for suf in _MULTI_TLDS:
        if host.endswith("." + suf):
            labels = host[: -(len(suf) + 1)].split(".")
            return labels[-1] + "." + suf if labels else host
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def openpagerank_for(url: str, api_key: str, timeout: float = 20.0) -> dict:
    """Domain AUTHORITY (0-10) + global rank from OpenPageRank (new Keywords
    Everywhere API: POST /v1/domains/bulk, Bearer auth). Covers low/obscure
    domains via Common Crawl's open web graph. NOT visit counts — a popularity
    proxy. Also returns referring-domains count."""
    host = host_of(url)
    dom = registrable(host)
    out = {"url": url, "queried": dom, "status": "no data",
           "page_rank": None, "global_rank": None, "referring_domains": None, "as_of": None}
    try:
        r = requests.post(OPR_ENDPOINT,
                          headers={"Authorization": f"Bearer {api_key}",
                                   "Content-Type": "application/json"},
                          json={"domains": [dom]}, timeout=timeout)
        j = r.json()
        results = j.get("results") or []
        if results:
            it = results[0]
            out.update(page_rank=it.get("open_page_rank"),
                       global_rank=it.get("rank"),
                       referring_domains=it.get("referring_domains"),
                       as_of=j.get("as_of"), status="ok")
        elif not r.ok:
            out["status"] = f"HTTP {r.status_code}: {str(j)[:80]}"
    except Exception as e:
        out["status"] = f"error: {type(e).__name__}"
    return out


def cloudflare_for(url: str, api_token: str, timeout: float = 20.0) -> dict:
    """Domain ranking bucket from Cloudflare Radar (1.1.1.1 DNS-query volume).
    Works on subdomains and reaches lower-traffic domains than SimilarWeb.
    Returns a rank/bucket (relative usage), not literal visit counts."""
    host = host_of(url)  # Radar can rank the full host (subdomain)
    out = {"url": url, "queried": host, "status": "no data", "rank": None, "bucket": None}
    try:
        r = requests.get(CF_RADAR_ENDPOINT.format(domain=host),
                         headers={"Authorization": f"Bearer {api_token}"}, timeout=timeout)
        j = r.json()
        if j.get("success"):
            res = (j.get("result") or {})
            det = res.get("details_0") or res
            out.update(rank=det.get("rank"), bucket=det.get("bucket"), status="ok")
        else:
            out["status"] = "no data / not ranked"
    except Exception as e:
        out["status"] = f"error: {type(e).__name__}"
    return out


def _fmt(n):
    return f"{n:,}" if isinstance(n, int) else "—"


@click.command()
@click.option("--url", "urls", multiple=True, help="URL or domain (repeatable).")
@click.option("--file", "file", default=None, help="File with one URL/domain per line.")
@click.option("--provider", type=click.Choice(["openpagerank", "cloudflare"]),
              default="openpagerank", show_default=True,
              help="openpagerank=authority 0-10 + rank + referring domains (free key, covers low domains); "
                   "cloudflare=Radar DNS-usage rank (free token, subdomain-capable).")
@click.option("--api-key", default=None, help="API key/token. Falls back to env OPR_API_KEY / CLOUDFLARE_API_TOKEN.")
@click.option("--out", "out", default=None, help="Write results to this CSV.")
@click.option("--sleep", "sleep_s", type=float, default=1.5, show_default=True, help="Delay between lookups (rate-limit friendly).")
def main(urls, file, provider, api_key, out, sleep_s):
    items = list(urls)
    if file:
        with open(file) as f:
            items += [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
    if not items:
        raise SystemExit("provide --url and/or --file")

    env_key = {"openpagerank": "OPR_API_KEY", "cloudflare": "CLOUDFLARE_API_TOKEN"}[provider]
    key = api_key or os.getenv(env_key)
    if not key:
        hint = {"openpagerank": "a free key at openpagerank.keywordseverywhere.com",
                "cloudflare": "a free token at dash.cloudflare.com (Radar read)"}[provider]
        raise SystemExit(f"{provider} needs a key: --api-key or env {env_key} ({hint})")

    rows = []
    for i, u in enumerate(items):
        if provider == "openpagerank":
            r = openpagerank_for(u, key)
            click.echo(f"{u}\n   queried={r['queried']}  status={r['status']}  "
                       f"authority(0-10)={r['page_rank']}  global_rank={r['global_rank']}  "
                       f"referring_domains={r['referring_domains']}  as_of={r['as_of']}")
        else:  # cloudflare
            r = cloudflare_for(u, key)
            click.echo(f"{u}\n   queried={r['queried']}  status={r['status']}  "
                       f"radar_rank={r['rank']}  bucket={r['bucket']}")
        rows.append(r)
        if i < len(items) - 1:
            time.sleep(sleep_s)

    if out:
        keys = list(dict.fromkeys(k for r in rows for k in r))
        with open(out, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=keys)
            w.writeheader()
            w.writerows(rows)
        click.echo(f"\nWrote {out} ({len(rows)} rows)")


if __name__ == "__main__":
    main()

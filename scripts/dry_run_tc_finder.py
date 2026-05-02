"""Dry-run: print every Stage C probe + final aggregated row for one OrgID,
WITHOUT touching the sheet or the analyzer cache.

For each portal of the target OrgID:
  * Run `tc_finder.find_tc_for_portal` inside `tc_probe_trace()` so every
    strict-validation attempt is captured (HTTP status, Content-Type, body
    length / PDF page count, decision, gate that fired).
  * Print the per-probe table.
  * Print the final selected T&C URL (or "(none)" + Yes (No T&C Found)).

Then run the analyzer on each accepted T&C URL with `force_refresh=True`
so the verdict comes from a fresh fetch, not the cache. Aggregate verdicts
via `tc_analyzer.aggregate_verdicts` and print the row that *would* be
written by Stage D — but never write it.

Example:
    python scripts/dry_run_tc_finder.py --orgid 664135
"""
from __future__ import annotations

import logging

import _bootstrap  # noqa: F401
import click

from agent.config import (
    CATEGORY_ORDER,
    CATEGORY_REMAP_FOR_SORTING,
    load_config,
)
from agent.sheets_client import SheetsClient
from agent.stages import discovery_rules, tc_analyzer, tc_finder
from agent.stages.js_renderer import JSRenderer
from agent.stages.sheet_writer import _portal_sort_key
from agent.state import StateStore


@click.command()
@click.option("--orgid", required=True, help="OrgID to dry-run")
@click.option("--debug", is_flag=True, help="Verbose tc-finder/tc-analyzer logging")
def main(orgid: str, debug: bool) -> None:
    config = load_config()
    if debug:
        logging.basicConfig(level="DEBUG", format="%(levelname)s %(name)s :: %(message)s")
        logging.getLogger("googleapiclient").setLevel(logging.WARNING)
        logging.getLogger("urllib3").setLevel(logging.WARNING)
    else:
        logging.basicConfig(level="WARNING")
    target = str(orgid).strip()

    sheets = SheetsClient.from_config(config)
    universities = sheets.read_universities()
    uni_row = next((u for u in universities if SheetsClient.extract_orgid(u) == target), None)
    if uni_row is None:
        raise click.ClickException(f"OrgID {target} not in Universities tab")
    university_name = str(uni_row.get("SheerID University Name", "")).strip()
    raw_domains = str(uni_row.get("SheerID Website Domain", ""))
    domains = discovery_rules.parse_domains(raw_domains)

    overrides = config.domain_overrides.get(target, {}) or {}
    extra_roots = [str(x).lower().lstrip(".") for x in overrides.get("extra_allowed_root_domains", []) if x]
    effective_domains = list(domains) + [r for r in extra_roots if r not in domains]

    js_renderer: JSRenderer | None = None
    if config.enable_js_rendering:
        js_renderer = JSRenderer(
            timeout_seconds=config.js_rendering_timeout_seconds,
            user_agent=config.user_agent,
        )

    try:
        with StateStore(config.state_db_path) as state:
            disc = state.get_result(target, "discovery") or {}
            portals = [
                {
                    "url": str(p.get("url") or "").strip(),
                    "js_rendered": bool(p.get("js_rendered")),
                    "category": str(p.get("category") or "Other"),
                }
                for p in (disc.get("portals") or [])
                if (p.get("url") or "").strip()
            ]
            if not portals:
                raise click.ClickException(
                    f"No cached portals for OrgID {target} in state.db — run Stage A first"
                )
            for p in portals:
                p["category"] = discovery_rules.infer_category(
                    p["url"], fallback=p.get("category") or "Other",
                )
            portals.sort(key=_portal_sort_key)

            portal_urls = [p["url"] for p in portals]
            university_domain = tc_finder.infer_university_domain(portal_urls, domains)

            click.echo("=" * 120)
            click.echo(f"OrgID:               {target}")
            click.echo(f"University:          {university_name}")
            click.echo(f"Configured domains:  {domains}")
            click.echo(f"Effective domains:   {effective_domains}")
            click.echo(f"Inferred uni domain: {university_domain}")
            click.echo(f"Portals (sorted):    {len(portals)}")
            for p in portals:
                click.echo(f"  • [{p['category']:<22}] {p['url']}")
            click.echo("=" * 120)

            findings: list[tuple[str, str | None, str | None]] = []  # (portal, tc_url, source)
            tc_urls_seen: set[str] = set()
            tc_urls_ordered: list[str] = []
            verdicts: list[str] = []

            for portal in portals:
                portal_url = portal["url"]
                click.echo()
                click.echo("─" * 120)
                click.echo(f"PORTAL: {portal_url}")
                click.echo("─" * 120)

                with tc_finder.tc_probe_trace() as trace:
                    finding = tc_finder.find_tc_for_portal(
                        portal_url=portal_url,
                        domains=effective_domains,
                        js_rendered_hint=bool(portal.get("js_rendered")),
                        js_renderer=js_renderer,
                        user_agent=config.user_agent,
                        http_timeout=config.http_timeout_seconds,
                        orgid=target,
                        university_domain=university_domain,
                    )

                if trace:
                    click.echo(
                        f"  {'CANDIDATE URL':<70} {'STATUS':<6} {'CT':<24} {'BYTES':<7} DECISION  REASON"
                    )
                    for r in trace:
                        ct = (r.content_type or "")[:22]
                        size = (
                            f"{r.body_len:>7}" if r.body_len is not None else "       "
                        )
                        if r.is_pdf and r.pdf_pages is not None:
                            size = f"PDF×{r.pdf_pages:<4}"
                        url_short = (r.url or "")[:68]
                        click.echo(
                            f"  {url_short:<70} {str(r.http_status or '-'):<6} {ct:<24} {size:<7} "
                            f"{r.decision:<8} {r.reason[:80]}"
                        )
                else:
                    click.echo("  (no probe attempts — Stage 1/2 found a link via anchor scoring with no validation calls)")

                tc_url = finding.get("tc_url")
                source = finding.get("source") or "(no T&C found)"
                click.echo()
                if tc_url:
                    click.echo(f"  → SELECTED: {tc_url}")
                    click.echo(f"    via {source}")
                else:
                    click.echo("  → SELECTED: (none) — verdict will default to 'Yes (No T&C Found)'")
                findings.append((portal_url, tc_url, source))

                if tc_url:
                    analysis = tc_analyzer.analyze_tc_url(
                        tc_url=tc_url, state=state,
                        user_agent=config.user_agent,
                        http_timeout=config.http_timeout_seconds,
                        orgid=target,
                        force_refresh=True,
                    )
                    verdict = str(analysis.get("verdict") or "Yes (No T&C Found)")
                    click.echo(f"    verdict:   {verdict}")
                    click.echo(f"    reasoning: {analysis.get('reasoning')}")
                    key = tc_url.strip().lower().rstrip("/")
                    if key not in tc_urls_seen:
                        tc_urls_seen.add(key)
                        tc_urls_ordered.append(tc_url)
                    verdicts.append(verdict)
                else:
                    verdicts.append("Yes (No T&C Found)")

            overall_verdict = tc_analyzer.aggregate_verdicts(verdicts)

            click.echo()
            click.echo("=" * 120)
            click.echo("AGGREGATED ROW (would be written; sheet NOT modified):")
            click.echo("=" * 120)
            click.echo(f"  OrgID:               {target}")
            click.echo(f"  University Name:     {university_name}")
            click.echo("  Portal URLs:")
            for p in portals:
                click.echo(f"    {p['url']}")
            click.echo("  T&C URLs:")
            if tc_urls_ordered:
                for u in tc_urls_ordered:
                    click.echo(f"    {u}")
            else:
                click.echo("    (blank)")
            click.echo(f"  Overall T&C Verdict: {overall_verdict}")
            click.echo("=" * 120)
            click.echo("(dry-run only — sheet NOT modified, state.db NOT mutated beyond")
            click.echo(" tc_analyzer cache writes from --force-refresh fresh analyses)")
    finally:
        if js_renderer is not None:
            js_renderer.close()


if __name__ == "__main__":
    main()

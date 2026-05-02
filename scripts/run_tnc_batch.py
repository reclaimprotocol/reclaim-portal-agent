"""Stage C batch — re-aggregate the Portals tab across many OrgIDs.

For each eligible OrgID (existing row missing an Overall T&C Verdict, or
all OrgIDs when `--force`), invokes the same per-OrgID logic as
`run_tnc_only.py`: load portals from state.db (or split the sheet cell),
run Stage C.1 + C.2 per portal, aggregate, and rewrite the OrgID's row.

The shared Playwright browser launches once across the whole batch.
The `tc_analyzer_cache` table dedups any T&C URLs that recur across
universities (same Samarth-style legal page, MKCL DigitalUniversity, etc.).

Examples:
    python scripts/run_tnc_batch.py --limit 20
    python scripts/run_tnc_batch.py --limit 20 --force
"""
from __future__ import annotations

import _bootstrap  # noqa: F401
import click

from agent.config import load_config
from agent.sheets_client import SheetsClient
from agent.stages import discovery_rules, tc_analyzer, tc_finder
from agent.stages.js_renderer import JSRenderer
from agent.state import StateStore


@click.command()
@click.option(
    "--limit", type=int, default=20, show_default=True,
    help="Max number of distinct OrgIDs to process",
)
@click.option(
    "--force", is_flag=True,
    help="Re-analyse OrgIDs that already have an Overall T&C Verdict",
)
def main(limit: int, force: bool) -> None:
    config = load_config()
    _bootstrap.setup_logging(config.log_level)

    sheets = SheetsClient.from_config(config)
    sheets.ensure_portals_header()

    universities = sheets.read_universities()
    uni_by_orgid: dict[str, dict] = {}
    domains_by_orgid: dict[str, list[str]] = {}
    for u in universities:
        oid = SheetsClient.extract_orgid(u)
        if not oid:
            continue
        uni_by_orgid[oid] = u
        domains_by_orgid[oid] = discovery_rules.parse_domains(
            str(u.get("SheerID Website Domain", ""))
        )

    portal_rows = sheets.read_portals()
    rows_by_orgid: dict[str, tuple[int, dict]] = {}
    for idx, row in enumerate(portal_rows):
        oid = SheetsClient.extract_orgid(row)
        if not oid:
            continue
        # First row wins if duplicates exist (legacy schema cleanup).
        rows_by_orgid.setdefault(oid, (idx + 2, row))

    eligible_orgids: list[str] = []
    for oid, (_, row) in rows_by_orgid.items():
        if force or not str(row.get("Overall T&C Verdict", "")).strip():
            eligible_orgids.append(oid)
    eligible_orgids.sort()
    targets = eligible_orgids[:limit]

    click.echo(
        f"batch: {len(targets)}/{len(eligible_orgids)} OrgIDs queued "
        f"(of {len(rows_by_orgid)} with rows; force={force})"
    )

    js_renderer: JSRenderer | None = None
    if config.enable_js_rendering:
        js_renderer = JSRenderer(
            timeout_seconds=config.js_rendering_timeout_seconds,
            user_agent=config.user_agent,
        )

    stats = {
        "orgids_processed": 0,
        "rows_written": 0,
        "portals_analyzed": 0,
        "errors": 0,
    }

    try:
        with StateStore(config.state_db_path) as state:
            for oid in targets:
                domains = domains_by_orgid.get(oid, [])
                overrides = config.domain_overrides.get(oid, {}) or {}
                extra_roots = [str(x).lower().lstrip(".") for x in overrides.get("extra_allowed_root_domains", []) if x]
                effective_domains = list(domains) + [r for r in extra_roots if r not in domains]

                row_num, sheet_row = rows_by_orgid[oid]
                portals = _load_portals(oid, state=state, sheet_row=sheet_row)
                if not portals:
                    click.echo(f"[{oid}] no portals in state.db or sheet — skipping", err=True)
                    continue

                university_name = (
                    str(uni_by_orgid.get(oid, {}).get("SheerID University Name", "")).strip()
                    or str(sheet_row.get("University Name", "")).strip()
                )
                portal_urls = [p["url"] for p in portals]
                university_domain = tc_finder.infer_university_domain(portal_urls, domains)

                tc_urls_seen: set[str] = set()
                tc_urls_ordered: list[str] = []
                verdicts: list[str] = []

                for portal in portals:
                    portal_url = portal["url"]
                    try:
                        finding = tc_finder.find_tc_for_portal(
                            portal_url=portal_url,
                            domains=effective_domains,
                            js_rendered_hint=bool(portal.get("js_rendered")),
                            js_renderer=js_renderer,
                            user_agent=config.user_agent,
                            http_timeout=config.http_timeout_seconds,
                            orgid=oid,
                            university_domain=university_domain,
                        )
                        tc_url = finding.get("tc_url") or ""
                        if tc_url:
                            analysis = tc_analyzer.analyze_tc_url(
                                tc_url=tc_url, state=state,
                                user_agent=config.user_agent,
                                http_timeout=config.http_timeout_seconds,
                                orgid=oid,
                            )
                        else:
                            analysis = {
                                "verdict": "Yes (No T&C Found)",
                                "evidence": "No T&C document found for this portal or its university",
                                "reasoning": "Defaulting to permissive — no document to analyze",
                            }
                    except Exception as err:
                        click.echo(f"[{oid}] ERROR on {portal_url}: {err}", err=True)
                        stats["errors"] += 1
                        continue

                    stats["portals_analyzed"] += 1
                    verdicts.append(str(analysis.get("verdict") or "Yes (No T&C Found)"))
                    if tc_url:
                        key = tc_url.strip().lower().rstrip("/")
                        if key not in tc_urls_seen:
                            tc_urls_seen.add(key)
                            tc_urls_ordered.append(tc_url)

                overall_verdict = tc_analyzer.aggregate_verdicts(verdicts)
                new_row = {
                    "OrgID": oid,
                    "University Name": university_name,
                    "Portal URLs": "\n".join(portal_urls),
                    "T&C URLs": "\n".join(tc_urls_ordered),
                    "Overall T&C Verdict": overall_verdict,
                }
                values = [new_row.get(col, "") for col in SheetsClient.PORTALS_COLUMNS]
                sheets.update_portal_rows([(row_num, values)])
                stats["rows_written"] += 1
                stats["orgids_processed"] += 1
                click.echo(
                    f"[{oid}] updated row: {len(portal_urls)} portals, "
                    f"{len(tc_urls_ordered)} unique T&C URLs, overall={overall_verdict!r}"
                )
    finally:
        if js_renderer is not None:
            js_renderer.close()

    click.echo(f"done: {stats}")


def _load_portals(orgid: str, *, state: StateStore, sheet_row: dict) -> list[dict]:
    """state.db cached Stage A → preferred. Fall back to splitting the sheet
    row's "Portal URLs" cell on "\\n"."""
    cached = state.get_result(orgid, "discovery")
    if isinstance(cached, dict) and cached.get("portals"):
        return [
            {
                "url": str(p.get("url") or "").strip(),
                "js_rendered": bool(p.get("js_rendered")),
            }
            for p in cached["portals"]
            if (p.get("url") or "").strip()
        ]
    cell = str(sheet_row.get("Portal URLs", "")).strip()
    return [
        {"url": line.strip(), "js_rendered": False}
        for line in cell.split("\n")
        if line.strip()
    ]


if __name__ == "__main__":
    main()

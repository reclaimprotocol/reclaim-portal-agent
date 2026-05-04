"""Stage C only — re-run the T&C finder + analyzer for one OrgID and
re-aggregate its single Portals-tab row.

Reads the OrgID's portal list from state.db's cached Stage A discovery
result (preferred) or, if that's missing, by splitting the existing
Portals-tab row's "Portal URLs" cell on "\\n". For each portal:

  1. Run Stage C.1 (`tc_finder.find_tc_for_portal`) on the portal URL.
  2. Run Stage C.2 (`tc_analyzer.analyze_tc_url`) on whatever it returned
     (cached by normalized URL in state.db).

Then aggregates per-portal verdicts via `tc_analyzer.aggregate_verdicts`,
deduplicates T&C URLs, and rewrites the OrgID's row with the schema:

    OrgID | University Name | Portal URLs | T&C URLs | Overall T&C Verdict

Examples:
    python scripts/run_tnc_only.py --orgid 663848
    python scripts/run_tnc_only.py --orgid 663848 --force
"""
from __future__ import annotations

import logging

import _bootstrap  # noqa: F401
import click

from agent.config import load_config
from agent.sheets_client import SheetsClient
from agent.stages import discovery_rules, tc_analyzer, tc_finder
from agent.stages.js_renderer import JSRenderer
from agent.stages.sheet_writer import _portal_sort_key
from agent.state import StateStore


@click.command()
@click.option("--orgid", required=True, help="OrgID whose Portals row to refresh")
@click.option(
    "--force", is_flag=True,
    help="Re-run full T&C discovery (tc_finder) even when the sheet's T&C URLs cell "
         "is already populated; also bypasses the per-URL analyzer cache so updated "
         "scoring logic takes effect. Without --force, a populated T&C URLs cell "
         "short-circuits to analyzer-only re-aggregation against the URLs already in "
         "the sheet.",
)
@click.option(
    "--debug", is_flag=True,
    help="Print every fallback-URL probe (kept/rejected) and every keyword match "
         "(kept/dropped) with its 80-char window",
)
def main(orgid: str, force: bool, debug: bool) -> None:
    config = load_config()
    _bootstrap.setup_logging("DEBUG" if debug else config.log_level)
    if debug:
        # Keep googleapiclient / urllib3 quiet — they spam at DEBUG.
        logging.getLogger("googleapiclient").setLevel(logging.WARNING)
        logging.getLogger("urllib3").setLevel(logging.WARNING)
    target = str(orgid).strip()

    sheets = SheetsClient.from_config(config)
    sheets.ensure_portals_header()
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

    # Lazy: only the full-discovery branch needs Playwright. The
    # analyzer-only short-circuit doesn't.
    js_renderer: JSRenderer | None = None

    stats = {"portals_analyzed": 0, "no_tc_found": 0, "errors": 0, "unique_tc_urls": 0}

    try:
        with StateStore(config.state_db_path) as state:
            existing_sheet_rows = sheets.read_portals_by_orgid(target)
            existing_tc_urls: list[str] = []
            if existing_sheet_rows:
                first_row = existing_sheet_rows[0][1]
                existing_tc_urls = [
                    line.strip()
                    for line in str(first_row.get("T&C URLs", "")).split("\n")
                    if line.strip()
                ]

            # Short-circuit: T&C URLs already in the sheet → skip
            # tc_finder discovery entirely and just (re-)run the
            # analyzer against the known URLs. --force opts back into
            # full discovery.
            if existing_tc_urls and not force:
                row_num, first_row = existing_sheet_rows[0]
                click.echo(
                    f"[{target}] using {len(existing_tc_urls)} T&C URL(s) from "
                    f"sheet, skipping discovery (pass --force to re-discover)"
                )
                verdicts: list[str] = []
                for tc_url in existing_tc_urls:
                    try:
                        analysis = tc_analyzer.analyze_tc_url(
                            tc_url=tc_url,
                            state=state,
                            user_agent=config.user_agent,
                            http_timeout=config.http_timeout_seconds,
                            orgid=target,
                            force_refresh=False,
                        )
                    except Exception as err:
                        click.echo(
                            f"[{target}] ERROR analysing {tc_url}: {err}",
                            err=True,
                        )
                        stats["errors"] += 1
                        continue
                    verdict = str(analysis.get("verdict") or "Yes (No T&C Found)")
                    verdicts.append(verdict)
                    click.echo(f"[{target}] {tc_url} → {verdict!r}")
                    stats["portals_analyzed"] += 1
                stats["unique_tc_urls"] = len(existing_tc_urls)
                overall_verdict = tc_analyzer.aggregate_verdicts(verdicts)
                new_row = {
                    "OrgID": target,
                    "University Name": (
                        str(first_row.get("University Name", "")).strip()
                        or university_name
                    ),
                    "Portal URLs": str(first_row.get("Portal URLs", "")),
                    "T&C URLs": "\n".join(existing_tc_urls),
                    "Overall T&C Verdict": overall_verdict,
                }
                values = [
                    new_row.get(col, "") for col in SheetsClient.PORTALS_COLUMNS
                ]
                sheets.update_portal_rows([(row_num, values)])
                click.echo(
                    f"[{target}] updated row: {len(existing_tc_urls)} T&C URL(s), "
                    f"overall={overall_verdict!r}"
                )
                click.echo(f"done: {stats}")
                return

            # Full-discovery path — init the JS renderer now (deferred
            # so the analyzer-only short-circuit doesn't pay for it).
            if config.enable_js_rendering:
                js_renderer = JSRenderer(
                    timeout_seconds=config.js_rendering_timeout_seconds,
                    user_agent=config.user_agent,
                )

            portals = _load_portals(target, state=state, sheet_rows=existing_sheet_rows)
            if not portals:
                raise click.ClickException(
                    f"No portals found for OrgID {target} in state.db or sheet — run Stage A first"
                )

            # Re-apply the categorizer with the latest signal set (LMS/Moodle
            # score-based detection, etc.) using the stored category as
            # fallback so search-query-origin classifications survive when no
            # rule fires. Then sort by `CATEGORY_ORDER` so the cell renders
            # in canonical order (Student Portal → LMS/Moodle → Examination
            # → Library → Fee → Other).
            for p in portals:
                p["category"] = discovery_rules.infer_category(
                    p["url"], fallback=p.get("category") or "Other",
                )
            portals.sort(key=_portal_sort_key)

            portal_urls = [p["url"] for p in portals]
            university_domain = tc_finder.infer_university_domain(portal_urls, domains)
            if university_domain:
                click.echo(f"[{target}] inferred university domain = {university_domain}")

            tc_urls_seen: set[str] = set()
            tc_urls_ordered: list[str] = []
            verdicts: list[str] = []

            for portal in portals:
                portal_url = portal["url"]
                js_hint = bool(portal.get("js_rendered"))
                try:
                    finding = tc_finder.find_tc_for_portal(
                        portal_url=portal_url,
                        domains=effective_domains,
                        js_rendered_hint=js_hint,
                        js_renderer=js_renderer,
                        user_agent=config.user_agent,
                        http_timeout=config.http_timeout_seconds,
                        orgid=target,
                        university_domain=university_domain,
                    )
                    tc_url = finding.get("tc_url") or ""
                    if tc_url:
                        analysis = tc_analyzer.analyze_tc_url(
                            tc_url=tc_url,
                            state=state,
                            user_agent=config.user_agent,
                            http_timeout=config.http_timeout_seconds,
                            orgid=target,
                            force_refresh=force,
                        )
                    else:
                        analysis = {
                            "verdict": "Yes (No T&C Found)",
                            "evidence": "No T&C document found for this portal or its university",
                            "reasoning": "Defaulting to permissive — no document to analyze",
                        }
                        stats["no_tc_found"] += 1
                except Exception as err:
                    click.echo(f"[{target}] ERROR analysing {portal_url}: {err}", err=True)
                    stats["errors"] += 1
                    continue

                stats["portals_analyzed"] += 1
                verdicts.append(str(analysis.get("verdict") or "Yes (No T&C Found)"))
                if tc_url:
                    key = tc_url.strip().lower().rstrip("/")
                    if key not in tc_urls_seen:
                        tc_urls_seen.add(key)
                        tc_urls_ordered.append(tc_url)
                click.echo(
                    f"[{target}] {portal_url} → {analysis['verdict']!r} (tc_url={tc_url or '(none)'})"
                )

            stats["unique_tc_urls"] = len(tc_urls_ordered)
            overall_verdict = tc_analyzer.aggregate_verdicts(verdicts)

            new_row = {
                "OrgID": target,
                "University Name": university_name,
                "Portal URLs": "\n".join(portal_urls),
                "T&C URLs": "\n".join(tc_urls_ordered),
                "Overall T&C Verdict": overall_verdict,
            }

            if existing_sheet_rows:
                row_num, _ = existing_sheet_rows[0]
                values = [new_row.get(col, "") for col in SheetsClient.PORTALS_COLUMNS]
                sheets.update_portal_rows([(row_num, values)])
                outcome = "updated"
            else:
                sheets.append_portal_rows([new_row])
                outcome = "appended"

            click.echo(
                f"[{target}] {outcome} row: {len(portal_urls)} portals, "
                f"{len(tc_urls_ordered)} unique T&C URLs, overall={overall_verdict!r}"
            )
    finally:
        if js_renderer is not None:
            js_renderer.close()

    click.echo(f"done: {stats}")


def _load_portals(
    orgid: str,
    *,
    state: StateStore,
    sheet_rows: list[tuple[int, dict]] | None = None,
) -> list[dict]:
    """Prefer state.db's cached Stage A discovery result. Fall back to
    splitting the existing sheet row's "Portal URLs" cell on "\\n" — the
    sheet-fallback portals are minimal (no category from state) and get
    fed through `infer_category` upstream to classify them.
    Preserves `category` so `_portal_sort_key` can group correctly."""
    cached = state.get_result(orgid, "discovery")
    if isinstance(cached, dict) and cached.get("portals"):
        return [
            {
                "url": str(p.get("url") or "").strip(),
                "js_rendered": bool(p.get("js_rendered")),
                "category": str(p.get("category") or "Other"),
            }
            for p in cached["portals"]
            if (p.get("url") or "").strip()
        ]
    if sheet_rows:
        first_row = sheet_rows[0][1]
        cell = str(first_row.get("Portal URLs", "")).strip()
        return [
            {"url": line.strip(), "js_rendered": False, "category": "Other"}
            for line in cell.split("\n")
            if line.strip()
        ]
    return []


if __name__ == "__main__":
    main()

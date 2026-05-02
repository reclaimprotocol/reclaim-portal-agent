"""Per-portal Stage C diagnostic for one OrgID — does NOT write to the sheet.

Runs Stage A discovery → Stage C.1 finder → instrumented Stage C.2 scoring
and prints, for every portal:
  * portal URL
  * T&C URL the finder selected (and source — per-portal-link / -probe / university-level)
  * raw snippet of T&C content (first 500 chars)
  * every keyword match: strength, exact term, offset, 80-char surrounding
    window, and whether NEGATION_PHRASES_NEAR appears in that window
  * per-portal verdict + the keyword(s) that triggered it

Then prints the OrgID-level aggregate via `tc_analyzer.aggregate_verdicts`.

Reads from / writes to state.db (Stage A discovery is cached so repeated
diagnostic runs are cheap), but never touches the Portals tab.

Example:
    python scripts/diagnose_tc.py --orgid 5819165
"""
from __future__ import annotations

import logging

import _bootstrap  # noqa: F401
import click

from agent.config import load_config
from agent.pipeline import PipelineContext
from agent.sheets_client import SheetsClient
from agent.stages import discovery, tc_analyzer, tc_finder
from agent.stages.js_renderer import JSRenderer
from agent.stages.tc_analyzer import (
    MODERATE_PROHIBITIVE_KEYWORDS,
    NEGATION_PHRASES_NEAR,
    PROHIBITION_INDICATOR_PHRASES,
    STRONG_PROHIBITIVE_KEYWORDS,
    _fetch_tc_text,
    _MAX_TEXT_LEN,
    _MIN_TEXT_LEN,
    _MODERATE_WINDOW_HALF,
    _score_tc_text,
)
from agent.state import StateStore


_STRONG_WINDOW_HALF: int = 40  # mirrors the diagnostic intent — strong matches don't use a window in scoring
_QUOTE_WINDOW_HALF: int = 40   # 80 chars total around each match for the diagnostic print


@click.command()
@click.option("--orgid", required=True, help="OrgID to diagnose")
def main(orgid: str) -> None:
    config = load_config()
    # Quiet most of the agent's INFO logs — diagnostic output should dominate.
    logging.basicConfig(level="WARNING", format="%(levelname)s %(name)s :: %(message)s")
    target = str(orgid).strip()

    sheets = SheetsClient.from_config(config)
    unis = sheets.read_universities()
    uni_row = next(
        (u for u in unis if SheetsClient.extract_orgid(u) == target),
        None,
    )
    if uni_row is None:
        raise click.ClickException(f"OrgID {target} not in Universities tab")
    uni_name = str(uni_row.get("SheerID University Name", "")).strip()

    js_renderer: JSRenderer | None = None
    if config.enable_js_rendering:
        js_renderer = JSRenderer(
            timeout_seconds=config.js_rendering_timeout_seconds,
            user_agent=config.user_agent,
        )

    try:
        with StateStore(config.state_db_path) as state:
            ctx = PipelineContext(
                orgid=target, row=uni_row,
                deps={"js_renderer": js_renderer, "state": state,
                      "user_agent": config.user_agent,
                      "http_timeout": config.http_timeout_seconds},
            )

            # ---- Stage A (use cache if present) ----
            cached = state.get_result(target, "discovery")
            if cached is not None:
                disc_result = cached
                click.echo(f"[Stage A] using cached discovery for {target}")
            else:
                click.echo(f"[Stage A] running discovery for {target} (no cache)…")
                disc_result = discovery.run(ctx)
                state.save_result(target, "discovery", disc_result)
            ctx.results["discovery"] = disc_result
            portals = disc_result.get("portals") or []

            click.echo("=" * 88)
            click.echo(f"OrgID:           {target}")
            click.echo(f"University:      {uni_name}")
            click.echo(f"Configured doms: {disc_result.get('domains')}")
            click.echo(f"Portals found:   {len(portals)}")
            for p in portals:
                click.echo(f"  • {p.get('url')}  (js_rendered={bool(p.get('js_rendered'))})")
            click.echo("=" * 88)
            click.echo()

            if not portals:
                click.echo("No portals discovered — nothing to analyse.")
                return

            # ---- Stage C.1 ----
            tcf_result = tc_finder.run(ctx)
            findings = tcf_result.get("tc_findings") or []

            verdicts: list[str] = []
            for finding in findings:
                portal_url = finding["portal_url"]
                tc_url = finding.get("tc_url")
                source = finding.get("source")

                click.echo("─" * 88)
                click.echo(f"PORTAL:    {portal_url}")
                click.echo(f"T&C URL:   {tc_url or '(none)'}")
                click.echo(f"Source:    {source or '(no T&C found)'}")

                if not tc_url:
                    click.echo("VERDICT:   Yes (No T&C Found)")
                    click.echo("Reasoning: Defaulting to permissive — no document to analyze")
                    verdicts.append("Yes (No T&C Found)")
                    click.echo()
                    continue

                text = _fetch_tc_text(
                    tc_url,
                    user_agent=config.user_agent,
                    http_timeout=config.http_timeout_seconds,
                )
                if not text or len(text.strip()) < _MIN_TEXT_LEN:
                    click.echo(f"Content:   (empty / under {_MIN_TEXT_LEN} chars — defaulting to Yes)")
                    click.echo("VERDICT:   Yes (No T&C Found)")
                    verdicts.append("Yes (No T&C Found)")
                    click.echo()
                    continue

                snippet = text[:500].replace("\n", " ").replace("  ", " ")
                click.echo(f"Length:    {len(text)} chars")
                click.echo(f"Snippet:   {snippet!r}")

                matches = _diagnostic_scan(text)
                if not matches:
                    click.echo("Matches:   (none)")
                else:
                    click.echo(f"Matches:   {len(matches)}")
                    for m in matches:
                        click.echo(f"  [{m['strength']}] '{m['term']}' @ offset {m['offset']}")
                        click.echo(f"    window:    ...{m['window']}...")
                        click.echo(f"    negation_present:    {m['negation_present']}")
                        click.echo(f"    prohibition_indicator: {m['prohibition_indicator']}")
                        click.echo(f"    classified_as: {m['context']}")

                # Authoritative verdict from the same scoring path the
                # pipeline uses — keeps the diagnostic in sync with prod.
                truncated = text[:_MAX_TEXT_LEN]
                result = _score_tc_text(truncated)
                triggers = _explain_trigger(matches, result["verdict"])
                click.echo(f"VERDICT:   {result['verdict']}")
                click.echo(f"Reasoning: {result['reasoning']}")
                if triggers:
                    click.echo(f"Triggered by: {triggers}")
                verdicts.append(result["verdict"])
                click.echo()

            click.echo("=" * 88)
            agg = tc_analyzer.aggregate_verdicts(verdicts)
            click.echo(f"Per-portal verdicts: {verdicts}")
            click.echo(f"AGGREGATE VERDICT:   {agg}")
            click.echo("=" * 88)
    finally:
        if js_renderer is not None:
            js_renderer.close()


# ------------------------------------------------------------------ helpers


def _diagnostic_scan(text: str) -> list[dict]:
    """Re-implements `tc_analyzer._score_tc_text`'s match-finding loop with
    extra fields captured for printing. The actual verdict is still computed
    via `_score_tc_text` so the diagnostic and prod stay aligned — this
    function only reports *what* matched, not what the verdict is."""
    text_lower = text.lower()
    out: list[dict] = []
    covered: list[tuple[int, int]] = []

    def is_covered(s: int, e: int) -> bool:
        return any(s >= cs and e <= ce for cs, ce in covered)

    for kw in sorted(STRONG_PROHIBITIVE_KEYWORDS, key=len, reverse=True):
        idx = text_lower.find(kw)
        if idx == -1:
            continue
        end = idx + len(kw)
        if is_covered(idx, end):
            continue
        covered.append((idx, end))
        win = _window_around(text_lower, idx, end, _QUOTE_WINDOW_HALF)
        out.append({
            "strength": "STRONG",
            "term": kw,
            "offset": idx,
            "window": win,
            "negation_present": any(p in win for p in NEGATION_PHRASES_NEAR),
            "prohibition_indicator": any(p in win for p in PROHIBITION_INDICATOR_PHRASES),
            "context": "prohibitive",  # strong matches always classified prohibitive
        })

    for kw in sorted(MODERATE_PROHIBITIVE_KEYWORDS, key=len, reverse=True):
        idx = text_lower.find(kw)
        if idx == -1:
            continue
        end = idx + len(kw)
        if is_covered(idx, end):
            continue
        covered.append((idx, end))
        win = _window_around(text_lower, idx, end, _MODERATE_WINDOW_HALF)
        negation = any(p in win for p in NEGATION_PHRASES_NEAR)
        prohibition = any(p in win for p in PROHIBITION_INDICATOR_PHRASES)
        if negation:
            ctx_label = "permissive"
        elif prohibition:
            ctx_label = "prohibitive"
        else:
            ctx_label = "ambiguous"
        out.append({
            "strength": "MODERATE",
            "term": kw,
            "offset": idx,
            "window": win,
            "negation_present": negation,
            "prohibition_indicator": prohibition,
            "context": ctx_label,
        })
    return out


def _window_around(text_lower: str, start: int, end: int, half: int) -> str:
    ws = max(0, start - half)
    we = min(len(text_lower), end + half)
    return text_lower[ws:we].replace("\n", " ")


def _explain_trigger(matches: list[dict], verdict: str) -> str:
    """Map a verdict back to the matches that drove it (for the diagnostic
    print). Mirrors the if/elif ladder in `_score_tc_text`."""
    strong = [m for m in matches if m["strength"] == "STRONG" and m["context"] == "prohibitive"]
    moderate_prohibitive = [m for m in matches if m["strength"] == "MODERATE" and m["context"] == "prohibitive"]
    moderate_ambiguous = [m for m in matches if m["strength"] == "MODERATE" and m["context"] == "ambiguous"]
    if verdict == "No":
        if strong:
            terms = ", ".join(f"'{m['term']}'@{m['offset']}" for m in strong[:3])
            return f"STRONG: {terms}"
        if len(moderate_prohibitive) >= 2:
            terms = ", ".join(f"'{m['term']}'@{m['offset']}" for m in moderate_prohibitive[:3])
            return f"MODERATE×{len(moderate_prohibitive)}: {terms}"
    if verdict == "Maybe":
        if len(moderate_prohibitive) == 1:
            m = moderate_prohibitive[0]
            return f"single MODERATE prohibitive: '{m['term']}'@{m['offset']}"
        if moderate_ambiguous:
            terms = ", ".join(f"'{m['term']}'@{m['offset']}" for m in moderate_ambiguous[:3])
            return f"AMBIGUOUS MODERATE: {terms}"
    return ""


if __name__ == "__main__":
    main()

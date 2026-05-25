"""Eval driver — compare agent Portal URLs against ground truth and
emit a fix prompt that scripts/run_autotune.py can hand to Claude Code.

Ground truth lives in the Universities sheet, column E
("Reclaim Protocol Login Page Url"). The agent's output is whatever
the Portals tab currently holds for that OrgID (multiline newline-
joined URLs in the "Portal URLs" cell).

CLI contract — driven by scripts/run_autotune.py
-------------------------------------------------

    python scripts/run_finetune_eval.py \
        --start <int> --end <int> \
        [--skip-discovery] \
        --output <path-to-fixes_needed.md>

Outputs
-------

* <output>:        Markdown fix prompt for Claude Code (Step 3 of the
                   autotune loop).
* <output>.json:   Stats sidecar matching the schema documented in
                   run_autotune.py:

      {
        "accuracy":                  float,       # 0..100
        "correct":                   int,
        "evaluated":                 int,
        "patterns_detected":         list[str],
        "errors":                    list[dict],
        "domain_overrides_proposed": int
      }

* eval_logs/<orgid>_row<row>.log: per-row discovery log capture (only
  written when running WITHOUT --skip-discovery).

Discovery behaviour
-------------------

Without --skip-discovery, the eval re-runs Stage A (discovery) + Stage
C.1 (tc_finder) for each row with cache busted (force=True), captures
every log line emitted while that OrgID is being processed, and writes
the fresh result to the Portals sheet. The agent's output then read
back from the sheet is guaranteed to reflect the current codebase.

With --skip-discovery, the eval skips re-running discovery and reads
whatever the Portals sheet currently holds. The verification step in
the autotune loop uses this — caveat that it only catches behaviour
changes that are observable in the sheet output (i.e. nothing yet, if
the Claude-applied fix needs Stage A to re-execute). Re-run the
autotune without --skip-discovery to validate Stage A changes.

Pattern classification
----------------------

Each mismatched row is bucketed into one of:

  no_portals_found, wrong_domain, admission_portal_accepted,
  shared_platform_missed, shortname_mismatch, path_mismatch, one_off

A bucket with ≥2 rows is reported as a systemic pattern with a
targeted Claude Code prompt. Rows in `one_off` get domain_overrides
suggestions instead.
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import _bootstrap  # noqa: F401  -- must come before agent.* imports
import click

from agent.config import (
    AFFILIATING_UNIVERSITY_PORTALS,
    KNOWN_SHARED_PLATFORM_PATTERNS,
    load_config,
)
from agent.pipeline import PipelineContext
from agent.sheets_client import SheetsClient
from agent.stages import discovery, tc_finder
from agent.stages.js_renderer import JSRenderer
from agent.stages.sheet_writer import _portal_sort_key
from agent.state import StateStore

logger = logging.getLogger(__name__)

REPO_ROOT = Path(_bootstrap.ROOT)
EVAL_LOGS_DIR = REPO_ROOT / "eval_logs"
GROUND_TRUTH_COL = "Reclaim Protocol Login Page Url"


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class RowEval:
    sheet_row: int
    orgid: str
    university_name: str
    ground_truth: str
    agent_urls: list[str]
    is_correct: bool
    pattern: str = ""               # set for incorrect rows
    log_snippet: str = ""           # set for incorrect rows
    log_file: str = ""              # set when discovery ran for this row


@dataclass
class EvalReport:
    correct: int
    evaluated: int
    rows: list[RowEval] = field(default_factory=list)

    @property
    def accuracy(self) -> float:
        if self.evaluated == 0:
            return 0.0
        return 100.0 * self.correct / self.evaluated

    @property
    def errors(self) -> list[RowEval]:
        return [r for r in self.rows if not r.is_correct]


# ---------------------------------------------------------------------------
# URL normalization + matching
# ---------------------------------------------------------------------------


def _split_ground_truth(cell: str) -> list[str]:
    """Column E often holds multiple URLs joined by newlines (and
    occasionally commas). Split, strip, drop empties — leave the
    rest exactly as written so caller-side normalization can apply
    the same rules it does to agent URLs."""
    if not cell:
        return []
    parts: list[str] = []
    for chunk in cell.replace("\r", "\n").split("\n"):
        for sub in chunk.split(","):
            s = sub.strip()
            if s:
                parts.append(s)
    return parts


def _normalize_url(url: str) -> str:
    """Reduce a URL to host+path for equality comparison.

    Drops scheme, port, query, fragment. Lowercases the host, strips a
    leading "www.", strips trailing slash on the path. Returns "" for
    falsy / unparseable input. Empty path becomes "/" before slash
    trimming, so `https://a.com` and `https://a.com/` collapse to the
    same key.
    """
    if not url:
        return ""
    raw = url.strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    try:
        parsed = urlparse(raw)
    except ValueError:
        return ""
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = parsed.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    return f"{host}{path}"


def _hosts_equal(a: str, b: str) -> bool:
    return _normalize_url(a).split("/", 1)[0] == _normalize_url(b).split("/", 1)[0]


def _agent_matches_ground_truth(agent_urls: list[str], ground_truth: str) -> bool:
    """True iff any agent URL matches any GT URL (column E can be
    multiline / comma-separated). Exact-normalized match wins outright;
    same-host match is the looser fallback — Indian-uni portals
    frequently expose multiple valid login paths on the same host."""
    gt_urls = _split_ground_truth(ground_truth)
    if not gt_urls:
        return False
    gt_norms = {_normalize_url(g) for g in gt_urls if g}
    agent_norms = {_normalize_url(u) for u in agent_urls if u}
    if gt_norms & agent_norms:
        return True
    for gt in gt_urls:
        for url in agent_urls:
            if _hosts_equal(url, gt):
                return True
    return False


# ---------------------------------------------------------------------------
# Per-OrgID log capture
# ---------------------------------------------------------------------------


class OrgIDLogCapture(logging.Handler):
    """Routes log records to a per-OrgID buffer based on `current_orgid`.

    Attach once at the root logger before processing begins; set
    `current_orgid` immediately before each row's pipeline call, and
    reset it to None after. All records emitted between set / reset
    accumulate into `by_orgid[orgid]` regardless of which logger
    emitted them. (Pattern detection later in this module greps
    these buffers for known failure signatures.)
    """

    def __init__(self) -> None:
        super().__init__()
        self.by_orgid: dict[str, list[str]] = defaultdict(list)
        self.current_orgid: str | None = None
        self.setFormatter(
            logging.Formatter("%(levelname)-7s %(name)s :: %(message)s")
        )

    def emit(self, record: logging.LogRecord) -> None:
        if self.current_orgid is None:
            return
        try:
            self.by_orgid[self.current_orgid].append(self.format(record))
        except Exception:  # noqa: BLE001 — never let logging break the eval
            pass

    def dump(self, orgid: str, dest: Path) -> None:
        lines = self.by_orgid.get(orgid, [])
        if not lines:
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# Discovery invocation
# ---------------------------------------------------------------------------


def _run_discovery_for_row(
    *,
    sheet_row: int,
    orgid: str,
    row: dict[str, Any],
    sheets: SheetsClient,
    state: StateStore,
    js_renderer: JSRenderer | None,
    config: Any,
) -> list[str]:
    """Re-run Stage A (discovery) + Stage C.1 (tc_finder) for a single
    row with cache busted, write the result to the Portals sheet, and
    return the resulting Portal URL list.

    Mirrors scripts/run_batch_discovery.py::_run_one without the
    cached-result short-circuit so each eval call exercises the current
    codebase, not the stale state.db entry.
    """
    ctx = PipelineContext(
        orgid=orgid, row=row,
        deps={
            "state": state,
            "js_renderer": js_renderer,
            "user_agent": config.user_agent,
            "http_timeout": config.http_timeout_seconds,
        },
    )

    state.mark_stage(orgid, "discovery", "in_progress")
    discovery_result = discovery.run(ctx)
    budget_tripped = bool(discovery_result.get("completed_with_timeout"))
    if not budget_tripped:
        state.save_result(orgid, "discovery", discovery_result)
    ctx.results["discovery"] = discovery_result

    portals = discovery_result.get("portals") or []
    if not portals:
        reason = discovery_result.get("reason") or "no portals found"
        if not budget_tripped:
            state.mark_final(
                orgid, status="failed_discovery", stage="discovery",
                error=str(reason),
            )
        return []

    state.mark_stage(orgid, "tc_finder", "in_progress")
    tc_finder_result = tc_finder.run(ctx)
    state.save_result(orgid, "tc_finder", tc_finder_result)
    ctx.results["tc_finder"] = tc_finder_result

    sorted_portals = sorted(
        (p for p in portals if (p.get("url") or "").strip()),
        key=_portal_sort_key,
    )
    portal_urls: list[str] = [p["url"] for p in sorted_portals]

    findings = tc_finder_result.get("tc_findings") or []
    tc_seen: set[str] = set()
    tc_ordered: list[str] = []
    for f in findings:
        tc_url = str(f.get("tc_url") or "").strip()
        if not tc_url:
            continue
        key = tc_url.lower().rstrip("/")
        if key not in tc_seen:
            tc_seen.add(key)
            tc_ordered.append(tc_url)

    university_name = (
        str(discovery_result.get("university_name") or "").strip()
        or str(row.get("SheerID University Name", "")).strip()
    )

    existing = sheets.read_portals_by_orgid(orgid)
    preserved_verdict = ""
    if existing:
        preserved_verdict = str(
            existing[0][1].get("Overall T&C Verdict", "")
        ).strip()

    new_row = {
        "OrgID": orgid,
        "University Name": university_name,
        "Portal URLs": "\n".join(portal_urls),
        "T&C URLs": "\n".join(tc_ordered),
        "Overall T&C Verdict": preserved_verdict,
    }
    values = [new_row.get(c, "") for c in SheetsClient.PORTALS_COLUMNS]
    if existing:
        sheets.update_portal_rows([(existing[0][0], values)])
    else:
        sheets.append_portal_rows([new_row])

    state.mark_stage(orgid, "tc_finder", "done")
    return portal_urls


# ---------------------------------------------------------------------------
# Pattern classification
# ---------------------------------------------------------------------------


_KNOWN_PLATFORM_HOSTS: tuple[str, ...] = tuple(KNOWN_SHARED_PLATFORM_PATTERNS.keys())
_AFFILIATING_HOSTS: tuple[str, ...] = tuple(AFFILIATING_UNIVERSITY_PORTALS.keys())


def _host_of(url: str) -> str:
    norm = _normalize_url(url)
    return norm.split("/", 1)[0] if norm else ""


def _host_matches_any(host: str, patterns: tuple[str, ...]) -> bool:
    if not host:
        return False
    for pat in patterns:
        if host == pat or host.endswith("." + pat):
            return True
    return False


def _classify_error(row: RowEval) -> tuple[str, str]:
    """Return (pattern_label, log_snippet).

    Order matters here. Each row's GT can list multiple URLs (column
    E is occasionally multiline); we classify based on the *union* of
    GT hosts so a single samarth/AKTU/GNDU host anywhere in the cell
    is enough to flag `shared_platform_missed`, not `wrong_domain`.
    """
    log_text = row.log_snippet
    gt_urls = _split_ground_truth(row.ground_truth)
    gt_hosts = {_host_of(g) for g in gt_urls if g}
    gt_hosts.discard("")
    agent_hosts = {_host_of(u) for u in row.agent_urls if u}
    agent_hosts.discard("")

    gt_on_known_platform = any(
        _host_matches_any(h, _KNOWN_PLATFORM_HOSTS) for h in gt_hosts
    )
    gt_on_affiliating = any(
        _host_matches_any(h, _AFFILIATING_HOSTS) for h in gt_hosts
    )
    agent_on_known_platform = any(
        _host_matches_any(h, _KNOWN_PLATFORM_HOSTS) for h in agent_hosts
    )

    if not row.agent_urls:
        pattern = "no_portals_found"
    elif (gt_on_known_platform or gt_on_affiliating) and not agent_on_known_platform:
        pattern = "shared_platform_missed"
    elif gt_hosts and not (gt_hosts & agent_hosts):
        pattern = "wrong_domain"
    elif gt_hosts & agent_hosts:
        pattern = "path_mismatch"
    else:
        pattern = "one_off"

    lt = log_text.lower()
    if pattern in {"wrong_domain", "one_off"}:
        if "admission" in lt and ("accepted" in lt or "rule-c" in lt):
            pattern = "admission_portal_accepted"
        elif "shortname" in lt and ("reject" in lt or "filtered" in lt):
            pattern = "shortname_mismatch"

    return pattern, log_text


def _extract_log_snippet(lines: list[str], max_lines: int = 25) -> str:
    """Pull the most diagnostic ~25 lines: anything mentioning a
    candidate URL, a validation rule, or an explicit rejection/accept."""
    if not lines:
        return ""
    keywords = (
        "rule-", "rule_", "candidate", "validate", "reject", "accept",
        "portal", "admission", "shortname", "blocklist", "shared",
        "discovery", "fallback", "force_accept", "samarth", "tenant",
    )
    interesting: list[str] = []
    rest: list[str] = []
    for line in lines:
        low = line.lower()
        if any(kw in low for kw in keywords):
            interesting.append(line)
        else:
            rest.append(line)
    pick = interesting[-max_lines:] if interesting else rest[-max_lines:]
    return "\n".join(pick)


# ---------------------------------------------------------------------------
# Fix prompt generation
# ---------------------------------------------------------------------------


_PATTERN_HINTS: dict[str, str] = {
    "no_portals_found": (
        "Stage A returned 0 portals for these OrgIDs. Inspect the per-row "
        "log to identify which validation rule killed every candidate. "
        "Likely fixes:\n"
        "  - Add an AFFILIATING_UNIVERSITY_PORTALS entry in agent/config.py "
        "    if the institution is affiliated to a central-ERP university\n"
        "    that's already mapped (AKTU, CCSU, LU, GNDU, ...).\n"
        "  - Add a per-OrgID `seed_urls` + `force_accept_seed_urls: true` "
        "    entry to domain_overrides.json if the URL is known but Stage A\n"
        "    can't surface it organically.\n"
        "  - Loosen an overly-tight validation rule in "
        "    agent/stages/discovery.py if the log shows the right URL\n"
        "    being rejected by a single rule."
    ),
    "wrong_domain": (
        "The agent picked a URL on a different host than ground truth. "
        "Likely the membership check (rule-A/B/host_belongs_to_org) is "
        "letting an unrelated host through. Inspect "
        "`agent/stages/discovery_rules.py::host_belongs_to_org` and "
        "consider tightening shortname matching for the affected OrgIDs, "
        "OR add the GT host to the OrgID's `extra_effective_domains` so "
        "the agent prefers it."
    ),
    "admission_portal_accepted": (
        "The agent accepted an admission portal where ground truth points "
        "at the enrolled-student login. Inspect "
        "`agent/stages/discovery_rules.py::is_admission_portal` and the "
        "`STRONG_ADMISSION_SIGNALS` / `URL_ADMISSION_PATH_KEYWORDS` lists "
        "in agent/config.py. Add the missing admission signal that would "
        "have flipped these candidates to rejected."
    ),
    "shared_platform_missed": (
        "Ground truth lives on a known shared platform (Samarth, "
        "DigitalUniversity, AKTU ERP, GNDU SLC, ...). The platform's "
        "tenant probe in agent/stages/discovery.py is not surfacing it "
        "for these OrgIDs. Either add the OrgID's state to the matching "
        "`AFFILIATING_UNIVERSITY_PORTALS` entry's `state_aliases`, or add "
        "a new entry for an affiliating university not yet listed."
    ),
    "shortname_mismatch": (
        "The agent's R6 shortname-in-domain rule rejected the correct "
        "host because the OrgID's auto-derived shortname doesn't match "
        "the GT host's leftmost label. Add an `exact_shortnames` entry "
        "in domain_overrides.json for these OrgIDs naming the host's "
        "actual subdomain prefix."
    ),
    "path_mismatch": (
        "Right host, wrong path. Verify whether the GT path is the "
        "canonical login surface; if so, set `canonical_path` on the "
        "shared-platform entry in `KNOWN_SHARED_PLATFORM_PATTERNS` or "
        "add a per-OrgID `seed_urls` override pointing at the exact "
        "path."
    ),
    "one_off": (
        "Idiosyncratic — no systemic fix obvious. Add a per-OrgID "
        "`seed_urls` + `force_accept_seed_urls: true` override in "
        "domain_overrides.json."
    ),
}


def _render_markdown(
    *,
    start: int,
    end: int,
    report: EvalReport,
    patterns: dict[str, list[RowEval]],
    skip_discovery: bool,
) -> str:
    lines: list[str] = []
    a = lines.append
    a(f"# Autotune Fix Prompt — Rows {start}-{end}\n")
    a(
        f"**Accuracy:** {report.accuracy:.1f}% "
        f"({report.correct}/{report.evaluated})  "
        f"**Errors:** {len(report.errors)}  "
        f"**Mode:** {'verify (skip-discovery)' if skip_discovery else 'full'}\n"
    )

    systemic = {k: v for k, v in patterns.items()
                if k != "one_off" and len(v) >= 2}
    one_offs = patterns.get("one_off", []) + [
        r for k, v in patterns.items()
        if k != "one_off" and len(v) < 2 for r in v
    ]

    if systemic:
        a("**Systemic patterns detected:**")
        for name in sorted(systemic, key=lambda k: -len(systemic[k])):
            a(f"  - `{name}` — {len(systemic[name])} row(s)")
        a("")
    if not systemic and not one_offs:
        a("All rows in range are correct. Nothing to fix.\n")
        return "\n".join(lines)

    if systemic:
        a("---\n")
        a("## Systemic Fixes\n")
        a(
            "For each pattern below, implement the suggested code change. "
            "Each section includes the affected OrgIDs and a per-row log "
            "snippet showing why discovery went wrong.\n"
        )
        for name in sorted(systemic, key=lambda k: -len(systemic[k])):
            bucket = systemic[name]
            a(f"### Pattern: `{name}` ({len(bucket)} rows)\n")
            a(_PATTERN_HINTS.get(name, "(no hint registered)") + "\n")
            a("**Affected rows:**\n")
            for r in bucket:
                gt_urls = _split_ground_truth(r.ground_truth)
                gt_str = (
                    ", ".join(f"`{u}`" for u in gt_urls)
                    if gt_urls else "_(empty)_"
                )
                a(
                    f"- row {r.sheet_row} — [{r.orgid}] {r.university_name}\n"
                    f"  - ground truth: {gt_str}\n"
                    f"  - agent output: "
                    + (
                        ", ".join(f"`{u}`" for u in r.agent_urls)
                        if r.agent_urls else "_(none)_"
                    )
                )
                if r.log_snippet:
                    a("  - log highlights:\n")
                    a("    ```")
                    for ln in r.log_snippet.splitlines()[:15]:
                        a(f"    {ln}")
                    a("    ```")
            a("")

    if one_offs:
        a("---\n")
        a("## One-off domain_overrides Suggestions\n")
        a(
            "These rows don't fit a systemic pattern. Add the following "
            "entries to `domain_overrides.json` (merge with any existing "
            "entry for the same OrgID; do not overwrite other keys):\n"
        )
        override_blob: dict[str, dict[str, Any]] = {}
        for r in one_offs:
            seed_urls = _split_ground_truth(r.ground_truth)
            if not seed_urls:
                continue
            override_blob[r.orgid] = {
                "seed_urls": seed_urls,
                "force_accept_seed_urls": True,
            }
        a("```json")
        a(json.dumps(override_blob, indent=2))
        a("```\n")
        a("**Affected rows:**\n")
        for r in one_offs:
            urls = _split_ground_truth(r.ground_truth)
            joined = ", ".join(f"`{u}`" for u in urls) if urls else "_(empty)_"
            a(
                f"- row {r.sheet_row} — [{r.orgid}] {r.university_name} "
                f"→ {joined}"
            )
        a("")

    a("---\n")
    a("## Instructions for Claude Code\n")
    a(
        "1. Read each systemic-pattern section above and implement the "
        "suggested fix in the indicated file. Prefer the smallest change "
        "that addresses ALL rows under that pattern.\n"
        "2. Add the one-off domain_overrides entries as a single merged "
        "JSON edit to `domain_overrides.json`.\n"
        "3. Do NOT add tests, docs, or refactor surrounding code unless "
        "the fix specifically requires it.\n"
        "4. Do NOT ask for clarification — implement the fixes verbatim.\n"
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Eval orchestration
# ---------------------------------------------------------------------------


def _build_report(
    *,
    in_range: list[tuple[int, dict[str, Any]]],
    agent_by_orgid: dict[str, list[str]],
    capture: OrgIDLogCapture | None,
    discovery_run: bool,
) -> EvalReport:
    rows: list[RowEval] = []
    correct = 0
    for sheet_row, row in in_range:
        orgid = SheetsClient.extract_orgid(row)
        if not orgid:
            continue
        gt = str(row.get(GROUND_TRUTH_COL, "")).strip()
        if not gt:
            continue
        uni = str(row.get("SheerID University Name", "")).strip()
        agent_urls = agent_by_orgid.get(orgid, [])
        ok = _agent_matches_ground_truth(agent_urls, gt)
        if ok:
            correct += 1
        re_row = RowEval(
            sheet_row=sheet_row,
            orgid=orgid,
            university_name=uni,
            ground_truth=gt,
            agent_urls=agent_urls,
            is_correct=ok,
        )
        if not ok and capture is not None:
            snippet = _extract_log_snippet(capture.by_orgid.get(orgid, []))
            re_row.log_snippet = snippet
            log_dest = EVAL_LOGS_DIR / f"{orgid}_row{sheet_row}.log"
            if discovery_run:
                capture.dump(orgid, log_dest)
                re_row.log_file = str(log_dest.relative_to(REPO_ROOT))
            pat, _ = _classify_error(re_row)
            re_row.pattern = pat
        rows.append(re_row)
    return EvalReport(correct=correct, evaluated=len(rows), rows=rows)


def _read_agent_outputs_from_sheet(
    sheets: SheetsClient, orgids: set[str],
) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {orgid: [] for orgid in orgids}
    portals = sheets.read_portals()
    for prow in portals:
        orgid = SheetsClient.extract_orgid(prow)
        if not orgid or orgid not in orgids:
            continue
        cell = str(prow.get("Portal URLs", "") or "")
        urls = [u.strip() for u in cell.splitlines() if u.strip()]
        out[orgid] = urls
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


@click.command()
@click.option("--start", type=int, required=True)
@click.option("--end", type=int, required=True)
@click.option(
    "--skip-discovery", "skip_discovery", is_flag=True,
    help="Read existing Portals-tab output instead of re-running discovery.",
)
@click.option(
    "--output", "output", type=click.Path(dir_okay=False), required=True,
    help="Path to write the fix-prompt markdown. Sidecar JSON goes to "
         "<output>.json (same basename, .json suffix).",
)
def main(start: int, end: int, skip_discovery: bool, output: str) -> None:
    if start > end:
        raise click.ClickException(
            f"--start ({start}) must be <= --end ({end})"
        )
    if start < 2:
        raise click.ClickException(
            "Row 2 is the first data row (row 1 is the header)."
        )

    config = load_config()
    _bootstrap.setup_logging(config.log_level)
    EVAL_LOGS_DIR.mkdir(parents=True, exist_ok=True)
    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sidecar_path = output_path.with_suffix(".json")

    sheets = SheetsClient.from_config(config)
    sheets.ensure_portals_header()
    all_rows = sheets.read_universities()

    if all_rows and GROUND_TRUTH_COL not in all_rows[0]:
        raise click.ClickException(
            f"Universities sheet has no column {GROUND_TRUTH_COL!r}. "
            f"Columns present: {list(all_rows[0].keys())}"
        )

    in_range: list[tuple[int, dict[str, Any]]] = [
        (i + 2, r) for i, r in enumerate(all_rows)
        if start <= (i + 2) <= end
    ]
    if not in_range:
        logger.warning("No rows in range %d-%d", start, end)

    capture: OrgIDLogCapture | None = None
    if not skip_discovery:
        capture = OrgIDLogCapture()
        capture.setLevel(logging.DEBUG)
        logging.getLogger().addHandler(capture)

    js_renderer: JSRenderer | None = None
    try:
        if not skip_discovery:
            if config.enable_js_rendering:
                js_renderer = JSRenderer(
                    timeout_seconds=config.js_rendering_timeout_seconds,
                    user_agent=config.user_agent,
                )
            with StateStore(config.state_db_path) as state:
                for sheet_row, row in in_range:
                    orgid = SheetsClient.extract_orgid(row)
                    if not orgid:
                        logger.warning(
                            "[sheet_row=%d] no OrgID — skipping", sheet_row,
                        )
                        continue
                    uni = str(row.get("SheerID University Name", "")).strip()
                    logger.info(
                        "[sheet_row=%d] [%s] %s → running discovery",
                        sheet_row, orgid, uni,
                    )
                    capture.current_orgid = orgid
                    try:
                        _run_discovery_for_row(
                            sheet_row=sheet_row, orgid=orgid, row=row,
                            sheets=sheets, state=state,
                            js_renderer=js_renderer, config=config,
                        )
                    except Exception:
                        logger.exception(
                            "[sheet_row=%d] [%s] discovery raised — recording "
                            "as eval error", sheet_row, orgid,
                        )
                    finally:
                        capture.current_orgid = None
    finally:
        if js_renderer is not None:
            js_renderer.close()
        if capture is not None:
            logging.getLogger().removeHandler(capture)

    in_range_orgids = {
        SheetsClient.extract_orgid(r) for _, r in in_range
        if SheetsClient.extract_orgid(r)
    }
    agent_by_orgid = _read_agent_outputs_from_sheet(sheets, in_range_orgids)

    report = _build_report(
        in_range=in_range,
        agent_by_orgid=agent_by_orgid,
        capture=capture,
        discovery_run=not skip_discovery,
    )

    patterns: dict[str, list[RowEval]] = defaultdict(list)
    for r in report.errors:
        patterns[r.pattern or "one_off"].append(r)
    patterns_detected = sorted(
        [k for k, v in patterns.items() if k != "one_off" and len(v) >= 2],
        key=lambda k: -len(patterns[k]),
    )

    md = _render_markdown(
        start=start, end=end, report=report, patterns=patterns,
        skip_discovery=skip_discovery,
    )
    output_path.write_text(md)
    logger.info("Wrote fix prompt → %s", output_path)

    sidecar = {
        "accuracy": round(report.accuracy, 2),
        "correct": report.correct,
        "evaluated": report.evaluated,
        "patterns_detected": patterns_detected,
        "errors": [
            {
                "sheet_row": r.sheet_row,
                "orgid": r.orgid,
                "university_name": r.university_name,
                "ground_truth": r.ground_truth,
                "agent_urls": r.agent_urls,
                "pattern": r.pattern,
                "log_file": r.log_file,
            }
            for r in report.errors
        ],
        "domain_overrides_proposed": sum(
            1 for r in report.errors
            if (r.pattern == "one_off" or
                (r.pattern and r.pattern not in patterns_detected))
        ),
    }
    sidecar_path.write_text(json.dumps(sidecar, indent=2))
    logger.info("Wrote sidecar → %s", sidecar_path)

    click.echo(
        f"Accuracy: {report.accuracy:.1f}% "
        f"({report.correct}/{report.evaluated}) — "
        f"{len(patterns_detected)} patterns detected"
    )


if __name__ == "__main__":
    main()

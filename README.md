# reclaim-portal-agent

CLI-based AI agent that, for each university row in a Google Sheet:

1. **Stage A** — Discovers all student-facing login portals (search + path probing + subdomain probing + JS-render fallback + validation).
2. **Stage B** — Confidence scoring (currently stubbed; Stage C runs against every Stage A portal).
3. **Stage C.1** — For each portal, locates a Terms & Conditions / Privacy / Disclaimer document (portal page → portal root → university root → curated path fallback).
4. **Stage C.2** — Analyses the T&C document with a deterministic keyword scan and returns a per-portal `Yes` / `Maybe` / `No` / `Yes (No T&C Found)` verdict.
5. **Stage D** — Aggregates all per-portal results for the OrgID into a **single Portals-tab row** and writes it back.

An orchestrator iterates OrgIDs, handles per-stage failures, and persists progress in SQLite so the run is resumable. When the Claude API or the Google Sheets API hits a rate limit, the orchestrator pauses and resumes after the backoff.

## Setup

```bash
# 1. Python 3.11+
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# One-time Chromium install for Playwright (JS-render fallback).
# Skip if you set ENABLE_JS_RENDERING=false in .env.
playwright install chromium

# 2. Configure
cp .env.example .env
# Edit .env: add ANTHROPIC_API_KEY, GOOGLE_SHEET_ID, etc.

# 3. Google OAuth
# `credentials.json` (OAuth client for a Desktop app) must be in the project root.
# First run will open a browser to consent and write token.json.
```

## Usage

```bash
# Process a single OrgID end-to-end
python scripts/run_single.py --orgid 5819165

# Process up to N un-done OrgIDs from the sheet
python scripts/run_batch.py --limit 20

# Process every remaining OrgID (stops on completion or on sustained rate-limit)
python scripts/run_background.py

# Re-process an OrgID that's already marked complete
python scripts/run_single.py --orgid 5819165 --force

# Re-run only Stage C (T&C finder + analyzer) for one OrgID and rewrite its row
python scripts/run_tnc_only.py --orgid 663848
python scripts/run_tnc_only.py --orgid 663848 --force   # bypass analyzer cache

# Re-run only Stage C across many OrgIDs (defaults to rows missing a verdict)
python scripts/run_tnc_batch.py --limit 20
python scripts/run_tnc_batch.py --limit 20 --force

# Inspect SQLite state — what's done, pending, failed
python scripts/inspect_state.py

# Wipe an OrgID from the Portals tab AND state.db (for true re-runs)
python scripts/purge_orgid.py --orgid 5819165

# Wipe the entire Portals tab (data rows + header reset) AND every state.db row
python scripts/clean_portals_sheet.py
```

### `--force`

All runners (`run_single`, `run_batch`, `run_background`, `run_tnc_only`, `run_tnc_batch`) accept `--force`. It re-processes OrgIDs even if `state.db` marks them as complete (`status=success`). Useful when you suspect portals were missed on an earlier run — e.g. after tightening a filter, extending `ALLOWED_FUNCTIONAL_LABELS`, or adding a new path probe. On the Stage-C-only runners, `--force` additionally bypasses the per-URL analyzer cache so updated keyword/scoring logic takes effect.

Stage D's write path is a **per-OrgID upsert** against the Portals tab:

* If a row exists for the OrgID, it is overwritten in place.
* If no row exists, a new one is appended.
* All per-portal data for the OrgID collapses into a single row (see *Sheet layout*). The previous "one row per portal" schema is gone.

When you want a true reset (e.g. an OrgID was removed from SheerID's sheet, or you want to drop all cached Stage A/C results), use `scripts/purge_orgid.py` (one OrgID) or `scripts/clean_portals_sheet.py` (entire tab + state.db).

## Layout

```
agent/               Core package
  config.py          .env loading and typed config (also: tunable budgets,
                     keyword sets, category ordering, blocklists, overrides)
  state.py           SQLite wrapper for resumable state + analyzer cache
  sheets_client.py   Google Sheets OAuth client (5-column Portals schema)
  anthropic_client.py Claude wrapper with retry + rate-limit handling
  pipeline.py        Runs the 5 stages for one OrgID (with caching)
  orchestrator.py    Main loop, batching, top-level rate-limit handling
  stages/
    discovery.py        Stage A entrypoint
    discovery_rules.py  Stage A — rule-based search/probe/validate
    discovery_claude.py Stage A — optional Claude fallback (off by default)
    js_renderer.py      Stage A — Playwright fallback for SPA portals
    confidence.py       Stage B (stub)
    tc_finder.py        Stage C.1 — locates a T&C document per portal
    tc_analyzer.py      Stage C.2 — keyword-only verdict + URL-keyed cache
    sheet_writer.py     Stage D — aggregates per-OrgID row + upserts
prompts/
  tc_analysis.md     Claude prompt (reserved for the Claude analyzer mode)
scripts/             CLI entry points (see "Usage")
tests/
```

## Sheet layout

The spreadsheet has two tabs:

* **Universities** — owned by SheerID. **Read-only.** The agent never writes to it, never adds columns, never touches rows. There is no method on `SheetsClient` that targets this tab for writes — the read-only guarantee is structural.
* **Portals** — the agent's output. **One row per OrgID**, five columns:

  | Column | Contents |
  |---|---|
  | `OrgID` | SheerID OrgID (string) |
  | `University Name` | Display name |
  | `Portal URLs` | Every discovered portal URL, joined by `\n` (Google Sheets renders the newline as an in-cell line break) |
  | `T&C URLs` | Unique T&C URLs across this OrgID's portals, joined by `\n` (case-insensitive + trailing-slash dedup) |
  | `Overall T&C Verdict` | Aggregate verdict: `Yes` / `Maybe` / `No` / `Yes (No T&C Found)` |

  Portals inside the cell are sorted by category (`Student Portal` → `LMS/Moodle` → `Examination` → `Library` → `Fee` → `Other`), then alphabetically by URL within each category. The aggregation rule for `Overall T&C Verdict` is majority vote across per-portal verdicts; ties prefer the worst (`No` > `Maybe` > `Yes`); when every portal has no T&C the suffix `(No T&C Found)` is preserved so downstream consumers can tell that case apart from "T&C found and permissive".

Per-portal debug fields (category, JS-rendered flag, discovery source, per-portal verdict, evidence, reasoning) deliberately do **not** make it to the sheet — they live in `state.db` only, where they're useful for filtering / debugging without cluttering SheerID's view. All per-OrgID run state (processed / pending / failed, last error, timestamps) also lives in SQLite (`state.db`); we deliberately do not mirror it to the sheet.

`SheetsClient.ensure_portals_header()` will write the canonical header to an empty tab and *extend* a header that's a strict prefix of the canonical one. If a non-prefix mismatch is detected (e.g. the legacy 12-column schema still sitting on the tab), it logs a warning and writes positionally — run `scripts/clean_portals_sheet.py` to migrate.

## Per-OrgID overrides: `domain_overrides.json`

Stage A's Filter 2 drops college/department-specific subdomains (e.g. `ioe.du.ac.in`, `pgdav.du.ac.in`) so that Stage A returns only university-wide portals. The allow-list used for this decision is strictly *functional* (`student`, `fee`, `exam`, `library`, …) — institution names never belong in it.

When a university has a legitimate non-campus institution that should be treated as central (e.g. DU's School of Open Learning at `sol.du.ac.in`), or a legitimate alternate root domain (e.g. `uod.ac.in` for Delhi University), list it under that OrgID in `domain_overrides.json`:

```json
{
  "5819165": {
    "extra_allowed_subdomains": ["sol", "ncweb", "duls", "web"],
    "extra_allowed_root_domains": ["uod.ac.in"],
    "note": "DU uses both du.ac.in and uod.ac.in. SOL/NCWEB/DULS are university-wide systems."
  }
}
```

- `extra_allowed_subdomains` — label-level allow-list. A subdomain with one of these labels passes Filter 2 even if the 3-6-char acronym heuristic would otherwise flag it.
- `extra_allowed_root_domains` — root-level permissive override. Any URL under these roots passes Filter 2 regardless of subdomain label (the root itself is trusted).

When to use this:
- A subdomain label is institution-specific (so it won't make it into the global allow-list) **but** serves a university-wide student population.
- You want the decision to be explicit, auditable, and scoped to one OrgID.

When *not* to use this:
- If the subdomain is a purely functional label (like `feeadmin`, `digilib`) — add it to `ALLOWED_FUNCTIONAL_LABELS` in `agent/stages/discovery_rules.py` instead, since it generalises across universities.

Changes to this file take effect on the next Stage A run. Cached Stage A results in `state.db` are not retroactively re-filtered — wipe the OrgID's cache (or `rm state.db`) to re-run discovery.

## Caching

Two caches live in `state.db`:

* **`stage_results`** — one row per `(orgid, stage)`. Each stage's output JSON is cached so a resumed run skips work it has already done. `sheet_writer` is deliberately *never* cached (re-runs must actually rewrite the sheet so the upsert is exercised).
* **`tc_analyzer_cache`** — keyed by *normalized T&C URL*. Many universities share a legal page (Samarth, MKCL DigitalUniversity, …); analysing the same URL once and reusing the verdict cuts redundant fetches. `--force` on the Stage-C runners bypasses this table.

## Terminology

Throughout the codebase the portal's Terms & Conditions document is referred to as **T&C** (module / variable prefix: `tc_`). Sheet columns use `T&C URLs` and `Overall T&C Verdict`.

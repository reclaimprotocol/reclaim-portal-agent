# reclaim-portal-agent

A CLI agent that finds the **student-login portal URL** for a university from
its name + website, and writes it back into a Google Sheet. It can also
analyze each portal's Terms & Conditions page and record a scraping-permission
verdict.

### What it covers

- **Discovery** — finds the real student login page using search (Gemini via
  OpenRouter → DuckDuckGo → Google), path/subdomain probing, known-platform
  tenant probing (Samarth, Digiicampus, Sumsraj, MPOnline, Knimbus, Core
  Campus, …), homepage crawling, and JS/SPA rendering for validation.
- **Filtering** — rejects things that aren't an enrolled-student login:
  admission/recruitment portals, CMS admin backends (`wp-login.php`), staff
  webmail, employee portals (`samarth.ac.in`), etc.
- **Two sheet workflows** — the SheerID "Universities" sheet, and the office
  "Indian Universities" consolidation sheet (one tab per state).
- **(Optional) T&C analysis** — fetches the portal's T&C page and records a
  Yes / Maybe / No scraping-permission verdict.

---

## 1. Setup

Requires **Python 3.11+**.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium          # needed for JS/SPA portal validation
```

Then run commands either with the venv active (`python scripts/…`) or directly
via `.venv/bin/python scripts/…`.

### Google credentials

The agent reads/writes Google Sheets via OAuth.

1. Put your OAuth client file at `./credentials.json` (Desktop-app type).
2. The first run opens a browser to authorize; the token is cached at the
   `GOOGLE_TOKEN_PATH` (default `token.json`). If the token expires/revokes,
   it auto re-authenticates on the next run.
3. The account must have read+write access to the target spreadsheet.

---

## 2. Configure

Copy `.env.example` to `.env` and fill it in.

| Variable | Required | Purpose |
|---|---|---|
| `GOOGLE_SHEET_ID` | **yes** | The SheerID Universities spreadsheet ID (used by all runners *except* `run_portal_sheet.py`, which is hardwired to the office sheet). |
| `GOOGLE_CREDENTIALS_PATH` | yes | OAuth client JSON path (default `credentials.json`). |
| `GOOGLE_TOKEN_PATH` | yes | Cached OAuth token path (default `token.json`). |
| `OPENROUTER_API_KEY` | for search | Enables Gemini-powered discovery search + name→domain recovery. Without it, discovery falls back to DuckDuckGo/Google only. |
| `UNIVERSITIES_TAB_NAME` / `PORTALS_TAB_NAME` | no | Tab names on the SheerID sheet (default `Universities` / `Portals`). |
| `ENABLE_JS_RENDERING` | no | Playwright SPA validation (default `true`). |
| `ANTHROPIC_API_KEY` | only for Claude fallback / autotune | — |

**Tuning (env, optional)** — raise these to "go deeper" on hard rows:
`TOTAL_DISCOVERY_BUDGET_SECONDS` (300), `JS_RENDER_BUDGET_SECONDS` (40),
`MAX_JS_RENDER_CANDIDATES` (20), `HTTP_TIMEOUT_SECONDS` (8).

---

## 3. Run

### A) Office "Indian Universities" sheet — `run_portal_sheet.py`

The main current workflow. Hardwired to the office consolidation sheet (one
tab per state). Reads each row's `University Name` + `Website`, finds the
student portal, and writes it into the in-place `Portals URL` column. Cannot
touch the SheerID sheet.

```bash
# first 10 rows of a state tab
python scripts/run_portal_sheet.py --tab "Karnataka" --start 1 --end 10

python scripts/run_portal_sheet.py --tab "Goa"                 # whole tab
python scripts/run_portal_sheet.py --tab "Bihar" --force       # redo filled rows
python scripts/run_portal_sheet.py --tab "Kerala" --dry-run    # preview, no writes
```

- `--start` / `--end` are 1-based **data** rows (row 1 = first row under the header).
- Idempotent: skips rows that already have a `Portals URL` (use `--force` to redo).
- Auto-creates the `Portals URL` column if the tab doesn't have one.
- Prints a sheet / tab / row banner at startup so you always know what's being hit.

**Tips for long sweeps:** run in ~10–15-row chunks under `caffeinate -i`
(keeps the laptop awake so the connection doesn't drop). If a batch dies,
re-read the tab and resume from the first blank row (`--start <blank>`). To dig
harder on empty rows, bump the env budgets above and re-run with `--force`.

### B) SheerID "Universities" sheet

```bash
# Portal + T&C URL discovery, rows 2..101 (literal sheet rows; row 2 = first data row)
python scripts/run_batch_discovery.py --start 2 --end 101
python scripts/run_single.py --orgid 664197 --force        # one university

# T&C verdicts
python scripts/run_batch_tnc_analysis.py --start 2 --end 1000 --blank-only
python scripts/run_tnc_only.py --orgid 664197 --use-sheet-urls
```

### C) Autotune (self-improving eval loop)

Runs the eval against ground-truth URLs (column E), pipes a fix prompt into the
Claude Code CLI to patch discovery rules, re-evals, and commits only if
accuracy improves. Requires the Claude Code CLI + populated ground truth.

```bash
python scripts/run_autotune.py --start 100 --end 120 --dry-run     # always preview first
python scripts/run_autotune.py --start 100 --end 120 --max-iterations 3
```

---

## 4. Scripts

| Script | Purpose | Key flags |
|---|---|---|
| `run_portal_sheet.py` | Office sheet: find + write portals in-place, per state tab | `--tab`, `--start`, `--end`, `--force`, `--dry-run` |
| `run_single.py` | Full pipeline, one university | `--orgid`, `--force` |
| `run_batch.py` | Full pipeline, next N pending | `--limit` |
| `run_batch_discovery.py` | Portal + T&C URL discovery only | `--start`, `--end`, `--force` |
| `run_batch_tnc_analysis.py` | T&C verdicts only | `--start`, `--end`, `--blank-only`, `--force` |
| `run_tnc_only.py` | T&C analysis for one OrgID | `--orgid`, `--use-sheet-urls`, `--blank-only` |
| `run_finetune_eval.py` | Compare agent output vs ground truth, emit fix prompt | `--start`, `--end`, `--skip-discovery`, `--output` |
| `run_autotune.py` | Self-improving eval loop | `--start`, `--end`, `--max-iterations`, `--dry-run`, `--no-commit` |
| `purge_orgid.py` | Wipe state.db + sheet row for one OrgID | `--orgid` |
| `inspect_state.py` | Dump cached state for one OrgID | `--orgid` |

> Row numbering differs by script: `run_portal_sheet.py` uses 1-based **data**
> rows; the SheerID runners use literal **sheet** rows (row 2 = first data row).

---

## 5. Per-university overrides — `domain_overrides.json`

Pin a known answer or hint discovery for a specific OrgID (takes effect on the
next run). Useful when a portal lives on a third-party platform that discovery
can't reach.

Common fields: `state`, `exact_shortnames` (tenant prefixes on shared
platforms), `extra_effective_domains`, `seed_urls`, `force_accept_seed_urls`
(bypass all validation), `blocked_urls`, `tc_domain`, `notes`.

```json
{
  "664197": {
    "state": "Punjab",
    "exact_shortnames": ["pup", "punjabiuniversity"],
    "seed_urls": ["https://punjabiuniversity.samarth.edu.in/index.php/site/login"],
    "force_accept_seed_urls": true
  }
}
```

---

## 6. Troubleshooting

- **Batch crashes mid-run** — the writer retries transient socket/DNS/OAuth
  errors, but if a run still dies, just re-read the tab and resume from the
  first blank row. Writes are idempotent.
- **Connection drops on long runs** — wrap the command in `caffeinate -i`.
- **`token.json` expired/revoked** — the next run auto re-authenticates in the
  browser; if a push or run can't authenticate non-interactively, run it
  yourself in a terminal.
- **Too many empty rows on hard tabs** — raise `TOTAL_DISCOVERY_BUDGET_SECONDS`
  / `JS_RENDER_BUDGET_SECONDS` / `MAX_JS_RENDER_CANDIDATES` and re-run `--force`.

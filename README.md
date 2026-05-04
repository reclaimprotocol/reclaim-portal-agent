# reclaim-portal-agent

CLI agent that discovers university student portals,
analyzes their T&C pages for scraping permissions, and
writes results to Google Sheets.

---

## Setup

### Requirements
- Python 3.11+
- OpenRouter API key (Gemini Pro)
- Google Sheets OAuth credentials (`credentials.json`)

### Install

```bash
git clone https://github.com/reclaimprotocol/reclaim-portal-agent
cd reclaim-portal-agent
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

### .env

```bash
OPENROUTER_API_KEY=sk-or-v1-...
OPENROUTER_MODEL=google/gemini-2.0-flash-001
GEMINI_SEARCH_ENABLED=true
GOOGLE_SHEET_ID=your_sheet_id
ENABLE_JS_RENDERING=true
ENABLE_CLAUDE_FALLBACK=false
TC_ANALYZER_MODE=keyword
```

### Google OAuth

Place `credentials.json` (OAuth Desktop app client) in the
project root. First run opens a browser to consent and
writes `token.json`.

---

## Usage

```bash
# Single university
python scripts/run_single.py --orgid 5819165

# Batch (N universities)
python scripts/run_batch.py --limit 20

# Re-run even if already complete
python scripts/run_single.py --orgid 5819165 --force

# Purge and re-run from scratch
python scripts/purge_orgid.py --orgid 5819165
python scripts/run_single.py --orgid 5819165
```

### T&C only

```bash
# Re-run T&C for one university
python scripts/run_tnc_only.py --orgid 663848 --force

# Use URLs already in sheet — skip discovery, just analyze
python scripts/run_tnc_only.py --orgid 663848 --use-sheet-urls

# Re-run all rows with blank verdict
python scripts/run_tnc_only.py --blank-only

# Re-run all rows matching a verdict
python scripts/run_tnc_only.py --verdict "Yes (No T&C Found)"
```

### Inspect state

```bash
python scripts/inspect_state.py
python scripts/inspect_state.py --orgid 5819165
```

---

## T&C Verdicts

| Verdict | Meaning |
|---|---|
| `Yes` | No scraping prohibition found |
| `No` | Explicit data mining / scraping prohibition |
| `Maybe` | Ambiguous — manual review needed |
| `Yes (No T&C Found)` | No T&C page found |

Aggregation: majority vote. Tiebreak: worst wins (No > Maybe > Yes).

---

## domain_overrides.json

Per-university config overrides. Takes effect on next run.

```json
{
  "5819165": {
    "seed_urls": ["https://portal.university.edu/login"],
    "force_accept_seed_urls": true,
    "extra_effective_domains": ["university2.edu"],
    "blocked_urls": ["https://wrong-portal.edu/login"],
    "tc_domain": "university.edu",
    "notes": "Uses two root domains"
  }
}
```

| Field | Purpose |
|---|---|
| `seed_urls` | Known portal URLs to inject |
| `force_accept_seed_urls` | Bypass all validation for seeds |
| `extra_effective_domains` | Additional domains to probe |
| `blocked_urls` | URLs to never accept |
| `tc_domain` | Override T&C discovery domain |
| `notes` | Human-readable notes |

---

## Debugging

| Log | Cause | Fix |
|---|---|---|
| `phase=search candidates=0` | Search failed | Check `OPENROUTER_API_KEY` |
| `body fails hard gate` | JS-rendered portal | Should auto-escalate to Playwright |
| `membership REJECTED` | Domain mismatch | Add to `domain_overrides.json` |
| `budget_tripped=True` | Run timed out | Check JS render queue in logs |

---

## Recent changes

- Gemini Pro (OpenRouter) as primary search — DDG as fallback
- Phase 3 Gemini subdomain expansion
- SPA detection + Playwright escalation for login paths
- js-render link-follow — follows portal links from rendered pages
- Wildcard DNS content fingerprint comparison
- `--use-sheet-urls` flag for `run_tnc_only.py`

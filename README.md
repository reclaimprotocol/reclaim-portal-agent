# reclaim-portal-agent

CLI agent that discovers student-login portals for Indian universities
listed in a Google Sheet, analyzes their T&C pages for scraping
permissions, and writes Portal URLs + T&C URLs + verdicts back to the
sheet. Includes a self-improving eval loop that drives Claude Code CLI
to patch its own discovery rules.

---

## Pipeline

| Phase | Name | What it does |
|---|---|---|
| 1 | Search | Gemini Pro (OpenRouter) → DDG → Google. Two neutral queries per uni. |
| 2 | Probes | Path probes (`/student`, `/portal`, …), Samarth tenant probes, shared-platform tenant probes (Sumsraj, Digiicampus, MPOnline). |
| 3 | Sibling walk | Homepage anchor walk; optional Gemini subdomain expansion. |
| **3.5** | **Affiliation probe** | State-driven probe of `AFFILIATING_UNIVERSITY_PORTALS` — queues the affiliating university's portal as a candidate. Routed through normal validation. |
| 4 | Same-host probes | `STUDENT_LOGIN_SAME_HOST_PROBES` against every surviving host. |
| 5 | Pre-validation filter | Cheap rejects: blocklist, IDN, admin paths, off-domain. |
| 6 | Validation | Parallel HTTP + Playwright SPA escalation. Rules A/B/C; wildcard-DNS canary check. |
| 7 | Consolidation | Strict per-OrgID membership re-check, dedup, score gate. |
| → | Retry cascade | When phase 7 returns 0: fresh DDG → broader Gemini → homepage crawl. |
| → | Affiliating fallback | Last-resort force-accept (skips `verify: True` entries). |

---

## Scripts

| Script | Purpose | Key flags |
|---|---|---|
| `run_single.py` | Full pipeline, one university | `--orgid`, `--force` |
| `run_batch.py` | Full pipeline, next N pending | `--limit` |
| `run_batch_discovery.py` | Phase A+C.1 only (Portal + T&C URLs) | `--start`, `--end`, `--force` |
| `run_batch_tnc_analysis.py` | Phase C.2 (verdicts) only | `--start`, `--end`, `--blank-only`, `--force` |
| `run_tnc_only.py` | T&C analysis for one OrgID | `--orgid`, `--use-sheet-urls`, `--blank-only` |
| `run_finetune_eval.py` | Compare agent vs. column-E ground truth, emit fix prompt | `--start`, `--end`, `--skip-discovery`, `--output` |
| `run_autotune.py` | Self-improving eval loop (see below) | `--start`, `--end`, `--max-iterations`, `--dry-run`, `--no-commit` |
| `purge_orgid.py` | Wipe state.db + sheet row | `--orgid` |
| `inspect_state.py` | Dump cached state | `--orgid` |

`--start` / `--end` are literal sheet row numbers (row 2 = first data row).

---

## Autotune (self-improving loop)

- Runs `run_finetune_eval.py` against rows `--start..--end`, gets accuracy + fix prompt.
- Pipes the prompt into Claude Code CLI; lets it implement the fixes.
- Re-runs the eval to verify.
- If accuracy gained ≥ `--min-improvement` (default 5 pts), commits. Otherwise reverts.
- Loops up to `--max-iterations` times (default 1).

```bash
python scripts/run_autotune.py --start 100 --end 120 --dry-run            # always do this first
python scripts/run_autotune.py --start 100 --end 120 --max-iterations 3
```

**Prereqs:** Claude Code CLI installed (`npm install -g @anthropic-ai/claude-code`), column E (ground-truth URLs) populated in the Universities sheet, `token.json` present.

---

## Known shared platforms

| Platform | Probed for every uni? | Notes |
|---|---|---|
| `samarth.edu.in` | Yes | `{shortname}.samarth.edu.in/index.php/site/login` |
| `digitaluniversity.ac` | No | Maharashtra state-DU; surfaced organically |
| `aktu.ac.in` | Yes (via affiliation) | `erp.aktu.ac.in` — 750+ UP colleges |
| `gndu.ac.in` | Yes (via affiliation) | `slc.gndu.ac.in` — Punjab/Haryana |
| `sumsraj.com` | Yes | `{shortname}{student\|portal\|examination}.sumsraj.com` |
| `digiicampus.com` | Yes | `{shortname}.digiicampus.com/V2/#/home` |
| `mponline.gov.in` | Yes | `{shortname}.mponline.gov.in` (MP state SIS) |
| `linways.com` | No | `{shortname}.linways.com` (Kerala / South India) |
| `edumarshal.com` | No | `app.edumarshal.com` or `{shortname}.edumarshal.com` |
| `bihar-ums.com` | No | Bihar state UMS |
| `campuspro.in` / `.com` | No | — |
| `emsi.live` | No | — |
| `moodle.live` | No | LMS |
| `knimbus.com` | No | Library |
| `myloft.xyz` | No | Library |
| `cognibot.in` | No | LMS |
| `campus365.io` | **No (known-only)** | Wildcard DNS — every subdomain resolves; never probed, only accepted when discovered organically |

---

## Blocklists

- **`samarth.ac.in`** — employee/admin portal (not student); rejected pre-fetch.
- **`/wp-login.php`, `/wp-admin`, `/admin/*`** — CMS admin backends.
- **`elms.*`, `career*.*`, `placement*.*`, `jobs.*`, `alumni.*`** — non-student-audience subdomains.
- **`edugrievance.com`** — grievance system, never a portal.
- Known per-tenant hosts: `nou/jpv/ppu/pu.bihar-ums.com`, `mituniversityindia.edu.in`.

---

## T&C verdicts

ERP-level baseline (overridden by per-uni page content when present):

| Platform | Verdict | Reason |
|---|---|---|
| Samarth | Yes (No T&C Found) | No T&C page exposed on the SPA |
| AKTU ERP | Yes (No T&C Found) | No T&C page exposed |
| GNDU SLC | Yes (No T&C Found) | No T&C page exposed |
| MPOnline | Maybe | Generic state-govt T&C; ambiguous wording on automated access |
| Bihar UMS | Yes (No T&C Found) | No T&C page exposed |
| Edumarshal / Digiicampus / Sumsraj | Yes (No T&C Found) | Multi-tenant ERP; no per-tenant T&C |

Aggregation across multiple T&C URLs: majority vote, tiebreak worst-wins (`No` > `Maybe` > `Yes`).

---

## domain_overrides.json

Per-OrgID config. Takes effect on the next run.

Fields: `state` (drives Phase 3.5 + state-platform check), `exact_shortnames` (tenant prefixes on shared platforms), `extra_effective_domains`, `extra_allowed_subdomains`, `extra_allowed_root_domains`, `seed_urls`, `force_accept_seed_urls` (bypass ALL validation), `blocked_urls`, `tc_domain`, `notes`.

```json
{
  "664197": {
    "state": "Punjab",
    "exact_shortnames": ["pup", "punjabiuniversity"],
    "extra_effective_domains": ["punjabiuniversity.ac.in"],
    "seed_urls": ["https://punjabiuniversity.samarth.edu.in/index.php/site/login"],
    "force_accept_seed_urls": true,
    "tc_domain": "punjabiuniversity.ac.in"
  }
}
```

---

## Commands cheatsheet

**Discovery**
```bash
python scripts/run_batch_discovery.py --start 2 --end 101
python scripts/run_batch_discovery.py --start 2 --end 101 --force
python scripts/run_single.py --orgid 664197 --force
```

**T&C**
```bash
python scripts/run_batch_tnc_analysis.py --start 2 --end 101
python scripts/run_batch_tnc_analysis.py --start 2 --end 1000 --blank-only
python scripts/run_tnc_only.py --orgid 664197 --use-sheet-urls
```

**Autotune**
```bash
python scripts/run_autotune.py --start 100 --end 120 --dry-run
python scripts/run_autotune.py --start 100 --end 120 --max-iterations 3
python scripts/run_finetune_eval.py --start 100 --end 120 --output fixes.md
```

**Single university**
```bash
python scripts/run_single.py --orgid 664197
python scripts/purge_orgid.py --orgid 664197
python scripts/inspect_state.py --orgid 664197
```

**Git**
```bash
git status
git log --oneline -20
git diff
```

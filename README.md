# reclaim-portal-agent

Finds the **student-login portal URL** for a university from its name + website,
then finds that portal's **Terms & Conditions / privacy page**, and writes both
back into Google Sheets. Output is exported as `orgId, portal url, tnc url` CSVs
for SheerID.

---

## 1. How it works

```
orchestrator.py → pipeline.py → stages/
config.py          platform tables, budgets, tuning (3.2k lines)
state.py + state.db SQLite cache — resumable, per-org, holds learned_patterns
sheets_client.py    OAuth Sheets I/O — the sheet is the database
proxy.py            residential proxy, exit country picked per-URL from the ccTLD
```

**Discovery** — `stages/discovery.py` + `discovery_rules.py`, or `magic.py` for
the condensed one-shot entry point the batch runners use. Strategies, run under a
time budget: search (Gemini via OpenRouter → DuckDuckGo → Google), path/subdomain
probing, known-platform tenant probing (Samarth, Digiicampus, TOTVS/RM, Ulife,
Jacad, MPOnline, Knimbus, Core Campus…), homepage crawling, then JS/SPA
validation. `regions.py` adds country packs activated by ccTLD.

**Filtering** — rejects things that aren't an enrolled-student login: admission /
applicant portals, CMS admin backends (`wp-login.php`), staff webmail, employee
portals, payment gateways, library catalogues (OPAC/Koha/Pergamum/eLibro),
IdP metadata URLs, publisher SSO (Elsevier/Springer/IOP), staging hosts
(`qa.`/`dev.`/`test.`/`uat.`), logout URLs.

**T&C** — `stages/tc_finder.py` + `tc_levels.py` walk a level cascade: portal host
→ linked university → parent domain → vendor, stopping at the first hit and
recording which level produced it. `tc_analyzer.py` renders the page and returns
a Yes / Maybe / No scraping-permission verdict.

**Self-improvement** — `run_finetune_eval.py` diffs output against ground truth
and emits a fix prompt; `run_autotune.py` pipes it to the Claude Code CLI, patches
discovery rules, re-evals, and commits only if accuracy improved.
`learned_patterns` in state.db feeds discovered subdomain conventions forward.

---

## 2. Setup

Requires **Python 3.11+**.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium          # needed for JS/SPA validation
```

### Google credentials

1. Put your OAuth client file at `./credentials.json` (Desktop-app type).
2. First run opens a browser; the token caches at `GOOGLE_TOKEN_PATH`.
3. The account needs read+write on the target spreadsheet.

> **The token expires every 7 days** while the OAuth consent screen is in
> *Testing* status, and long unattended runs die mid-batch when it does
> (`invalid_grant: Token has been expired or revoked`). Publish the consent
> screen to **In production** to stop this. Re-auth is interactive, so it must be
> run from a terminal you can see.

### Residential proxy — read this before trusting any verdict

Set `USE_PROXY=1` plus the `RESIDENTIAL_PROXY_*` values. The proxy is applied to
target-site fetches **and** Playwright renders, with the exit country derived
per-URL from the ccTLD. `.in` and bare gTLDs go direct.

**It fails closed, not open.** If the provider rejects our IP
(`401 Auth Failed (code: ip_blacklisted)`), every proxied fetch returns status 0
and the reviewer writes `unreachable (status 0, render failed) — remove` for a
portal that is perfectly alive. This has produced dozens of false removals.

```bash
# ALWAYS probe before a review / classify / verify run
.venv/bin/python -c "
import requests, agent.magic as M
print(requests.get('https://api.ipify.org', proxies=M._proxies('https://x.edu.br/'), timeout=25).text)"
# expect a foreign exit IP — NOT a 401
```

Whitelisting gotchas: the office IP is dynamic (it has moved across `/24`s), and
macOS prefers IPv6, so whitelist the IPv6 `/64` as well as a wide IPv4 range.

---

## 3. Configure

Copy `.env.example` to `.env`. Key variables:

| Variable | Purpose |
|---|---|
| `GOOGLE_SHEET_ID` | Target spreadsheet |
| `GOOGLE_CREDENTIALS_PATH` / `GOOGLE_TOKEN_PATH` | OAuth client + cached token |
| `USE_PROXY`, `RESIDENTIAL_PROXY_*` | Residential proxy (see above) |
| `OPENROUTER_API_KEY` | Gemini-powered discovery search |
| `ANTHROPIC_API_KEY` | Claude fallback / autotune |
| `ENABLE_JS_RENDERING` | Playwright SPA validation (default true) |
| `MAGIC_TNC` | `0` to skip T&C during discovery |
| `MAGIC_REVIEW_COLOR` | `1` to restore red row tinting (default: off) |

**Budgets** — raise to go deeper on hard rows: `TOTAL_DISCOVERY_BUDGET_SECONDS`,
`JS_RENDER_BUDGET_SECONDS`, `MAX_JS_RENDER_CANDIDATES`, `HTTP_TIMEOUT_SECONDS`,
`MAGIC_ROW_TIMEOUT`, `TNC_ROW_TIMEOUT`.

---

## 4. Running batches

Every long-running driver follows the same pattern, learned the hard way:

- **daemonized** via `scripts/_daemonize.py` (double-fork) so it survives the
  shell, plus `caffeinate -dimsu`
- **sharded** N ways, each shard with its **own** done-file — a single shared
  state file gets clobbered by concurrent writers
- shard on the **full** work list, *then* drop completed items; sharding a
  pre-filtered list shifts positions between attempts and processes items twice
- **pre-mark before fetching**, so a page that wedges the headless browser costs
  one row instead of stalling the run
- **staleness watchdog** — a dead Chromium makes `JSRenderer` spin below the
  interpreter where `SIGALRM` never lands; only a `SIGKILL` on a stale log
  recovers it (there is no `timeout(1)` on macOS, and `perl -e alarm` is
  swallowed by the script's own SIGALRM handler)
- **empty `--report-remaining` means "unknown", not "done"** — a DNS blip
  otherwise burns the whole retry budget in seconds

```bash
# discovery over a row range of a source tab
.venv/bin/python scripts/_daemonize.py run.log \
    bash scripts/_run_to_completion.sh scripts/_run_julybatch_9july_portals.py \
    --start-row 315 --count 70

# rolling review + prune over Portals TnC
STALL_SECS=300 MAX_ADVANCES=200 .venv/bin/python scripts/_daemonize.py review.log \
    bash scripts/_run_review_rolling.sh
```

---

## 5. Scripts

**Core pipeline**

| Script | Purpose |
|---|---|
| `run_portal_sheet.py` | Office sheet: find + write portals in-place, per state tab |
| `run_single.py` / `run_batch.py` | Full pipeline, one org / next N |
| `run_batch_discovery.py` | Portal + T&C discovery only |
| `run_batch_tnc_analysis.py` | T&C verdicts only |
| `run_autotune.py` | Self-improving eval loop |

**Review / prune**

| Script | Purpose |
|---|---|
| `_run_portals_review.py` | Review each portal row: real login? writes verdict + T&C |
| `_reverify_portals.py` | Re-verify portals; **header-driven**, safe after column moves |
| `_recheck_red_urls.py` | Re-check NOT-WORKING rows with a longer timeout |
| `_strict_login_check.py` | Strict test: real password field, or login-shaped + reachable |
| `_prune_dead_portals.py` | Delete rows the review condemned (snapshots first) |
| `_build_prune_window.py` | Scope the prune to a full reviewed range |
| `_dedupe_portals_tnc.py` | Remove exact (org, portal, T&C) duplicates |
| `_audit_tnc_ownership.py` | Flag T&Cs that belong to another institution / country |

**Export / organise**

| Script | Purpose |
|---|---|
| `_export_next_batch.py` | Next delivery CSV — excludes orgs already shipped |
| `_report_reported_urls.py` | Clean + dedupe student-reported URLs |
| `_sort_portals_tnc_by_country.py` / `_by_traffic.py` | Reorder the tab |
| `_reorder_portals_tnc.py` | Group by portal/T&C availability |

---

## 6. Hard-won rules

- **Uniqueness is per-org, not global.** "Have we ever seen this URL?" and "has
  *this org* ever had it?" are different questions. Group filtering discards
  valid finds for sibling orgs on a shared platform.
- **Resolve sheet columns by header, never by letter.** The Portals TnC layout
  has changed several times; hardcoded columns silently write verdicts into
  `Category`.
- **A form with 2+ inputs is not a login.** Any WordPress page has a search box —
  require a `type="password"` field, or a login-shaped URL that returns 2xx/3xx.
  A login-shaped URL returning **404** is not a portal (SPA platforms 404 on
  unknown paths).
- **Validate what the T&C waterfall returns.** Search will happily attach another
  university's policy; check the T&C domain against the org's own domains and
  country before accepting.
- **`unreachable (status 0)` usually means slow or geo-blocked, not dead.**
  Re-checking with a longer timeout and a country-correct exit flips a large
  share of them to working.
- **Never tint sheet rows** unless asked (`MAGIC_REVIEW_COLOR=1`). The verdict
  text is the signal.
- **Snapshot before every destructive write.** Every prune/dedupe/reorder script
  writes a timestamped JSON first.

---

## 7. Data hygiene — what must never reach GitHub

`.gitignore` covers all of this; the categories exist because these files carry
org data or credentials:

- **`.env`, `.env.local`, `token.json`, `credentials.json`** — never committed
  (verified: zero commits touch them). `.env.example` is a placeholder template
  and is tracked deliberately.
- **`*.csv`** — every export contains org data.
- **`*.log`, `*_boot.log`** — run logs contain portal URLs and org names.
- **`state.db`** and all `*.db`/`*.sqlite`.
- **Run artifacts** — `*_shard[0-9]*.json`, `*_snapshot*.json`, `*_before_*.json`,
  `*_results.json`, `*_verdicts.json`, `*_done.json`, and the per-workflow
  families (`disabled_*`, `reported*`, `red_*`, `strict_*`, `pending928_*`,
  `perorg_*`, …). All regenerable.
- **`scripts/idp_denylist.txt`**, `scripts/inactive_orgs.csv` — internal lists.

```bash
# before committing, confirm nothing sensitive is staged
git status --porcelain -uall | grep '^??'          # should show only scripts/ & docs
git ls-files | grep -E '\.(csv|log)$|^\.env'       # must be empty
```

> **Known history exposure.** 51 CSV data files and `scripts/idp_denylist.txt`
> were committed before these rules existed and were later untracked — they
> remain retrievable from the pushed history on GitHub. No credentials are in
> history. Purging them needs a history rewrite (`git filter-repo`) plus a
> force-push coordinated with everyone who has a clone; treat it as a deliberate
> decision, not a routine cleanup.

---

## 8. Per-university overrides — `domain_overrides.json`

Pin a known answer or hint discovery for a specific OrgID:

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

Fields: `state`, `exact_shortnames`, `extra_effective_domains`, `seed_urls`,
`force_accept_seed_urls`, `blocked_urls`, `tc_domain`, `notes`.

---

## 9. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Everything suddenly `unreachable (status 0)` | Proxy is blocked — probe it, whitelist the current IP, re-check the affected rows |
| `invalid_grant: Token has been expired or revoked` | 7-day OAuth token; re-auth interactively, publish the consent screen |
| Run burns 500 iterations in a minute | Network/DNS drop made `--report-remaining` return empty — the guard now sleeps instead |
| A pass hangs at high CPU with no Chromium alive | Wedged renderer; the watchdog SIGKILLs it and the pre-mark skips the offending row |
| Verdicts written into the wrong column | Script hardcodes column letters — use a header-driven one |
| Row counts disagree with what a script reported | Read ranges that overlap the stats block count those rows; trust the sheet, filtered by header |
| Long run dies overnight | `caffeinate -dimsu` doesn't hold off sleep on battery — keep the machine on AC |

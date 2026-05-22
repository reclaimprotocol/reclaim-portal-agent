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
writes `token.json`. The client auto-refreshes the token
silently when it has <10 min left; if the refresh token is
revoked, `token.json` is wiped and the browser flow re-runs
automatically — the script never crashes with `RefreshError`.

---

## Commands (quick reference)

| Command | Purpose |
|---|---|
| `scripts/run_single.py --orgid <id>` | Full pipeline for one university |
| `scripts/run_single.py --orgid <id> --force` | Re-run, bypass state cache |
| `scripts/run_batch.py --limit <N>` | Full pipeline for next N pending rows |
| `scripts/run_batch_discovery.py --start <r> --end <r>` | Phase A+C.1 only (Portal + T&C URLs) for sheet rows `r..r` |
| `scripts/run_batch_tnc_analysis.py --start <r> --end <r>` | Phase C.2 (verdicts) for rows that already have T&C URLs |
| `scripts/run_tnc_only.py --orgid <id>` | T&C only for one OrgID |
| `scripts/run_tnc_only.py --orgid <id> --use-sheet-urls` | Re-analyze the URLs already in the sheet |
| `scripts/run_tnc_only.py --blank-only` | Re-analyze every row with a blank verdict |
| `scripts/purge_orgid.py --orgid <id>` | Wipe state.db + sheet row for an OrgID |
| `scripts/inspect_state.py [--orgid <id>]` | Dump cached state |

`--start` / `--end` are literal Google Sheet row numbers
(row 1 = header, row 2 = first data row), not array
indices. `run_batch_tnc_analysis.py` additionally accepts
`--blank-only` (only fill empty verdicts) and `--force`
(re-analyze even if a verdict exists).

---

## Pipeline Architecture

Discovery (Stage A) runs 7 phases plus two retry tiers.
Every candidate URL — regardless of phase — flows through
Phase 5 → 6 → 7 before being written. The only exceptions
are operator-curated `force_accept_seed_urls` overrides,
which skip validation, and the affiliating-university
fallback, which fires only when consolidation returned 0
portals.

| Phase | Name | What it does |
|---|---|---|
| 1 | Search | Gemini Pro (OpenRouter) → DDG fallback → Google fallback. Two neutral queries (`<name> student login`, `<domain> student portal`). |
| 2 | Probes | Path probes (`/student`, `/portal`, …), subdomain probes (`student.<dom>`, `erp.<dom>`, …), Samarth tenant probes (`{shortname}.samarth.edu.in`), and `SHARED_PLATFORM_TENANT_PROBES` (Sumsraj, Digiicampus, MPOnline). |
| 3 | Sibling walk | Homepage anchor walk on the primary domain → portal-anchored URLs + admitted sibling hosts. Optional Gemini subdomain expansion when sibling-walk yielded <3 hosts. Optional deep homepage crawl on portal-shaped anchors. |
| **3.5** | **Affiliation probe** | **NEW.** Looks up `domain_overrides[orgid].state`, finds every entry in `AFFILIATING_UNIVERSITY_PORTALS` whose `state_aliases` match, and queues that affiliator's portal URL as a candidate. Wildcard-DNS hosts are skipped. Routed through normal validation — does NOT force-accept. |
| 4 | Same-host probes | For every host that survived earlier phases, probe `STUDENT_LOGIN_SAME_HOST_PROBES` (`/Login/Login/StudentLogin`, `/account/login`, `/itxlogin`, …). Filtered against wildcard-DNS roots. |
| 5 | Pre-validation filter | Cheap rejections before HTTP: blocklist, file extensions, IDN hosts, instance blocklist, non-production hosts, admission paths, admin paths, non-student labels, off-domain. |
| 6 | Validation | Parallel HTTP with hard gate (DNS resolves, status ∈ {200, 401, 403}, body >1000 chars, real form). Three accept rules: **A** static login form, **B** login signal in path/body, **C** known shared-platform host. SPA escalation to Playwright. Wildcard-DNS canary fingerprint check for rule-C accepts. Audience veto rejects admin/staff/non-student-subdomain pages. |
| 7 | Consolidation | Strict per-OrgID membership re-check (rules 1–5 from `host_belongs_to_org`). `(host, category)` dedup. Score gate drops anything below 0. |
| → | Retry cascade | Fires when phase 7 returned 0 portals: DDG fresh queries → Gemini broader prompt → homepage anchor crawl. Each retry runs through the full filter→validate→consolidate path. |
| → | Affiliating-university fallback | Last-resort force-accept of an `AFFILIATING_UNIVERSITY_PORTALS` entry when retry cascade also returned 0. **Skips entries with `verify: True`** — those are Phase 3.5 probe-only. |

---

## Known shared platforms

`KNOWN_SHARED_PLATFORM_PATTERNS` — hosts that pass
validation rule-C without needing a static login form
(many are JS-rendered SPAs). Tenants are usually
`{shortname}.<platform>`.

| Platform | Category | Tenant pattern / canonical path |
|---|---|---|
| `samarth.edu.in` | Student Portal | `{shortname}.samarth.edu.in/index.php/site/login` (probed) |
| `digitaluniversity.ac` / `.ac.in` | Student Portal | Maharashtra state-DU |
| `aktu.ac.in` | Student Portal | `erp.aktu.ac.in/` (centralized — 750+ UP affiliated colleges) |
| `gndu.ac.in` | Student Portal | `slc.gndu.ac.in/Integration/StudentArea/login.aspx` |
| `linways.com` | Student Portal | `{shortname}.linways.com` (Kerala / South India ERP) |
| `edumarshal.com` | Student Portal | `app.edumarshal.com` / `{shortname}.edumarshal.com` |
| `campuspro.in` / `.com` | Student Portal | `{shortname}.campuspro.*` |
| `emsi.live` | Student Portal | `{shortname}.emsi.live` |
| `moodle.live` | LMS/Moodle | `{shortname}.moodle.live` |
| `sumsraj.com` | Student Portal | `{shortname}{student\|portal\|examination}.sumsraj.com` (probed) |
| `digiicampus.com` | Student Portal | `{shortname}.digiicampus.com/V2/#/home` (probed) |
| `campus365.io` | Student Portal | `{shortname}.campus365.io/site/userlogin` (wildcard DNS — accepted only when discovered organically) |
| `mponline.gov.in` | Student Portal | `{shortname}.mponline.gov.in/` (probed; MP state SIS) |
| `bihar-ums.com` | Student Portal | `{shortname}.bihar-ums.com` (Bihar state UMS) |
| `knimbus.com` | Library | `{shortname}.knimbus.com/portal/v2/default/login` |
| `myloft.xyz` | Library | `{shortname}.myloft.xyz` |
| `cognibot.in` | LMS/Moodle | `{shortname}.cognibot.in` |

Tenant *probes* (URL synthesis at phase 2) are scoped to
the OrgID's own `shortname` and `acronym`. Dead tenants
404/timeout and drop during validation.

---

## Affiliating universities

`AFFILIATING_UNIVERSITY_PORTALS` — parent universities
whose centralized portals are queued in Phase 3.5 for any
OrgID whose `state` matches. Entries marked `verify: True`
are probe-only (no force-accept fallback). Verified
entries drive both Phase 3.5 AND the zero-portal
force-accept fallback.

**Verified (force-accept eligible)**

| Affiliator | State / region | Portal |
|---|---|---|
| `aktu.ac.in` | UP | `erp.aktu.ac.in` |
| `ccsuniversity.ac.in` | UP (Meerut, western) | `ccsuniversity.samarth.edu.in` |
| `abvmu.edu.in` | UP (medical) | `abvmu.samarth.edu.in` |
| `lkouniv.ac.in` | UP (Lucknow region) | `lu.samarth.edu.in` |
| `gndu.ac.in` | Punjab / Haryana | `slc.gndu.ac.in/.../login.aspx` |

**Probe-only (Phase 3.5; `verify: True`)**

| State | Affiliators |
|---|---|
| Uttar Pradesh | MJPRU Bareilly, VBSPU Jaunpur, DDU Gorakhpur, MGKVP Varanasi |
| Maharashtra | SPPU Pune, Mumbai U, RTMNU Nagpur, KBC NMU Jalgaon, BAMU Aurangabad, SRTMUN Nanded, YCMOU Nashik |
| Rajasthan | U Rajasthan Jaipur, MDSU Ajmer, MGSU Bikaner, JNVU Jodhpur |
| Tamil Nadu | Anna U Chennai, Bharathidasan Trichy, Bharathiar Coimbatore, MS U Tirunelveli |
| Karnataka | VTU Belagavi, Bangalore U, Karnatak U Dharwad |
| Andhra Pradesh | JNTU Anantapur, JNTU Kakinada, Andhra U Vizag |
| Telangana | Osmania Hyderabad, JNTU Hyderabad, Kakatiya Warangal |
| Gujarat | Gujarat U Ahmedabad, VNSGU Surat, SP U Vallabh Vidyanagar |
| Madhya Pradesh | RD U Jabalpur, Vikram U Ujjain, DAVV Indore |
| West Bengal | MAKAUT/WBUT, Calcutta U |
| Bihar | BRABU Muzaffarpur, LNMU Darbhanga, Magadh Bodh Gaya |
| Punjab | Punjabi U Patiala, IKG PTU Jalandhar |
| Haryana | MDU Rohtak, KUK Kurukshetra |
| Himachal Pradesh | HPU Shimla |
| Odisha | Utkal Bhubaneswar, Berhampur U |
| Kerala | U Kerala Trivandrum, CUSAT Kochi, MG U Kottayam |

To graduate a probe-only entry to force-accept, remove its
`verify: True` flag in `agent/config.py` after confirming
the URL works for a real affiliated college.

---

## Blocklists

| Constant | Purpose | Sample entries |
|---|---|---|
| `KNOWN_INSTANCE_BLOCKLIST` | Hosts rejected pre-fetch regardless of OrgID. | `samarth.ac.in` (Samarth employee/admin portal — never student), `pu.bihar-ums.com`, `ppu.bihar-ums.com`, `nou.bihar-ums.com`, `mituniversityindia.edu.in` |
| `NON_STUDENT_SUBDOMAIN_BLOCKLIST` | Subdomain labels that mean "not students" — rejected post-validation via audience veto. Rule-C tenants are exempt. | `career`, `careers`, `placement`, `placements`, `jobs`, `recruit`, `alumni`, `donate`, `shop`, `news`, `events`, `hostel`, `transport` |
| `ADMIN_URL_PATH_TOKENS` | CMS / Django / WP admin paths rejected pre-fetch. | `/wp-login.php`, `/wp-admin`, `/admin/`, `/admin/login`, `/administrator/`, `/cpanel`, `/adminpanel`, `/dashboard/login` |
| `EXTERNAL_DOMAIN_BLOCKLIST` | External services — never a university portal. Used during sibling-walk. | `facebook.com`, `youtube.com`, `gov.in`, `nic.in`, `nptel.ac.in`, `ugc.ac.in`, `digilocker.gov.in` |

---

## domain_overrides.json

Per-OrgID config. Takes effect on the next run; the agent
treats override-supplied URLs as authoritative seeds.

```json
{
  "664197": {
    "state": "Punjab",
    "exact_shortnames": ["pup", "punjabiuniversity"],
    "extra_effective_domains": ["punjabiuniversity.ac.in", "pupexamination.ac.in"],
    "extra_allowed_subdomains": ["distance"],
    "extra_allowed_root_domains": ["uod.ac.in"],
    "force_accept_seed_urls": true,
    "seed_urls": ["https://punjabiuniversity.samarth.edu.in/index.php/site/login"],
    "blocked_urls": ["https://wrong-portal.edu/login"],
    "tc_domain": "punjabiuniversity.ac.in",
    "notes": "Punjabi University Patiala. SheerID-listed pbi.ac.in is stale."
  }
}
```

| Field | Purpose |
|---|---|
| `state` | OrgID's state (free-form). Drives **Phase 3.5 affiliation probe** and the state-platform strict-membership check. |
| `exact_shortnames` | Tenant-subdomain prefixes accepted on shared/state platforms (Samarth, bihar-ums). Pins R3/R4 of the membership check. |
| `extra_effective_domains` | Additional university-owned domains. Treated as full peers of the primary — they get searches, probes, and validation allow-listing. |
| `extra_allowed_subdomains` | Functional subdomain labels (`distance`, `cdoe`, …) that pass the audience filter for this OrgID. |
| `extra_allowed_root_domains` | Additional root domains to keep on-domain during consolidation. |
| `seed_urls` | Known portal URLs to inject at phase 1. Validated like any other candidate. |
| `force_accept_seed_urls` | When `true`, every `seed_urls` entry bypasses ALL validation (DNS, HTTP, audience, membership). Reserved for portals the static heuristics can't verify (React SPAs, 403-to-crawler hosts). |
| `blocked_urls` | Exact-canonical URL blocklist. Compared post-canonicalisation so port/case/fragment differences don't bypass. |
| `tc_domain` | Pin T&C lookup to a specific domain even when discovery surfaced portals on a different one. |
| `notes` | Free-form. Document *why* this override exists. |

---

## T&C verdicts

| Verdict | Meaning |
|---|---|
| `Yes` | No scraping/data-mining prohibition found in the analyzed text |
| `No` | Explicit prohibition matched a **strong** keyword *with* a prohibition phrase nearby |
| `Maybe` | Moderate signal — ambiguous wording or keyword in a non-binding context; needs manual review |
| `Yes (No T&C Found)` | No T&C page found at the portal/university root |

Aggregation across multiple T&C URLs: majority vote.
Tiebreak — worst wins (`No` > `Maybe` > `Yes`).

The `keyword` analyzer (`TC_ANALYZER_MODE=keyword`,
default) scans for:

- **Strong prohibitive** — `scrape`/`scraping`, `crawl`,
  `robot`/`spider`, `data mining`, `harvest`,
  `automated tools/means/access`, `bypass technical`,
  `circumvent`, `reverse engineer`.
- **Moderate prohibitive** — `extract`, `archive`,
  `index`, `interfere`, `bulk download`, `systematic
  copy`, `unauthorized access`.
- A match becomes `No` only when a **prohibition phrase**
  (`shall not`, `may not`, `must not`, `prohibited`,
  `forbidden`, `without permission`, …) sits in the
  80-char window around it, AND no **context-negation**
  phrase (`shall not be liable`, `notification regarding`,
  `act, …`, `grievance redressal`) is in that same window.

Set `TC_ANALYZER_MODE=claude` to use the Anthropic-backed
analyzer instead (requires `ANTHROPIC_API_KEY`).

---

## Phased batch workflow

For large ranges (hundreds of rows) the two-phase split
keeps Sheets writes minimal and lets you run T&C analysis
independently of discovery:

```bash
# 1. Discover portals + find T&C URLs for sheet rows 2-101
python scripts/run_batch_discovery.py --start 2 --end 101

# 2. Analyze T&C verdicts for the same range
python scripts/run_batch_tnc_analysis.py --start 2 --end 101

# Re-analyze only rows with a blank verdict
python scripts/run_batch_tnc_analysis.py --start 2 --end 1000 --blank-only

# Force re-analysis (e.g. after updating analyzer keywords)
python scripts/run_batch_tnc_analysis.py --start 2 --end 101 --force
```

Phase 1 (`run_batch_discovery.py`) writes Portal URLs +
T&C URLs but preserves any existing verdict. Phase 2
(`run_batch_tnc_analysis.py`) fills in / updates the
verdict cell only — never touches Portal URLs.

---

## Debugging

| Log | Cause | Fix |
|---|---|---|
| `phase=search candidates=0` | Search failed | Check `OPENROUTER_API_KEY` |
| `body fails hard gate` | JS-rendered portal | Should auto-escalate to Playwright |
| `membership REJECTED` | Domain mismatch | Add to `domain_overrides.json` |
| `budget_tripped=True` | Run timed out | Check JS render queue in logs; raise `TOTAL_DISCOVERY_BUDGET_SECONDS` |
| `affiliation probe: added <url>` | Phase 3.5 fired | Expected when OrgID has a `state` override |
| `affiliating university fallback` | All other phases returned 0 | Verify the affiliator's portal is actually live |

---

## Recent changes

- **Phase 3.5 affiliation probe** — state-driven probing
  of `AFFILIATING_UNIVERSITY_PORTALS`. Adds parent-university
  Samarth tenants as ordinary validated candidates for any
  OrgID with a `state` override.
- **`AFFILIATING_UNIVERSITY_PORTALS` expanded** from 5 to
  ~45 entries spanning 16 states. New entries carry
  `verify: True` and are probe-only — they don't trigger
  the zero-portal force-accept fallback until graduated.
- **OAuth auto-refresh** — Sheets client refreshes the
  access token silently when <10 min remain and re-runs
  the browser flow automatically if the refresh token is
  revoked. Never crashes with `RefreshError`.
- **New batch scripts** — `run_batch_discovery.py` and
  `run_batch_tnc_analysis.py` split the pipeline so
  large ranges can be staged independently and T&C
  verdicts can be regenerated without re-running discovery.
- **GNDU affiliating-university fallback** for
  Punjab/Haryana colleges.
- **`samarth.ac.in` blocked** as employee/admin portal —
  only `samarth.edu.in` is treated as a student portal.
- **`campus365.io` wildcard-DNS fix** — removed from
  tenant probes; organically discovered URLs still
  validate via rule-C with wildcard-canary fingerprint
  rejection.
- **Zero-portal retry cascade** — DDG fresh queries →
  Gemini broader prompt → homepage link crawl before
  accepting 0 portals.
- **Samarth admin-tenant filter (Option B)** — drops
  `*adm` Samarth tenants only when a peer non-adm tenant
  is also live.
- **Gemini Pro (OpenRouter) as primary search** with DDG
  fallback; Phase 3 Gemini subdomain expansion.
- **SPA detection + Playwright escalation** with
  per-host body-length tracking.
- **js-render link-follow** — follows portal anchors
  extracted from rendered DOMs.
- **Wildcard DNS content fingerprint comparison** —
  rejects fake-tenant accepts whose body matches the
  canary's fingerprint.
- **Edumarshal, Sumsraj, Digiicampus, MPOnline, Linways**
  added to `KNOWN_SHARED_PLATFORM_PATTERNS`.
- **`--use-sheet-urls` flag** for `run_tnc_only.py`.

# Genie-V3 as an HTTP service

CSV of organisations in, CSV of **newly discovered** portals out. No database.

```
your dashboard ──POST /runs (csv)──▶ service ──▶ runs/run_2026-09-19_a3f2c1/
                                        │              input.csv
                                        │              results.csv   ◀── appended per org
                 ◀──GET .../results.csv─┘              status.json
```

It imports one thing from the engine: `agent.lookup.find_portals`. Not
`genie_core`, not the V2 engine, and not `agent.config` — those last two are
imported *inside* `_fetch_rows_sync` ([v3_orchestrator.py:242](../agent/v3_orchestrator.py#L242)),
the Sheets reader, so a CSV-fed service never loads them. No `credentials.json`
on the server, and none of the 7-day OAuth expiry that kills long batches.

## Endpoints

| | |
|---|---|
| `GET /health` | open, no key |
| `POST /runs` | body **is** the CSV → `{job_id, total}` |
| `GET /runs` | recent runs, newest first |
| `GET /runs/{job_id}` | progress counters |
| `GET /runs/{job_id}/results.csv` | results — downloadable **mid-run** |

Auth: one shared secret in `X-API-Key`. Call it from your dashboard's
**backend** — a key in frontend JS is a public key.

**The service refuses to start if `AGENT_API_KEY` is unset.** It accepts uploads
and runs expensive browser jobs, so an unauthenticated deploy is a data leak and
a free compute pool. For local work set `AGENT_ALLOW_NO_AUTH=1` to opt in
deliberately.

```bash
curl -X POST https://<host>/runs -H 'X-API-Key: <key>' \
     -H 'Content-Type: text/csv' --data-binary @orgs.csv
# {"job_id":"run_2026-09-19_a3f2c1","total":412,"status":"queued"}

curl -H 'X-API-Key: <key>' https://<host>/runs/run_2026-09-19_a3f2c1
# {"status":"running","done":143,"total":412,"new_found":91,...}

curl -H 'X-API-Key: <key>' https://<host>/runs/run_2026-09-19_a3f2c1/results.csv -o out.csv
```

## Input CSV

| column | required | |
|---|---|---|
| `orgId` | yes | also `org id`, `organization id`, `id` |
| `website` | yes | also `website domain`, `email domains`, `domains`, `domain` |
| `current_portals` | no | portals you already have, `\|`-separated |
| `name` | no | improves model accuracy |
| `country` | no | picks the residential-proxy exit |

Headers resolve by **name, never position** — same rule as the Sheets reader,
because layouts change and a position-indexed reader silently reads the wrong
column. A UTF-8 BOM from Excel is handled.

## Output CSV

One row per org **always**, plus extra rows when an org has several new portals.

```
orgId,status,new_portal_url,category,system,tnc_url,tnc_reason,confidence,
match_basis,http_status,suppressed_as_known,dead_known_portals,notes,error
```

`status` is `new_found` · `none_found` · `failed`. **Absence of a row is never
the answer** — "crawled, found nothing new", "errored" and "never processed"
have to stay distinguishable, or a partial run reads as a complete one.

`tnc_reason` explains an empty `tnc_url`: `no_legal_links_found` (nothing was
harvested to score) vs `no_match_above_threshold` (candidates existed and were
rejected). Those need completely different follow-up, and a blank cell hides
which happened.

`notes` qualifies a row without failing it — currently only
`known_portals_truncated`, when more portals were supplied than
`AGENT_KNOWN_CHECK_MAX`. Without it an empty `dead_known_portals` would be
ambiguous between "none were dead" and "we did not check them all".

Rows are in **completion order, not input order** — that is the cost of results
being readable mid-run. Sort by `orgId` if you need input order.

## What "new" means

Suppression is at **host** level. If you already have `erp.x.edu/login` and the
crawl surfaces `erp.x.edu/student/login`, that is the same system by another
path — the extraction schema already collapses path variants on one host, and
reporting it as new costs a human review to conclude nothing. Whatever was
suppressed is listed in `suppressed_as_known`; nothing is dropped silently.

Switch to exact-URL suppression by comparing `_norm_url` instead of `host_of`
in [discovery.py](discovery.py) — one line.

## Dead known portals

The portals you send are also liveness-checked, and any that fail come back in
`dead_known_portals`. Roughly one HTTP request each, no crawl, no model — this
is the "mark previous portal urls as not correct" half of the workflow.

The guardrail counts **401/403/429 as alive** — a WAF refusing us still proves a
server is there — so protected portals are not falsely reported dead. Disable
with `AGENT_CHECK_KNOWN_PORTALS=0`.

## Local

```bash
pip install -r requirements.txt && playwright install chromium
export AGENT_API_KEY=dev OPENROUTER_API_KEY=... SERPER_API_KEY=...
# or, to run with no auth at all: export AGENT_ALLOW_NO_AUTH=1
uvicorn service.api:app --port 8800
```

## Deploy

One service, one disk. Everything mutable must live on the mount:

| | |
|---|---|
| `AGENT_RUNS_DIR` | `/data/runs` |
| `GENIE_TNC_MEMORY` | `/data/tnc_memory.json` |
| `GENIE_DOMAIN_HISTORY` | `/data/domain_history.json` |
| `GENIE_BLOCK_FILE` | `/data/infrastructure_block.json` |

Three things are load-bearing:

**~2 GB minimum.** `find_portals` renders every homepage in headless Chromium,
and — unlike the batch orchestrator — has *no* internal browser semaphore.
`AGENT_CONCURRENCY` (default 4) is the only thing bounding the pool. 512 MB
OOMs on the first batch, and swap presents as network timeouts, i.e. as live
portals recorded dead.

**Seed the memory files** onto the disk on first boot. An empty
`tnc_memory.json` is not a neutral start: it discards 166 vendor→legal
mappings, and that cache is keyed by vendor, so one lost entry costs a crawl
for every institution on the platform — 542 share `samarth.edu.in`.

**Turn autoDeploy off.** With a persistent disk there is no zero-downtime
deploy, so a merge to `main` kills a running batch. Interrupted runs are marked
`interrupted` on the next boot and their partial `results.csv` stays valid and
downloadable, but the remaining orgs are not resumed.

### Environment

| | |
|---|---|
| `AGENT_API_KEY` | shared secret for `X-API-Key`. Required — the service will not start without it |
| `AGENT_ALLOW_NO_AUTH` | `1` permits starting with no key (local dev only) |
| `AGENT_CONCURRENCY` | browsers in flight, default 4 |
| `AGENT_RUNS_DIR` | where run directories live |
| `AGENT_RUN_RETENTION_DAYS` | purged on boot, default 90 |
| `AGENT_MAX_ORGS_PER_RUN` | default 5000 |
| `AGENT_MAX_UPLOAD_BYTES` | default 8 MB |
| `AGENT_CHECK_KNOWN_PORTALS` | `0` skips the dead-portal sweep |
| `AGENT_KNOWN_CHECK_CONCURRENCY` | probes in flight per org, default 8 |
| `AGENT_KNOWN_CHECK_MAX` | portals checked per org, default 50; excess is reported in `notes` |
| `AGENT_CORS_ORIGINS` | only if a browser calls this directly |
| `OPENROUTER_API_KEY`, `SERPER_API_KEY` | required by the engine |
| `USE_PROXY` | `1` enables the residential proxy — see below |
| `PROXY_HOME_CC` | the country this host egresses from; `us` on Render, default `in` |
| `RESIDENTIAL_PROXY_GATEWAY` | `host:port`, **no scheme** |
| `RESIDENTIAL_PROXY_USER`, `RESIDENTIAL_PROXY_PASS` | provider credentials |

## The proxy is not optional on a hosted deploy

Measured on this service: `buet.ac.bd` returned **no portals** from the US, and
the correct BIIS portal plus its privacy policy through a Bangladesh exit. The
crawl did not fail in the first case — it fetched a *different* page, with
enough links to pass the filter and none of them the portal. The output said
`no portals identified`, which is indistinguishable from a genuine miss.

Two configuration mistakes cause this, and both are silent:

* **`PROXY_HOME_CC` left at its default.** Domains in the home country skip the
  proxy, because a direct connection is already local. The default is `in`, so
  on a US host every `.ac.in` and `.edu.in` university — the largest slice of
  the org list, 542 sharing `samarth.edu.in` alone — is fetched direct from the
  wrong continent, and the log says `via direct` as though that were intended.
* **A scheme in the gateway.** `http://host:port` becomes
  `http://user:pass@http://host:port` and every fetch fails.

`GET /health` reports both:

```json
"proxy": {"enabled": true, "configured": true, "gateway_has_scheme": false,
          "home_country": "us", "india_routed_via_proxy": true}
```

`india_routed_via_proxy: false` on a non-Indian host means the largest part of
your list is being fetched from the wrong place.

The proxy **fails closed**: an unwhitelisted egress IP returns 401 and every
portal is recorded dead. Whitelist the host's outbound IPs with the provider
before enabling it. The crawler retries once directly when a proxied fetch
fails, and logs which exit was at fault; the liveness guardrail has no such
fallback.

## Not done yet

- **No resume.** An interrupted run keeps its partial CSV but does not continue;
  re-upload the orgs that are missing. V3's own `domain_history` shield makes a
  re-run cheaper than the first.
- **No retry** for an org that failed.
- **`tnc_reason` is two cases, not four.** Splitting `no_match_above_threshold`
  into foreign-domain-veto vs vendor-terms-cap needs the matcher's `last_edges`
  threaded out of `find_portals`.
- **Runs execute one at a time**, queued. Deliberate — two concurrent batches
  double the Chromium pool.

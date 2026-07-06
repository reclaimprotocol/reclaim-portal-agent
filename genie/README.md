# Genie 🧞 — student login portal finder

A UI (and, later, MCP) front-end over the Reclaim portal-discovery agent.

- **Feature 1 — Search:** type a university → portals we already have (from the DB).
- **Feature 2 — Discover:** paste a college website → the agent finds its ERP / LMS /
  library / Moodle / student portals live (streamed), with an optional
  "also find the affiliating university's portals" checkbox.

## Architecture — core library + transport adapters

```
Next.js UI  ─┐
MCP (later) ─┼─▶ FastAPI (/search, /discover→/stream SSE) ─▶ genie_core ─┬─▶ Portals DB (SQLite)
CLI (later) ─┘                                                          └─▶ Reclaim discovery agent
                                                        ETL: Google Sheets + SheerID Universities ─┘
```
`genie_core` holds the only logic (`search_portals`, `discover_portals`). Every
surface is a thin adapter, so adding MCP later is ~50 lines.

## Layout
```
genie/
  core/genie_core/   models.py · db.py (SQLite repo) · search.py · discover.py (wraps agent)
  api/main.py        FastAPI: /health /search /discover /stream/{job}
  web/               Next.js app (reclaim theme): Search + Discover pages
  etl_seed.py        Google Sheets → SQLite portals DB
  genie.db           seeded SQLite (gitignore-able)
```

## Run (local MVP)

1. **Seed the DB** (once / on refresh):
   ```bash
   .venv/bin/python genie/etl_seed.py
   ```
2. **Start the API** (port 8799):
   ```bash
   .venv/bin/uvicorn genie.api.main:app --port 8799 --reload
   ```
3. **Start the UI** (port 3000):
   ```bash
   cd genie/web && npm install && npm run dev
   ```
   Open http://localhost:3000. (Point the UI at a non-default API with
   `NEXT_PUBLIC_GENIE_API`.)

## Notes / next steps
- SQLite is behind a small repo interface in `db.py` → swap to Postgres for prod.
- No auth yet (MVP). Add API-key middleware + rate limits before public launch
  (each live discovery makes billable Gemini/API calls).
- MCP: add `genie/mcp/` exposing `search_portals` + `discover_portals` as tools.

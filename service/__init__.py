"""Genie-V3 HTTP service — CSV of organisations in, CSV of new portals out.

    runs.py       run store; the filesystem IS the database
    csvio.py      the input/output CSV contract
    discovery.py  one org: discover, subtract what the caller already has
    api.py        FastAPI surface + the in-process background runner

It imports exactly ONE thing from the agent: `agent.lookup.find_portals`. Not
the Sheets reader, not agent.config, not genie_core, not the V2 engine. See
service/README.md for why that matters operationally.
"""

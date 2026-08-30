"""Genie-V2 legacy engine, isolated from the V3 modules.

Moved here 2026-08-31 purely to make `agent/` readable: V3 is ~2,800 lines and
was buried under ~20,000 lines of V2. NOTHING here is deprecated — `genie/` runs
in production against it, 111 scripts import it, and 3,114 August4000 rows still
need its T&C pass.

Every original import path still works. `agent/magic.py`, `agent/pipeline.py`
etc. remain as shims that alias the real module into `sys.modules`, so
`agent.magic is agent.legacy.magic` — one module object, one set of module-level
state. That aliasing matters: a shim that merely re-exported names would create
a SECOND module instance, and the caches and locks held at module level in
discovery.py/magic.py would silently diverge between the two.
"""

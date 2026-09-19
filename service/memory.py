"""Genie-V3 service · seed the L5 memory files onto the persistent disk.

In production `GENIE_TNC_MEMORY`, `GENIE_DOMAIN_HISTORY` and `GENIE_BLOCK_FILE`
point at a mounted volume so the agent's memory survives a redeploy. A fresh
volume is empty, and an empty memory file is NOT a neutral starting point:

  * `tnc_memory.json` ships 166 vendor->legal mappings, keyed by VENDOR, so one
    discarded entry costs a crawl for every institution on that platform — 542
    of them share `samarth.edu.in`.
  * `domain_history.json` ships 179 resolved orgs and is the resume shield.
  * `infrastructure_block.json` ships 104 firewalled hosts we already know not
    to retry.

`MemoryCache` creates an empty file when the path is missing, so losing these
is silent: the run still works, it is just slower and re-learns what it knew.

Only ever seeds a MISSING file. A disk that has been running is newer than
anything in git and must never be overwritten.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger("genie.service.memory")

ROOT = Path(__file__).resolve().parents[1]

#: env var -> the git-tracked file it is seeded from.
MEMORY_FILES = {
    "GENIE_TNC_MEMORY": "tnc_memory.json",
    "GENIE_DOMAIN_HISTORY": "domain_history.json",
    "GENIE_BLOCK_FILE": "infrastructure_block.json",
}


def seed_memory_files() -> list[str]:
    """Copy the repo's memory onto the disk for any file not already there.

    MUST run before the engine is first imported: `agent.memory_cache` resolves
    these paths at module import time, so seeding afterwards has no effect on
    an already-imported module. `service.api` only imports the engine lazily,
    inside a request, which is what makes the ordering safe.
    """
    seeded: list[str] = []
    for env_key, filename in MEMORY_FILES.items():
        target = (os.getenv(env_key) or "").strip()
        if not target:
            continue                      # using the in-repo default already
        dest, src = Path(target), ROOT / filename
        if dest.exists() or not src.exists():
            continue
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(src.read_bytes())
            seeded.append(f"{filename} -> {dest}")
            logger.info("seeded %s -> %s (%d bytes)", filename, dest,
                        dest.stat().st_size)
        except OSError as exc:
            logger.warning("could not seed %s: %s — the run will still work, "
                           "it will just re-learn what it already knew", dest, exc)
    return seeded

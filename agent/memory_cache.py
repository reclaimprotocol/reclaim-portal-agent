"""Genie-V3 · Layer 5 — dynamic pattern caching and memory compounding.

`Layer5MemoryCacheManager` is the vendor-signature cache: it learns a SaaS
platform's legal pages once and answers for every university on that platform
thereafter. 542 institutions share `samarth.edu.in`; that lookup should happen
once, not 542 times.

`MemoryCache` wraps it and adds the two other Pre-Crawl Shield arms — prior
resolution (`domain_history.json`) and firewall blocks
(`infrastructure_block.json`) — which is what `v3_orchestrator` consumes.

FILE LOCATION — DELIBERATE DEVIATION
------------------------------------
The brief specifies `agent/tnc_memory.json`. The live file is at the REPOSITORY
ROOT: it was created there in an earlier step, is git-tracked via an explicit
`!tnc_memory.json` negation in .gitignore (the generic `tnc_*.json` rule would
otherwise swallow it), and is already read/written by the orchestrator. Writing
to `agent/` would silently start a second, empty cache while the populated one
sat unread — a bug that looks exactly like "the cache never hits".

`DEFAULT_MEMORY_PATH` therefore points at the root file, and the constructor
takes a path so it can be pointed anywhere. Set GENIE_TNC_MEMORY to override.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

logger = logging.getLogger("genie.memory")

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MEMORY_PATH = Path(os.getenv("GENIE_TNC_MEMORY", ROOT / "tnc_memory.json"))
DOMAIN_HISTORY_PATH = Path(os.getenv("GENIE_DOMAIN_HISTORY", ROOT / "domain_history.json"))
BLOCK_PATH = Path(os.getenv("GENIE_BLOCK_FILE", ROOT / "infrastructure_block.json"))

#: ccTLDs whose registrable root needs three labels, so `daotao.sgu.edu.vn`
#: reduces to `sgu.edu.vn` rather than the meaningless `edu.vn`.
_TWO_LABEL_TLDS = frozenset({
    "ac.in", "co.in", "edu.in", "org.in", "net.in", "gov.in", "ac.uk", "co.uk",
    "com.br", "edu.br", "org.br", "gov.br", "com.au", "edu.au", "com.mx",
    "edu.mx", "com.ar", "edu.ar", "com.co", "edu.co", "ac.id", "sch.id",
    "edu.ph", "com.ph", "ac.lk", "edu.lk", "ac.bd", "edu.bd", "edu.pk",
    "edu.ng", "ac.ke", "ac.za", "edu.my", "edu.vn", "com.vn", "ac.vn",
    "ac.th", "edu.eg", "edu.sa", "com.pe", "edu.pe", "com.tr", "edu.tr",
    "edu.ua", "com.ua",
})


def signature(host_or_url: str) -> str:
    """Module-level shortcut for `Layer5MemoryCacheManager.extract_domain_signature`."""
    host = host_or_url or ""
    if "://" in host:
        host = urlsplit(host).netloc
    host = host.lower().split("@")[-1].split(":")[0].strip().strip(".")
    if host.startswith("www."):
        host = host[4:]
    parts = [p for p in host.split(".") if p]
    if len(parts) <= 2:
        return ".".join(parts)
    if ".".join(parts[-2:]) in _TWO_LABEL_TLDS:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _atomic_write(path: Path, data: dict[str, Any]) -> None:
    """Serialize via temp file + os.replace.

    A plain `open(path, "w")` truncates first: a crash or a concurrent reader
    mid-write leaves a half-written or empty cache, and this file is the only
    place our compounded vendor knowledge lives. os.replace is atomic on POSIX.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class Layer5MemoryCacheManager:
    """Vendor-signature -> legal URLs, held in memory, persisted to JSON."""

    def __init__(self, memory_path: str | Path = DEFAULT_MEMORY_PATH) -> None:
        self.memory_path = Path(memory_path)
        # Guards the read-modify-write in check_cache/update_cache. The
        # orchestrator drives 20 concurrent workers, and two of them committing
        # different signatures at once would otherwise race and lose one.
        self._lock = threading.RLock()
        self.cache: dict[str, Any] = self._initialize()
        logger.info("memory: %d vendor signature(s) loaded from %s",
                    len(self.cache), self.memory_path)

    # ------------------------------------------------------------------ #
    def _initialize(self) -> dict[str, Any]:
        """Load the file, creating an empty `{}` one if it does not exist."""
        if not self.memory_path.exists():
            _atomic_write(self.memory_path, {})
            logger.info("memory: initialised empty cache at %s", self.memory_path)
            return {}
        try:
            data = json.loads(self.memory_path.read_text(encoding="utf-8") or "{}")
            return data if isinstance(data, dict) else {}
        except Exception as exc:  # noqa: BLE001
            # Never destroy an unreadable cache — move it aside so it can be
            # inspected, rather than overwriting evidence with {}.
            bak = self.memory_path.with_suffix(
                f".corrupt-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}.json")
            try:
                self.memory_path.rename(bak)
                logger.error("memory: %s unreadable (%s) — preserved as %s, "
                             "starting empty", self.memory_path.name,
                             type(exc).__name__, bak.name)
            except OSError:
                logger.error("memory: %s unreadable (%s) — starting empty",
                             self.memory_path.name, type(exc).__name__)
            _atomic_write(self.memory_path, {})
            return {}

    def _flush(self) -> None:
        _atomic_write(self.memory_path, self.cache)

    # ------------------------------------------------------------------ #
    def extract_domain_signature(self, url: str) -> str:
        """Parent platform domain for `url`.

            https://xyz.samarth.edu.in/site/login  ->  samarth.edu.in
            https://uni.instructure.com/login      ->  instructure.com
            https://a.b.jacad.com.br/aluno         ->  jacad.com.br

        Two-label ccTLDs are handled explicitly: a naive "last two labels" rule
        turns every Indian tenant into `edu.in`, which would collapse unrelated
        platforms into one cache key and serve the wrong terms.
        """
        return signature(url)

    # ------------------------------------------------------------------ #
    def check_cache(self, portal_url: str) -> dict[str, Any] | None:
        """Cached legal URLs for this portal's platform, or None on a miss.

        Increments `hit_counter` and persists immediately, so the file doubles
        as a usage ledger showing which platforms actually pay for themselves.
        """
        sig = self.extract_domain_signature(portal_url)
        if not sig:
            return None
        with self._lock:
            entry = self.cache.get(sig)
            if not entry:
                return None
            if not (entry.get("tnc_url") or entry.get("privacy_policy_url")):
                return None
            entry["hit_counter"] = int(entry.get("hit_counter", 0)) + 1
            entry["last_hit_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            self._flush()
            result = dict(entry)
        logger.info("memory: CACHE HIT %s (hit_counter=%d)", sig, result["hit_counter"])
        return result

    # ------------------------------------------------------------------ #
    def update_cache(self, portal_url: str, platform_name: str,
                     tnc_url: str | None, privacy_url: str | None) -> bool:
        """Commit a signature -> legal-links pattern. True if anything changed.

        Requires at least one legal URL: an entry with neither would be a
        permanent negative cached against the whole platform, so every future
        tenant would inherit "no terms" without anyone re-checking.
        """
        sig = self.extract_domain_signature(portal_url)
        if not sig or not (tnc_url or privacy_url):
            return False
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._lock:
            entry = self.cache.get(sig)
            if entry is None:
                entry = {"domain_signature": sig, "platform_name": platform_name or "",
                         "tnc_url": tnc_url or None,
                         "privacy_policy_url": privacy_url or None,
                         "hit_counter": 1, "first_seen": now, "last_updated": now}
                logger.info("memory: NEW pattern %s -> %s", sig,
                            tnc_url or privacy_url)
            else:
                # Fill gaps but never overwrite a known-good URL with nothing.
                entry["tnc_url"] = tnc_url or entry.get("tnc_url")
                entry["privacy_policy_url"] = privacy_url or entry.get("privacy_policy_url")
                entry["platform_name"] = entry.get("platform_name") or platform_name or ""
                entry["last_updated"] = now
            self.cache[sig] = entry
            self._flush()
        return True

    # ------------------------------------------------------------------ #
    def stats(self) -> dict[str, int]:
        with self._lock:
            return {"signatures": len(self.cache),
                    "total_hits": sum(int(v.get("hit_counter", 0))
                                      for v in self.cache.values())}


# --------------------------------------------------------------------------- #
#  Pre-Crawl Shield (history + firewall blocks) — consumed by v3_orchestrator  #
# --------------------------------------------------------------------------- #
@dataclass
class ShieldHit:
    kind: str                    # "history" | "blocked"
    reason: str
    record: dict[str, Any]

    @property
    def skip_crawl(self) -> bool:
        return True


def _read(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8") or "{}")
    except FileNotFoundError:
        return {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("memory: unreadable %s (%s) — treating as empty",
                       path.name, type(exc).__name__)
        return {}


class MemoryCache:
    """Pre-Crawl Shield + the Layer-5 vendor cache.

    The shield asks three questions in order — prior resolution, firewall block,
    then (post-inference) vendor signature. Only the first two can run BEFORE a
    crawl: pre-crawl we know the university's domain but not its portal's, so a
    vendor lookup has nothing to key on until the cascade has produced a URL.
    """

    def __init__(self, *, retry_blocked: bool = False,
                 memory_path: str | Path = DEFAULT_MEMORY_PATH) -> None:
        self.retry_blocked = retry_blocked
        self.layer5 = Layer5MemoryCacheManager(memory_path)
        self.history = _read(DOMAIN_HISTORY_PATH)
        self.blocks = _read(BLOCK_PATH)
        self._hist_lock = asyncio.Lock()
        self.stats = {"history_hits": 0, "blocked_skips": 0,
                      "vendor_hits": 0, "misses": 0}
        logger.info("memory: %d orgs in history, %d vendor signatures, %d blocked hosts",
                    len(self.history), len(self.layer5.cache), len(self.blocks))

    @property
    def tnc(self) -> dict[str, Any]:
        return self.layer5.cache

    def pre_crawl_check(self, org_id: str, domain: str) -> ShieldHit | None:
        rec = self.history.get(str(org_id))
        if rec and rec.get("portals"):
            self.stats["history_hits"] += 1
            return ShieldHit("history",
                             f"already resolved ({len(rec['portals'])} portal(s))", rec)

        sig = signature(domain)
        entry = self.blocks.get(sig) or self.blocks.get(
            (domain or "").lower().removeprefix("www."))
        if entry and not entry.get("resolved") and not self.retry_blocked:
            self.stats["blocked_skips"] += 1
            return ShieldHit("blocked",
                             f"{entry.get('block_type', 'blocked')} "
                             f"(seen {entry.get('attempts', 1)}x)", entry)

        self.stats["misses"] += 1
        return None

    def legal_for_portal(self, portal_url: str) -> dict[str, Any] | None:
        hit = self.layer5.check_cache(portal_url)
        if hit:
            self.stats["vendor_hits"] += 1
        return hit

    #: Only mappings at least this confident are compounded into the vendor
    #: cache. The waterfall's "not Stage 4" rule is gone with the waterfall; the
    #: graph's composite score is the replacement, and it is a better one — it
    #: is continuous and it states WHY. 0.60 admits sibling/vertical matches
    #: with a real compliance token (0.4*0.7 + 0.4*1.0 + 0.2*1.0 = 0.88) while
    #: excluding weak-signal edges that merely scraped past the 0.40 gate. A
    #: cache entry is inherited by every future tenant on that platform, so the
    #: bar to enter it is deliberately higher than the bar to be reported once.
    MEMORY_CONFIDENCE_MIN = 0.60

    async def remember_legal(self, entries: list[dict[str, Any]]) -> None:
        """Compound vendor knowledge, gated on graph confidence."""
        for e in entries or []:
            conf = float(e.get("graph_confidence") or 0.0)
            if conf < self.MEMORY_CONFIDENCE_MIN:
                continue
            self.layer5.update_cache(
                e.get("exact_url", ""), e.get("portal_system_name", ""),
                e.get("tnc_url"), e.get("privacy_policy_url"))

    async def remember_org(self, org_id: str, record: dict[str, Any]) -> None:
        async with self._hist_lock:
            hist = _read(DOMAIN_HISTORY_PATH)
            hist[str(org_id)] = {**record,
                                 "updated_at": datetime.now(timezone.utc)
                                 .isoformat(timespec="seconds")}
            _atomic_write(DOMAIN_HISTORY_PATH, hist)
            self.history = hist

    def summary(self) -> str:
        s = self.stats
        return (f"history_hits={s['history_hits']} blocked_skips={s['blocked_skips']} "
                f"vendor_hits={s['vendor_hits']} misses={s['misses']} "
                f"signatures={len(self.layer5.cache)}")

#!/usr/bin/env python3
"""Re-categorize portals in the Genie DB using the content-aware classifier.

Fetches each portal page and picks the most specific category justified by its
content (title / generator / form fields / text) + URL. By default only touches
rows that are currently GENERIC ('Student Portal' / 'ERP / Student Portal' /
'Portal' / blank) so good specific labels aren't disturbed; pass --all to redo
everything. Never downgrades a specific label to generic on a failed fetch.

Usage:
  .venv/bin/python genie/reclassify.py                 # generic rows only
  .venv/bin/python genie/reclassify.py --all --workers 16
  .venv/bin/python genie/reclassify.py --limit 200 --dry-run
"""
from __future__ import annotations

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "genie" / "core"))

from genie_core import db  # noqa: E402
from genie_core import categorize as cz  # noqa: E402

GENERIC = {"", "Portal", "Student Portal", "ERP / Student Portal", "Student Portal ", None}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="reclassify every portal, not just generic ones")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--timeout", type=float, default=12.0)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    db.init_db()
    with db.connect() as c:
        rows = c.execute("SELECT id, portal_url, category FROM portals ORDER BY id").fetchall()
    targets = [(r["id"], r["portal_url"], r["category"] or "") for r in rows
               if args.all or (r["category"] or "") in GENERIC]
    if args.limit:
        targets = targets[:args.limit]
    print(f"{len(targets)} portal(s) to classify ({'all' if args.all else 'generic only'}), "
          f"{args.workers} workers")

    # dedupe identical URLs so we fetch each once
    cache: dict[str, tuple[str, int, str]] = {}
    uniq = list({u for _, u, _ in targets})

    def work(u: str):
        return u, cz.classify(u, timeout=args.timeout)

    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for fut in as_completed([ex.submit(work, u) for u in uniq]):
            u, res = fut.result()
            cache[u] = res
            done += 1
            if done % 100 == 0:
                print(f"  …classified {done}/{len(uniq)} unique URLs")

    updates, changes = [], {}
    for pid, url, old in targets:
        cat, score, _ev = cache.get(url, ("", 0, ""))
        # only upgrade on a confident hit; never downgrade specific→generic on a miss
        if score > 0 and cat != old:
            updates.append((cat, pid))
            changes[f"{old or '∅'} → {cat}"] = changes.get(f"{old or '∅'} → {cat}", 0) + 1
        elif score == 0 and old in ("", None):
            updates.append((cat, pid))
            changes[f"∅ → {cat}"] = changes.get(f"∅ → {cat}", 0) + 1

    print(f"\n{len(updates)} row(s) would change:")
    for k, n in sorted(changes.items(), key=lambda kv: -kv[1]):
        print(f"  {n:5d}  {k}")

    if args.dry_run:
        print("\n(dry-run — no writes)")
        return
    with db.connect() as c:
        c.executemany("UPDATE portals SET category=? WHERE id=?", updates)
        c.commit()
    print(f"\n✓ updated {len(updates)} portals")
    with db.connect() as c:
        print("new distribution:")
        for cat, n in c.execute("SELECT category, COUNT(*) FROM portals GROUP BY category ORDER BY COUNT(*) DESC"):
            print(f"  {n:5d}  {cat!r}")


if __name__ == "__main__":
    main()

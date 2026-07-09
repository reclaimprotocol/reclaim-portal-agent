#!/usr/bin/env python3
"""Run the rules-free global judge agent and (optionally) score it against the
benchmark.

Usage:
  # one university
  .venv/bin/python scripts/run_magic.py nus.edu.sg "National University of Singapore"

  # whole benchmark, with recall scoring on labeled rows
  .venv/bin/python scripts/run_magic.py --benchmark

  # limit / filter the benchmark
  .venv/bin/python scripts/run_magic.py --benchmark --only nus.edu.sg,yonsei.ac.kr
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:  # load OPENROUTER_API_KEY etc. before the module reads them at import
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except Exception:  # noqa: BLE001
    pass

from agent import magic as G  # noqa: E402

BENCH = ROOT / "benchmark" / "portals.jsonl"


def _matches(url: str, needle: str) -> bool:
    return needle.lower() in url.lower()


def run_one(name: str, domain: str, country: str = "") -> list[dict]:
    portals = G.discover(name, domain, country)
    print(f"\n=== {name}  ({domain})  [{country or '—'}] ===")
    if not portals:
        print("  (no portals)")
    for p in portals:
        print(f"  [{p['confidence']:.2f}] {p['category']:14} {p['url']}")
        print(f"        via {p['provenance']} — {p['reason']}")
    return portals


def run_benchmark(only: set[str] | None) -> None:
    rows = [json.loads(l) for l in BENCH.read_text().splitlines() if l.strip()]
    if only:
        rows = [r for r in rows if r["domain"] in only]
    labeled_recalls: list[float] = []
    for r in rows:
        portals = run_one(r["name"], r["domain"], r.get("country", ""))
        urls = [p["url"] for p in portals]
        expected = r.get("expected") or []
        if expected:
            hit = [e for e in expected if any(_matches(u, e) for u in urls)]
            miss = [e for e in expected if e not in hit]
            recall = len(hit) / len(expected)
            labeled_recalls.append(recall)
            print(f"  >> recall {len(hit)}/{len(expected)} = {recall:.0%}"
                  + (f"   MISSED: {miss}" if miss else "   ✓ all found"))
        else:
            print(f"  >> unlabeled — {len(urls)} found, review to label ground truth")
    print("\n========================================")
    if labeled_recalls:
        avg = sum(labeled_recalls) / len(labeled_recalls)
        print(f"LABELED RECALL (mean over {len(labeled_recalls)} unis): {avg:.0%}")
    print("Unlabeled rows: eyeball the output above and add correct portals to "
          "benchmark/portals.jsonl 'expected' to lock them into the score.")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = sys.argv[1:]
    if "--benchmark" in args:
        only = None
        if "--only" in args:
            only = set(args[args.index("--only") + 1].split(","))
        run_benchmark(only)
        return
    if not args:
        print(__doc__)
        return
    domain = args[0]
    name = args[1] if len(args) > 1 else domain.split(".")[0]
    country = args[2] if len(args) > 2 else ""
    run_one(name, domain, country)


if __name__ == "__main__":
    main()

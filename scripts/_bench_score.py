#!/usr/bin/env python3
"""Score the suggestion-model benchmark against human-reviewed ground truth.

Ground truth is the 9July `Portals TnC` tab filtered to human-CONFIRMED working
portals (verdicts starting "ok", "login on same page", "resolved to exact login
endpoint", "kept — no separate login"). That makes this a real ACCURACY measure,
unlike the production sweep's "we found something" rate.

Matching reuses run_finetune_eval's normalization (drop scheme/port/query/
fragment, strip www, strip trailing slash) at two strictnesses:

  exact  — normalized host+path equal            (we found the same endpoint)
  host   — same host, any path                   (right system, different path)

Host-level is the fairer headline: a university's portal legitimately exposes
several login paths on one host, and the human reviewer recorded whichever they
landed on.

    .venv/bin/python scripts/_bench_score.py --sample bench9july_sample.json
"""
from __future__ import annotations
import argparse, json, sys
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]


def norm(url: str) -> str:
    if not url or not url.strip():
        return ""
    raw = url.strip()
    if "://" not in raw:
        raw = "https://" + raw
    try:
        p = urlparse(raw)
    except ValueError:
        return ""
    host = (p.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = p.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    return f"{host}{path}"


def host_of(url: str) -> str:
    return norm(url).split("/", 1)[0]


ap = argparse.ArgumentParser()
ap.add_argument("--sample", required=True)
a = ap.parse_args()

sample = {s["orgId"]: s for s in json.loads(Path(a.sample).read_text())}
arms: dict[str, dict] = defaultdict(dict)
for p in ROOT.glob("bench_*_shard*.json"):
    arm = p.name[len("bench_"):p.name.index("_shard")]
    try:
        arms[arm].update(json.loads(p.read_text()))
    except Exception:  # noqa: BLE001
        pass
if not arms:
    sys.exit("no bench_*_shard*.json results found")

MODEL_OF = {}
for arm, res in arms.items():
    for v in res.values():
        if v.get("model"):
            MODEL_OF[arm] = v["model"]; break

rows = []
per_org_hit: dict[str, set] = defaultdict(set)
for arm, res in sorted(arms.items()):
    scored = [o for o in res if o in sample]
    ex = hs = found = 0
    strict_tot = acc_tot = sug_tot = 0
    secs = []
    extra_verified = 0
    for oid in scored:
        r = res[oid]
        gt = sample[oid]["gt"]
        gt_n = {norm(g) for g in gt if g}
        gt_h = {host_of(g) for g in gt if g}
        got = r.get("accepted", []) or []
        strict_urls = [s["url"] for s in (r.get("strict") or [])]
        got_n = {norm(u) for u in got}
        got_h = {host_of(u) for u in got}
        if got:
            found += 1
        if gt_n & got_n:
            ex += 1
        if gt_h & got_h:
            hs += 1
            per_org_hit[oid].add(arm)
        # strict-verified URLs on a host the reviewer never recorded
        extra_verified += len([u for u in strict_urls if host_of(u) not in gt_h])
        sug_tot += len(r.get("suggested", []))
        acc_tot += len(got)
        strict_tot += len(strict_urls)
        if r.get("secs"):
            secs.append(r["secs"])
    n = len(scored) or 1
    rows.append({
        "arm": arm, "model": MODEL_OF.get(arm, arm), "n": len(scored),
        "exact": ex, "host": hs, "found": found,
        "exact_pct": 100 * ex / n, "host_pct": 100 * hs / n, "found_pct": 100 * found / n,
        "sug_per_org": sug_tot / n, "acc_per_org": acc_tot / n,
        "strict_per_org": strict_tot / n, "extra_verified": extra_verified,
        "median_s": sorted(secs)[len(secs) // 2] if secs else 0,
    })

rows.sort(key=lambda r: -r["host_pct"])
w = max(len(r["model"]) for r in rows)
print(f"\n{'MODEL':<{w}}  n   GT-hit(host)   GT-hit(exact)  found-any  sug/org acc/org strict/org  med-s  extra-verified")
print("-" * (w + 96))
for r in rows:
    print(f"{r['model']:<{w}} {r['n']:>3}  "
          f"{r['host']:>3} ({r['host_pct']:5.1f}%)  "
          f"{r['exact']:>3} ({r['exact_pct']:5.1f}%)  "
          f"{r['found']:>3} ({r['found_pct']:4.1f}%) "
          f"{r['sug_per_org']:>7.1f} {r['acc_per_org']:>7.2f} {r['strict_per_org']:>10.2f} "
          f"{r['median_s']:>6.1f} {r['extra_verified']:>14}")

# Complementarity: orgs only ONE model got — the union argument for harvest.
if len(arms) > 1:
    print("\n--- complementarity (host-level GT hits) ---")
    common = [o for o in per_org_hit if len(per_org_hit[o]) == len(arms)]
    none_ = [o for o in sample if o not in per_org_hit]
    print(f"  every model hit : {len(common)}")
    print(f"  no model hit    : {len(none_)}")
    for arm in sorted(arms):
        uniq = [o for o, s in per_org_hit.items() if s == {arm}]
        print(f"  ONLY {MODEL_OF.get(arm,arm):<34} {len(uniq):>3}"
              + (f"   e.g. {sample[uniq[0]]['name'][:34]}" if uniq else ""))
    union = len(per_org_hit)
    best = max(r["host"] for r in rows)
    print(f"\n  best single model : {best}/{len(sample)} ({100*best/len(sample):.1f}%)")
    print(f"  UNION of all      : {union}/{len(sample)} ({100*union/len(sample):.1f}%)"
          f"   -> +{union-best} orgs from combining harvests")

#!/usr/bin/env python3
"""A/B the HARVEST-suggestion model used by portal discovery.

Why only the suggestion model: across the 4,000-org August sweep, 2,236 orgs
(56%) returned NOTHING. That is a recall problem, and recall is set by what the
harvest proposes — the judge can only reject. So this benchmark holds the judge
(MAGIC_MODEL) fixed and varies MAGIC_SUGGEST_MODEL alone, which makes the arms
differ in exactly one variable and lets them share the judge-verdict cache.

Per org it records, for the arm's model:
  * raw URLs the LLM proposed          -> pure recall of the model's knowledge
  * portals accepted by judge+filters  -> what survives the existing pipeline
  * portals passing the STRICT test    -> a real password field, or a
                                          login-shaped URL answering 2xx/3xx

Set MAGIC_SUGGEST_MODEL in the environment; the arm label is derived from it.
The suggestion is fetched once and primed into the harvest cache under the same
model-namespaced key harvest uses, so discover() reuses it instead of paying a
second LLM call.

    MAGIC_SUGGEST_MODEL=qwen/qwen3.7-flash \
      .venv/bin/python scripts/_bench_suggest_models.py --sample s.json --shard 0 --of 5
"""
from __future__ import annotations
import argparse, json, os, re, signal, sys, time
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts"))
try:
    from dotenv import load_dotenv; load_dotenv(ROOT / ".env")
except Exception:  # noqa: BLE001
    pass
os.environ.setdefault("MAGIC_TNC", "0")          # portals only
import _bootstrap  # noqa: F401,E402
from agent import magic as G  # noqa: E402
import _run_portals_review as R  # noqa: E402
from agent.stages.js_renderer import _has_password_input  # noqa: E402

R._is_indian = lambda *a, **k: False

JUNK = re.compile(
    r"admission|applicant|ucanapply|/apply|entrance|prospectus|enrollonline|"
    r"opac|koha|pergamum|elibro|/library|biblioteca|digilib|"
    r"feepay|/payment|/pagar|checkout|/logout|logout=true|/privacy|/terms|"
    r"webmail|roundcube|/owa|elsevier|springer|ioppublishing|ebsco|jstor|turnitin|"
    r"facebook\.com|twitter\.com|linkedin\.com|youtube\.com", re.I)
STAGING = re.compile(r"(?:^|[.-])(uat|qa|test|testing|staging|homolog|dev|sandbox)(?:[.-]|\.)", re.I)

ap = argparse.ArgumentParser()
ap.add_argument("--sample", required=True)
ap.add_argument("--shard", type=int, default=0)
ap.add_argument("--of", type=int, default=1)
ap.add_argument("--report-remaining", action="store_true")
a = ap.parse_args()

MODEL = G.SUGGEST_MODEL
ARM = re.sub(r"[^a-z0-9]+", "-", MODEL.lower()).strip("-")
OUT = ROOT / f"bench_{ARM}_shard{a.shard}.json"

targets = json.loads(Path(a.sample).read_text())
done = {}
for p in ROOT.glob(f"bench_{ARM}_shard*.json"):
    try: done.update(json.loads(p.read_text()))
    except Exception: pass  # noqa: BLE001
mine = json.loads(OUT.read_text()) if OUT.exists() else {}

slice_ = [t for i, t in enumerate(targets) if i % a.of == a.shard]
todo = [t for t in slice_ if t["orgId"] not in done]
if a.report_remaining:
    print(len(todo)); sys.exit(0)


def strict_login(u: str):
    """(ok, evidence) — real password field, or login-shaped url answering 2xx/3xx."""
    try:
        signal.alarm(int(os.getenv("ROW_TIMEOUT", "75")))
        st, fin, html = R._fetch(u)
        if _has_password_input(html or ""):
            signal.alarm(0); return True, f"password field (static, {st})"
        res = R._render(u); rh = res.html if (res and res.ok) else ""
        if _has_password_input(rh or ""):
            signal.alarm(0); return True, "password field (rendered)"
        if isinstance(st, int) and 200 <= st < 400 and R._LOGIN_URLISH.search(fin or u):
            signal.alarm(0); return True, f"login-shaped endpoint, reachable ({st})"
        signal.alarm(0); return False, f"no login form (status {st})"
    except R._RowTimeout:
        signal.alarm(0); R._reset_renderer(); return False, "timeout"
    except Exception as e:  # noqa: BLE001
        signal.alarm(0); return False, f"error {type(e).__name__}"


signal.signal(signal.SIGALRM, R._on_alarm)
print(f"[{ARM}] shard {a.shard}/{a.of}: {len(todo)} orgs", flush=True)
for n, t in enumerate(todo, 1):
    oid, name, country = t["orgId"], t["name"], t["country"]
    primary = next((d.strip() for d in re.split(r"[,\s]+", t["domains"]) if d.strip()), "")
    rec = {"row": t["row"], "name": name, "country": country, "model": MODEL,
           "had_portal_baseline": t["had_portal"], "suggested": [], "accepted": [],
           "strict": [], "secs": 0.0, "why": "pre-mark (wedged) — revisit"}
    mine[oid] = rec; OUT.write_text(json.dumps(mine))
    if not primary:
        rec["why"] = "no email domain"; OUT.write_text(json.dumps(mine)); continue
    t0 = time.time()
    # 1) raw suggestion: the model's own knowledge, before any validation.
    try:
        signal.alarm(int(os.getenv("SUGGEST_TIMEOUT", "120")))
        sug = G._llm_suggest(name, primary, country) or []
        signal.alarm(0)
    except Exception as e:  # noqa: BLE001
        signal.alarm(0); sug = []; rec["why"] = f"suggest failed {type(e).__name__}"
    rec["suggested"] = sug[:60]
    # Prime harvest's cache under the SAME model-namespaced key so discover()
    # reuses this instead of making a second identical LLM call.
    if sug:
        try: G._cache_put(f"llm:{name}|{primary}{G._model_tag(MODEL)}", sug)
        except Exception: pass  # noqa: BLE001
    # 2) end-to-end: harvest -> fetch -> judge -> filters
    try:
        signal.alarm(int(os.getenv("DISCOVER_TIMEOUT", "300")))
        found = G.discover(name, primary, country) or []
        signal.alarm(0); rec["why"] = ""
    except R._RowTimeout:
        signal.alarm(0); R._reset_renderer(); found = []; rec["why"] = "discovery timed out"
    except Exception as e:  # noqa: BLE001
        signal.alarm(0); found = []; rec["why"] = f"discover error {type(e).__name__}"
    kept = []
    for p in found:
        u = p["url"]
        if JUNK.search(u) or STAGING.search((urlsplit(u).netloc or "").lower()):
            continue
        kept.append(u)
    rec["accepted"] = kept
    # 3) strict verification — identical bar to the production sweep
    for u in kept:
        ok, ev = strict_login(u)
        if ok:
            rec["strict"].append({"url": u, "evidence": ev})
    rec["secs"] = round(time.time() - t0, 1)
    mine[oid] = rec; OUT.write_text(json.dumps(mine))
    print(f"  [{n}/{len(todo)}] {name[:24]:26} [{country[:10]:10}] "
          f"sug={len(sug):2} acc={len(kept):2} strict={len(rec['strict']):2} "
          f"{rec['secs']:5.1f}s {'(baseline had none)' if not t['had_portal'] else ''}",
          flush=True)
print("DONE", flush=True)

#!/usr/bin/env python3
"""Re-run the T&C waterfall for an explicit list of **Portals TnC** rows
(tnc_rerun_rows.json) — the rows whose T&C was cleared as wrong/unrelated.

Header-driven (the tab layout has changed three times), sharded, resumable.

Crucially it VALIDATES what the waterfall returns before writing it: a result is
rejected if it fails the same ownership rules that condemned the old value —
wrong ccTLD for the org's country, another institution's domain, a standards or
generic-SaaS site. Otherwise the search would happily re-attach the very page we
just cleared (30 INACAP rows had picked up admision.eclass.com).
"""
from __future__ import annotations
import argparse, collections, json, os, re, signal, sys
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts"))
try:
    from dotenv import load_dotenv; load_dotenv(ROOT / ".env")
except Exception:  # noqa: BLE001
    pass
import _bootstrap  # noqa: F401,E402
from agent.config import load_config  # noqa: E402
from agent.sheets_client import SheetsClient  # noqa: E402
import agent.magic as M  # noqa: E402
import _run_portals_review as R  # noqa: E402

CCTLD = {"br":"Brazil","ar":"Argentina","mx":"Mexico","cl":"Chile","ph":"Philippines","ng":"Nigeria",
         "pk":"Pakistan","bd":"Bangladesh","lk":"Sri Lanka","bo":"Bolivia","co":"Colombia","ve":"Venezuela",
         "uy":"Uruguay","pe":"Peru","py":"Paraguay","cr":"Costa Rica","ni":"Nicaragua","ec":"Ecuador",
         "gt":"Guatemala","hn":"Honduras","sv":"El Salvador","pa":"Panama","do":"Dominican Republic",
         "jm":"Jamaica","in":"India"}
SPEC = re.compile(r"dublincore\.org|openarchives\.org|w3\.org|schema\.org", re.I)
GENERIC = re.compile(r"freshworks|oracle\.com|linkedin\.com|automattic|sites\.google|drive\.google|"
                     r"amazonaws\.com|website-files\.com|helpjuice|ninjateam|policies\.google", re.I)
OTHER_INST = re.compile(r"wccaviation\.com|ccacolchester\.com|wanderbrowser\.com|admision\.eclass\.com", re.I)

ap = argparse.ArgumentParser()
ap.add_argument("--rows-json", default=str(ROOT / "tnc_rerun_rows.json"))
ap.add_argument("--shard", type=int, default=0)
ap.add_argument("--of", type=int, default=1)
ap.add_argument("--report-remaining", action="store_true")
a = ap.parse_args()

sc = SheetsClient.from_config(load_config()); sc.sheet_id = R.SHEET
svc = sc._service.spreadsheets()
g = svc.values().get(spreadsheetId=R.SHEET, range=f"'{R.TAB}'!A1:K").execute().get("values", [])
hdr = g[0]; W = max(len(x) for x in g); rows = [(x + [""] * W)[:W] for x in g[1:]]
idx = {(x or "").strip().lower(): i for i, x in enumerate(hdr)}
O, N, C, D, P, T = (idx["organization id"], idx["organization name"], idx["country"],
                    idx["email domains"], idx["portal url"], idx["t&c url"])
TR = idx.get("tnc human review")
A1 = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"

def root(u):
    try: return M._registrable_root(M._norm_host(u if "://" in u else "http://" + u)) or ""
    except Exception: return ""  # noqa: BLE001
def cc(u):
    h = (urlsplit(u if "://" in u else "http://" + u).netloc or "").lower().split(":")[0]
    return CCTLD.get(h.rsplit(".", 1)[-1], "")

orgroots = collections.defaultdict(set)
for r in rows:
    o = (r[O] or "").strip()
    if not o: continue
    for d in re.split(r"[,\s]+", (r[D] or "")):
        if d.strip(): orgroots[o].add(root(d))
    if (r[P] or "").strip(): orgroots[o].add(root(r[P]))
    orgroots[o].discard("")

def acceptable(url, org, country):
    """Same ownership rules that condemned the old value."""
    if root(url) in orgroots.get(org, set()):
        return True, "own domain"
    if SPEC.search(url): return False, "standards/spec site"
    if GENERIC.search(url): return False, "generic SaaS"
    if OTHER_INST.search(url): return False, "another institution"
    tcc = cc(url)
    if tcc and country and tcc != country:
        return False, f"wrong country ({tcc} vs {country})"
    return True, "vendor/parent (allowed)"

want = set(json.loads(Path(a.rows_json).read_text()))
DONE = ROOT / (f"tnc_rerun_done_shard{a.shard}.json" if a.of > 1 else "tnc_rerun_done.json")
done = set()
for p in ROOT.glob("tnc_rerun_done*.json"):
    try: done |= set(json.loads(p.read_text()))
    except Exception: pass  # noqa: BLE001
mine = set(json.loads(DONE.read_text())) if DONE.exists() else set()

todo = [(i, rows[i - 2]) for i in sorted(want)
        if i - 2 < len(rows) and i not in done
        and not (rows[i - 2][T] or "").strip().lower().startswith("http")]
todo = [t for n, t in enumerate(todo) if n % a.of == a.shard]
if a.report_remaining:
    print(len(todo)); sys.exit(0)

print(f"tnc re-run shard {a.shard}/{a.of}: {len(todo)} rows", flush=True)
signal.signal(signal.SIGALRM, R._on_alarm)
to = int(os.getenv("TNC_ROW_TIMEOUT", "90"))
cache: dict = {}
hits = rejected = 0
for n, (row, r) in enumerate(todo, 1):
    org, name, dom, portal, country = ((r[O] or "").strip(), (r[N] or "").strip(),
                                       (r[D] or "").strip(), (r[P] or "").strip(), (r[C] or "").strip())
    try:
        signal.alarm(to)
        gnew, _tc, tf = R.review_or_fill_tnc("", portal, dom, name, cache)
        signal.alarm(0)
    except R._RowTimeout:
        signal.alarm(0); R._reset_renderer(); gnew, tf = "N/A", "timed out"
    except Exception as e:  # noqa: BLE001
        signal.alarm(0); gnew, tf = "N/A", f"error ({type(e).__name__})"
    val, note = "N/A", tf
    if str(gnew).lower().startswith("http"):
        ok, why = acceptable(gnew, org, country)
        if ok:
            val, note, hits = gnew, f"{tf} [{why}]", hits + 1
        else:
            note, rejected = f"rejected: {why} — {str(gnew)[:60]}", rejected + 1
    data = [{"range": f"'{R.TAB}'!{A1[T]}{row}", "values": [[val]]}]
    if TR is not None:
        data.append({"range": f"'{R.TAB}'!{A1[TR]}{row}", "values": [[note[:200]]]})
    R._retry(lambda: svc.values().batchUpdate(spreadsheetId=R.SHEET,
             body={"valueInputOption": "RAW", "data": data}).execute())
    mine.add(row); DONE.write_text(json.dumps(sorted(mine)))
    print(f"  [{n}/{len(todo)}] row{row} {name[:22]:22} {val[:56]}", flush=True)
print(f"DONE (found {hits}, rejected {rejected}, none {len(todo)-hits-rejected})", flush=True)

#!/usr/bin/env python3
"""Audit **Portals TnC**: does each T&C actually belong to its org?

Flags a T&C whose registrable root matches neither the org's own email domains
nor its portal host, and then classifies why:

  WRONG-COUNTRY  the T&C's ccTLD contradicts the org's country (a Brazilian org
                 carrying a .ph policy — five such cases turned up in a 56-org
                 sample, so this is the headline check)
  VENDOR         a known platform/host policy (moodle.com, blackboard, sucuri,
                 symplicity, quicklaunch…) — legitimate-ish but not the school's
  PARENT/GROUP   different domain, same country: usually a parent brand, fine
  UNRELATED      different domain and no country signal either way

Read-only. Writes tnc_ownership_audit.csv.
"""
from __future__ import annotations
import collections, csv, re, sys
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

CCTLD = {"br": "Brazil", "ar": "Argentina", "mx": "Mexico", "cl": "Chile", "ph": "Philippines",
         "ng": "Nigeria", "pk": "Pakistan", "bd": "Bangladesh", "lk": "Sri Lanka", "bo": "Bolivia",
         "co": "Colombia", "ve": "Venezuela", "uy": "Uruguay", "pe": "Peru", "py": "Paraguay",
         "cr": "Costa Rica", "ni": "Nicaragua", "ec": "Ecuador", "gt": "Guatemala", "hn": "Honduras",
         "sv": "El Salvador", "pa": "Panama", "do": "Dominican Republic", "jm": "Jamaica", "in": "India"}
VENDOR = re.compile(r"moodle\.com|blackboard\.com|symplicity\.com|sucuri\.net|quicklaunch\.io|"
                    r"instructure\.com|jacad\.com|ulife\.com|eduqtecnologia|academic\.lat|"
                    r"provafacilnaweb|nugitech\.com|policies\.google|privacytools\.com|"
                    r"microsoft\.com|apple\.com|d2l\.com|canvaslms", re.I)

def root(u):
    try:
        return M._registrable_root(M._norm_host(u if "://" in u else "http://" + u)) or ""
    except Exception:  # noqa: BLE001
        return ""
def cc(u):
    h = (urlsplit(u if "://" in u else "http://" + u).netloc or "").lower().split(":")[0]
    parts = h.rsplit(".", 2)
    for cand in (".".join(parts[-2:]), parts[-1] if parts else ""):
        t = cand.split(".")[-1]
        if t in CCTLD:
            return CCTLD[t]
    return ""

sc = SheetsClient.from_config(load_config()); sc.sheet_id = R.SHEET
g = sc._service.spreadsheets().values().get(spreadsheetId=R.SHEET, range=f"'{R.TAB}'!A1:K").execute().get("values", [])
hdr = g[0]; W = max(len(x) for x in g); rows = [(x + [""] * W)[:W] for x in g[1:]]
idx = {(x or "").strip().lower(): i for i, x in enumerate(hdr)}
O, N, C, D, P, T = (idx["organization id"], idx["organization name"], idx["country"],
                    idx["email domains"], idx["portal url"], idx["t&c url"])

out, tally = [], collections.Counter()
for i, r in enumerate(rows, start=2):
    t = (r[T] or "").strip()
    if not t.lower().startswith("http"):
        continue
    org, name, country = (r[O] or "").strip(), (r[N] or "").strip(), (r[C] or "").strip()
    dom_roots = {root(d) for d in re.split(r"[,\s]+", (r[D] or "")) if d.strip()}
    dom_roots |= {root((r[P] or "").strip())}
    dom_roots.discard("")
    troot = root(t)
    if troot and troot in dom_roots:
        tally["OK (own domain)"] += 1; continue
    tcc = cc(t)
    if VENDOR.search(t):
        kind = "VENDOR"
    elif tcc and country and tcc != country:
        kind = "WRONG-COUNTRY"
    elif tcc and country and tcc == country:
        kind = "PARENT/GROUP (same country)"
    else:
        kind = "UNRELATED (no country signal)"
    tally[kind] += 1
    out.append({"row": i, "orgId": org, "name": name[:44], "country": country,
                "kind": kind, "tnc_country": tcc, "portal": (r[P] or "")[:70], "tnc": t[:90]})

print(f"T&C rows checked: {sum(tally.values())}")
for k, v in tally.most_common():
    print(f"  {v:5}  {k}")
p = ROOT / "tnc_ownership_audit.csv"
with open(p, "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(out[0].keys()) if out else
                       ["row","orgId","name","country","kind","tnc_country","portal","tnc"])
    w.writeheader(); w.writerows(out)
print(f"\nwrote {p.name} ({len(out)} flagged rows)")
wc = [o for o in out if o["kind"] == "WRONG-COUNTRY"]
print(f"\n=== WRONG-COUNTRY: {len(wc)} rows, {len({o['orgId'] for o in wc})} orgs ===")
for o in wc[:25]:
    print(f"  row{o['row']:<5} {o['orgId']:10} {o['name'][:30]:32} [{o['country']:<12}] <- T&C in {o['tnc_country']:<12} {o['tnc'][:52]}")
if len(wc) > 25: print(f"  ... {len(wc)-25} more (see CSV)")

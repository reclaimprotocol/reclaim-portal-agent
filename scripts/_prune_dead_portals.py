#!/usr/bin/env python3
"""Prune the current review window down to WORKING login portals + their T&Cs.

Runs AFTER the review pass has assigned a col-E verdict to every portal row in
the window (scripts/_run_portals_review.py, proxy-enabled). Deletes rows whose
portal verdict marks them dead / not-working / no-login / junk, and KEEPS rows
with a confirmed login form (green) or an alive-but-gated geo-blocked portal
(real student area that WAFs our IP — per global_portal_review_rules).

  DELETE if col-E matches _REMOVE (unreachable, render-failed, no-login element,
          webmail, junk, bare-IdP, sub-page dup, duplicate, blocked/dead, remove)
  KEEP    green ("ok — login…") and geo-block ambers ("geo-blocked … verify")
  SKIP    empty col-E (unreviewed — never delete something not yet reviewed)

Every deleted row is snapshotted to a timestamped JSON first so a bad prune is
fully recoverable. Deletes bottom-up so row indices stay valid. Scope = the org
IDs in portals_review_window.json (the active 50-org batch).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts"))
try:
    from dotenv import load_dotenv; load_dotenv(ROOT / ".env")
except Exception:  # noqa: BLE001
    pass

import _bootstrap  # noqa: F401,E402
from agent.config import load_config  # noqa: E402
from agent.sheets_client import SheetsClient  # noqa: E402

SHEET = "1sDK_1VnRHIuUqBComrvwS1JvSmB_l0_4Rsf9rfezFNw"
TAB = "Portals TnC"
WINDOW = ROOT / "portals_review_window.json"

# col-E verdicts that mean "remove this portal row"
_REMOVE = re.compile(
    r"unreachable|render failed|fails to render|no login element|live but no login|"
    r"webmail|\bjunk\b|bare idp|idp[- ]metadata|sub-?page|not a login|duplicate|"
    r"blocked/dead|likely blocked|—\s*remove\b", re.I)
# never delete these even if some other word matched
_KEEP = re.compile(r"\bok\b|login form present|login present|resolved to exact|"
                   r"geo-?block|verify via vpn|kept —", re.I)
# tenant/federated SSO logins are real portals — never prune by URL shape,
# independent of the col-E verdict (belt-and-suspenders with the reviewer).
_TENANT_SSO = re.compile(
    r"login\.microsoftonline\.com/[0-9a-f]{8}-[0-9a-f]{4}-|/adfs/ls/|"
    r"[?&]SAMLRequest=|[?&]wtrealm=|/o/oauth2/|/oauth2/authorize", re.I)


def _retry(fn, n=5):
    for i in range(n):
        try:
            return fn()
        except Exception:  # noqa: BLE001
            if i == n - 1:
                raise
            time.sleep(2 * (i + 1))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report-remaining", action="store_true",
                    help="print how many rows WOULD be deleted, delete nothing")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    sc = SheetsClient.from_config(load_config()); sc.sheet_id = SHEET
    svc = sc._service.spreadsheets()
    gid = next(s["properties"]["sheetId"]
               for s in _retry(lambda: svc.get(spreadsheetId=SHEET).execute())["sheets"]
               if s["properties"]["title"] == TAB)
    try:
        win = set(json.loads(WINDOW.read_text()))
    except Exception:  # noqa: BLE001
        print("no window cache — nothing to prune"); return
    rows = _retry(lambda: svc.values().get(spreadsheetId=SHEET,
                  range=f"'{TAB}'!A2:H").execute()).get("values", [])

    from collections import defaultdict
    org_all: dict[str, list] = defaultdict(list)   # oid -> [(row, portal, e, g, name, dom)]
    removal: set[int] = set()                       # rows with a removal verdict
    rowdata: dict[int, dict] = {}
    for i, r in enumerate(rows, 2):
        r = (r + [""] * 8)[:8]
        oid, name, dom, portal, e, cat, g, h = [c.strip() if isinstance(c, str) else c for c in r]
        if oid not in win:
            continue
        org_all[oid].append((i, portal, e, g, name, dom))
        rowdata[i] = {"row": i, "orgId": oid, "name": name, "domains": dom,
                      "portal": portal, "E": e, "tnc": g, "H": h}
        if not portal or portal == "(none found)":
            continue
        if not e or _TENANT_SSO.search(portal) or _KEEP.search(e):
            continue  # unreviewed / tenant-SSO / working-login — keep
        if _REMOVE.search(e):
            removal.add(i)

    # Decide per org: if a WORKING portal survives, just delete the removal rows.
    # If NO working portal survives, DON'T drop the org — collapse it to a single
    # N/A row (portal + T&C = N/A) and delete its other rows.
    to_delete: list[int] = []
    to_na: list[tuple[int, str, str]] = []          # (row, name, dom)
    for oid, rlist in org_all.items():
        survives = any(p and p != "(none found)" and ri not in removal
                       for (ri, p, e, g, name, dom) in rlist)
        if survives:
            to_delete += [ri for (ri, p, e, g, name, dom) in rlist if ri in removal]
        else:
            keep = min(ri for (ri, p, e, g, name, dom) in rlist)
            nm = next(nm for (ri, p, e, g, nm, dm) in rlist if ri == keep)
            dm = next(dm for (ri, p, e, g, nm, dm) in rlist if ri == keep)
            to_na.append((keep, nm, dm))
            to_delete += [ri for (ri, p, e, g, name, dom) in rlist if ri != keep]

    if args.report_remaining:
        print(len(to_delete) + len(to_na)); return

    print(f"prune: {len(to_delete)} rows to delete, {len(to_na)} orgs -> N/A "
          f"(no working portal; kept as a row) (window {len(win)} orgs)", flush=True)
    if not to_delete and not to_na:
        print("nothing to prune — list already clean"); return

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    snap = ROOT / f"prune_snapshot_{stamp}.json"
    snap.write_text(json.dumps({"deleted": [rowdata[i] for i in to_delete],
                                "set_na": [rowdata[i] for i, _, _ in to_na]},
                               indent=2, ensure_ascii=False))
    print(f"snapshot -> {snap.name} ({len(to_delete)} del + {len(to_na)} na, recoverable)", flush=True)

    if args.dry_run:
        for i, nm, dm in to_na[:40]:
            print(f"  WOULD N/A  row{i} {nm[:24]:24} (was {rowdata[i]['portal'][:35]})")
        for i in to_delete[:20]:
            print(f"  WOULD DEL  row{i} {rowdata[i]['name'][:20]:20} {rowdata[i]['portal'][:40]}")
        print("dry-run — no changes"); return

    # 1) set the N/A rows (portal D, review E, tnc G, tnc-review H) + neutral color
    WHITE = {"red": 1, "green": 1, "blue": 1}
    data = []
    color = []
    for i, nm, dm in to_na:
        data += [{"range": f"'{TAB}'!D{i}", "values": [["N/A"]]},
                 {"range": f"'{TAB}'!E{i}", "values": [["no working portal found after review"]]},
                 {"range": f"'{TAB}'!G{i}", "values": [["N/A"]]},
                 {"range": f"'{TAB}'!H{i}", "values": [["N/A"]]}]
        color.append({"repeatCell": {"range": {"sheetId": gid, "startRowIndex": i - 1, "endRowIndex": i,
                      "startColumnIndex": 0, "endColumnIndex": 8},
                      "cell": {"userEnteredFormat": {"backgroundColor": WHITE}},
                      "fields": "userEnteredFormat.backgroundColor"}})
    if data:
        _retry(lambda: svc.values().batchUpdate(spreadsheetId=SHEET,
               body={"valueInputOption": "RAW", "data": data}).execute())
        _retry(lambda: svc.batchUpdate(spreadsheetId=SHEET, body={"requests": color}).execute())

    # 2) delete removal rows, bottom-up so row numbers stay valid
    reqs = [{"deleteDimension": {"range": {"sheetId": gid, "dimension": "ROWS",
             "startIndex": row - 1, "endIndex": row}}}
            for row in sorted(to_delete, reverse=True)]
    for k in range(0, len(reqs), 200):
        _retry(lambda k=k: svc.batchUpdate(spreadsheetId=SHEET,
               body={"requests": reqs[k:k + 200]}).execute())
    print(f"DELETED {len(reqs)} rows, set {len(to_na)} orgs to N/A. "
          f"Every org kept: working portals or an N/A row.", flush=True)
    print("DONE", flush=True)


if __name__ == "__main__":
    main()

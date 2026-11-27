"""Offline replay of the 1,281 reviewed Tier A gate decisions.

The Tier A review ran GenieV3's student-login gate (commit d29cc60) on every
user-reported URL of 568 low-conversion SheerID orgs, and an independent
reviewer judged each decision from its own browser visit. This script replays
those decisions through the rules added since, WITHOUT the network:

  1. No ACCEPT without a seen login     -> from the gate's own note
  2. Ownership                          -> `ownership_evidence` on the reviewer's
                                           page text; "the org's homepage links
                                           to it" needs the network and is
                                           counted as REVIEW here
  3. Same-login key + already have      -> `portal_dedupe_key` / known URLs
  4. One-time / IdP URLs                -> `publishable_login_url` rules, with the
                                           reviewer's fresh visit of the URL as
                                           the reload result

and reports ACCEPT precision, real logins moved out of ACCEPT, duplicates and
one-time URLs, before and after. Rows whose published URL changes to a login
the replay cannot see (a confirmed link target, the best of several logins)
are counted apart: their old label does not apply to the new URL.

Usage:
    python benchmark/replay_tier_a.py --merged <scratch>/review/merged.json \\
        [--tier-a <scratch>/tier_a.json] [--dash <scratch>/dash_urls.json] \\
        [--recheck <scratch>/recheck_all.json]

Inputs are local review files; nothing is fetched or written. URLs are never
printed, only counts and review ids.
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import json
import os
import re
import sys
from urllib.parse import urlsplit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import guardrails as G  # noqa: E402

CLICK_ONE = "login reached by clicking"
CLICK_MODAL = "login opens on the page after a click"
CLICK_MULTI = "different student logins reachable"
CLICK_DRAWN = "once it finished drawing"
UNCONF, UNOPENED = G.LINK_UNCONFIRMED_NOTE, G.BUTTON_UNOPENED_NOTE
CONFIRMED = "login link opened and confirmed"

# One-time or per-person parts the review found in published URLs.
VOLATILE_RE = re.compile(
    r"(?i)/membros/\d{6,}|login-automatico|autologin|[?&;](?:authstate|samlrequest|sesskey|"
    r"token|jwt|sid|ticket|jacamsysid|jsessionid|code_challenge)=|/oauth2/|"
    r"/protocol/openid-connect/|authenticationendpoint/")

_OLD_INDEX_RE = re.compile(r"/(?:index|default)\.(?:php|html?|aspx?|jsp|do)$", re.I)
_OLD_JSESSIONID_RE = re.compile(r";jsessionid=[^/?#;]*", re.I)
_OLD_TRACKING = {"_gl", "_ga", "fbclid", "gclid", "gbraid", "wbraid", "msclkid", "dclid",
                 "mc_cid", "mc_eid", "yclid", "igshid"}


def old_dedupe_key(url: str) -> str:
    """`portal_dedupe_key` as of d29cc60, for the "before" numbers."""
    from urllib.parse import parse_qsl, urlencode, urlunsplit
    parsed = urlsplit(G._as_url(url))
    query = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
             if k.lower() not in _OLD_TRACKING and k.lower() not in {"lang", "changelangcd"}
             and not k.lower().startswith(("utm_", "gad_"))]
    path = _OLD_JSESSIONID_RE.sub("", parsed.path or "")
    parsed = urlsplit(urlunsplit((parsed.scheme, parsed.netloc, path, urlencode(query), "")))
    host = (parsed.hostname or "").lower().rstrip(".").removeprefix("www.")
    if parsed.port and parsed.port not in (80, 443):
        host = f"{host}:{parsed.port}"
    path = _OLD_INDEX_RE.sub("", (parsed.path or "/").rstrip("/")).rstrip("/").lower() or "/"
    return f"{host}{path}" + (f"?{parsed.query}" if parsed.query else "")


def reference_key(url: str) -> str:
    """The hand-built same-login key of the review cleanup (`loose()` in
    dedup_tier_a.py): the ground truth the new key is measured against."""
    from urllib.parse import parse_qsl, urlencode, urlunsplit
    drop = {"ref", "returnurl", "mcode", "redirect", "sesskey", "token", "jwt", "sid", "ticket",
            "authstate", "samlrequest", "samlresponse", "relaystate", "signature", "sigalg",
            "state", "nonce", "code", "cookietime", "client_id", "enc", "a", "b", "logout",
            "next", "loginredirect", "sessionexpired", "target", "estado", "inscripcion",
            "aspxautodetectcookiesupport", "_gl", "utm_source", "utm_medium", "utm_campaign",
            "gclid", "fbclid", "lang", "language"}
    if re.match(r"(?i)^https?://[^/]+/iitms[^/?]*", url or ""):
        p = urlsplit(url)
        return (p.hostname or "").lower().removeprefix("www.") + "/iitms"
    u = re.sub(r"(?i);jacamsysid=[^?#/]*|;jsessionid=[^?#/]*", "", url or "")
    p = urlsplit(u if "://" in u else "https://" + u)
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if k.lower() not in drop]
    p = urlsplit(urlunsplit((p.scheme, p.netloc, p.path, urlencode(q), "")))
    path = re.sub(r"(?i)/(index|default|home|inicio)(\.(php|aspx?|html?|jsp))?$", "/", p.path.lower())
    path = re.sub(r"(?i)/(acceso|account|accounts|auth|login|signin)(/login)?/?$", "/", path).rstrip("/")
    return (p.hostname or "").lower().removeprefix("www.") + path + (
        "?" + p.query.lower() if p.query else "")


def known_urls(row: dict, dash: dict[str, list[str]], org_id: str) -> list[str]:
    cur = [c.replace(" (inactive)", "").strip()
           for c in (row.get("currentProviderUrls") or "").split(" | ") if c.strip()]
    return cur + dash.get(org_id, [])


RECHECK: dict[str, dict] = {}


def reload_oracle(row: dict, url: str):
    """A fresh visit of ``url`` from the review data, when there is one: the
    cleanup's own reload of the cleaned URL (``--recheck``) first, then the
    reviewer's visit of the URL the gate published."""
    hit = RECHECK.get(G.canonical_portal_url(url))
    if hit is not None and not hit.get("error"):
        status = int(hit.get("status") or 0)
        if status == 0 or status >= 400:
            return False
        if str(hit.get("pw")) in ("True", "true", "1") or G.is_identity_provider_url(
                hit.get("final") or ""):
            return True
        return None
    for ev in (row.get("evidence_final"), row.get("evidence_reported")):
        if not ev or ev.get("error"):
            continue
        if G.canonical_portal_url(ev.get("final") or "") == G.canonical_portal_url(url) or \
                G.canonical_portal_url(row["gate"]["finalUrl"]) == G.canonical_portal_url(url):
            status = int(ev.get("status") or 0)
            if status >= 400 or status == 0:
                return False
            return True if str(ev.get("pw")) in ("True", "true", "1") else None
    return None


async def replay(row: dict, website: str, known: list[str]) -> tuple[str, str, str]:
    """``(decision, reason, url)`` under the new rules, offline."""
    gate = row["gate"]
    if gate["decision"] != "ACCEPT":
        return gate["decision"], "unchanged", ""
    note, requested, found = gate["note"], row["reportedUrl"], gate["finalUrl"] or row["reportedUrl"]
    unseen = UNCONF in note or UNOPENED in note
    url_unknown = False
    if CLICK_DRAWN in note:
        return "REVIEW", "login_unseen", ""
    if CLICK_MULTI in note:
        url_unknown = True                    # now the best of the logins reached
    elif unseen and CLICK_ONE not in note and CLICK_MODAL not in note:
        return "REVIEW", "login_unseen", ""
    if (CONFIRMED in note and "Chromium" not in note
            and found.rstrip("/") == requested.rstrip("/")):
        url_unknown = True                    # the confirmed link target, not the landing

    # Item 4, with the reviewer's visit standing in for the cookie-less reload.
    async def fake_reload(url, **_k):
        return reload_oracle(row, url)
    G._reloads_as_login = fake_reload  # type: ignore[assignment]
    published, _why = await G.publishable_login_url(requested, found)
    if not published:
        return "REVIEW", "volatile", ""

    # Item 2: the reviewer's page text stands in for the fetched page.
    ev = row.get("evidence_final") or row.get("evidence_reported") or {}
    text = f"{ev.get('title') or ''} {ev.get('text') or ''}"
    relation, _ = G.ownership_evidence(published, official_domain=website, known_urls=known,
                                       org_name=row.get("org") or "", page_text=text)
    if not relation and G.ownership_evidence(requested, official_domain=website,
                                             known_urls=known)[0] in ("official", "known") \
            and (url_unknown or G.portal_dedupe_key(requested) != G.portal_dedupe_key(published)):
        relation = "official_link"
    if not relation:
        return "REVIEW", "wrong_org", published   # homepage-link proof not replayable offline
    if not url_unknown and G.portal_dedupe_key(published) in {
            G.portal_dedupe_key(k) for k in known if k}:
        return "ALREADY_HAVE", "already_have", published
    return "ACCEPT", "url_changed" if url_unknown else "login", published


def pct(a: int, b: int) -> str:
    return f"{100 * a / b:.1f}%" if b else "n/a"


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--merged", required=True, help="review/merged.json")
    ap.add_argument("--tier-a", help="tier_a.json (org x URL rows, for duplicate counts)")
    ap.add_argument("--dash", help="dash_urls.json (dashboard portal rows per org)")
    ap.add_argument("--recheck", help="recheck_all.json (fresh reloads of cleaned URLs)")
    ap.add_argument("--json", help="write per-row results here")
    args = ap.parse_args()

    rows = json.load(open(args.merged))
    if args.recheck:
        for url, hit in json.load(open(args.recheck)).items():
            RECHECK[G.canonical_portal_url(url)] = hit
    dash: dict[str, list[str]] = collections.defaultdict(list)
    if args.dash:
        for d in json.load(open(args.dash)):
            if d.get("url"):
                dash[str(d["orgId"])].append(d["url"])

    out = []
    for r in rows:
        org = str(r["orgId"])
        decision, reason, url = await replay(r, r.get("website") or "",
                                             known_urls(r, dash, org))
        rv = r.get("review") or {}
        out.append({"id": r["id"], "orgId": org, "before": r["gate"]["decision"],
                    "after": decision, "reason": reason, "label": rv.get("gate", ""),
                    "view": rv.get("view", ""), "error": rv.get("error", ""),
                    "beforeUrl": r["gate"]["finalUrl"], "afterUrl": url})

    def accepted(o, side):
        return o[side] in ("ACCEPT", "ALREADY_HAVE")

    print(f"Reviewed decisions: {len(out)}")
    print("\n== ACCEPT precision (reviewer-labelled correct / (correct + wrong)) ==")
    b = [o for o in out if o["before"] == "ACCEPT"]
    bc, bw = sum(o["label"] == "correct" for o in b), sum(o["label"] == "wrong" for o in b)
    print(f"before: {len(b)} accepts, {bc} correct, {bw} wrong, "
          f"{len(b) - bc - bw} unclear -> precision {pct(bc, bc + bw)}")
    a = [o for o in out if accepted(o, "after")]
    scored = [o for o in a if o["reason"] != "url_changed"]
    ac, aw = sum(o["label"] == "correct" for o in scored), sum(o["label"] == "wrong" for o in scored)
    changed = [o for o in a if o["reason"] == "url_changed"]
    print(f"after:  {len(a)} accepts ({sum(o['after'] == 'ALREADY_HAVE' for o in a)} of them "
          f"ALREADY_HAVE), {ac} correct, {aw} wrong, {len(scored) - ac - aw} unclear "
          f"-> precision {pct(ac, ac + aw)}")
    print(f"        + {len(changed)} accepts now publish a different login the replay cannot "
          f"see (reviewer view of the old page: "
          f"{dict(collections.Counter(o['view'] for o in changed))})")
    lead = sum(o["view"] in ("STUDENT_LOGIN", "LEADS_TO_STUDENT_LOGIN") for o in changed)
    print(f"        if those {lead} whose page is or leads to a student login are right: "
          f"precision {pct(ac + lead, ac + aw + len(changed))}")

    print("\n== Where the old accepts went ==")
    moved = collections.Counter((o["after"], o["reason"], o["label"]) for o in b)
    for (dec, why, label), n in sorted(moved.items()):
        print(f"  {dec:12s} {why:14s} {label or '-':8s} {n}")
    wrong_err = collections.Counter(o["error"] for o in b if o["label"] == "wrong"
                                    and not accepted(o, "after"))
    print(f"  wrong accepts removed from ACCEPT, by error type: {dict(wrong_err)}")
    left = collections.Counter(o["error"] for o in a if o["label"] == "wrong"
                               and o["reason"] != "url_changed")
    print(f"  wrong accepts still accepted, by error type: {dict(left)}")

    print("\n== Real logins kept out of ACCEPT ==")
    rej_wrong = [o for o in out if o["before"] == "REJECT" and o["label"] == "wrong"]
    lost_before = sum(o["view"] == "STUDENT_LOGIN" for o in rej_wrong)
    demoted = [o for o in b if not accepted(o, "after") and o["label"] == "correct"]
    print(f"wrong REJECTs: {len(rej_wrong)} before, {len(rej_wrong)} after "
          f"(rows 1-4 do not touch rejects; {lost_before} are real student logins)")
    print(f"correct ACCEPTs now REVIEW (not lost, but a person must confirm): {len(demoted)} "
          f"{dict(collections.Counter(o['reason'] for o in demoted))}")

    print("\n== One-time / session URLs among published accepts ==")
    vb = sum(bool(VOLATILE_RE.search(o["beforeUrl"] or "")) for o in b)
    va = sum(bool(VOLATILE_RE.search(o["afterUrl"] or "")) for o in a)
    print(f"before: {vb}   after: {va}")

    if args.tier_a:
        print("\n== Same-org duplicates among published accepts (org x URL rows) ==")
        by_url = {r["reportedUrl"]: r for r in rows}
        res = {o["id"]: o for o in out}
        tier = json.load(open(args.tier_a))
        for side, key_fn, url_field in (("before", old_dedupe_key, "beforeUrl"),
                                        ("after", G.portal_dedupe_key, "afterUrl")):
            groups: dict = collections.defaultdict(set)
            exact: dict = collections.defaultdict(int)
            for t in tier:
                r = by_url.get(t["url"])
                # One review per URL, made for one org: another org's row for
                # the same URL would borrow that org's outcome, so it is left out.
                if not r or str(r["orgId"]) != str(t["orgId"]):
                    continue
                o = res[r["id"]]
                if not accepted(o, side) or not o[url_field]:
                    continue
                u = o[url_field]
                groups[(t["orgId"], reference_key(u))].add(key_fn(u))
                exact[(t["orgId"], key_fn(u))] += 1
            slipped = sum(len(keys) - 1 for keys in groups.values())
            merged = sum(n - 1 for n in exact.values())
            print(f"{side}: {sum(exact.values())} accepted org x URL rows, {merged} merged by the "
                  f"key, {slipped} same-login spellings the key misses")
        skipped = sum(1 for t in tier if t["url"] in by_url
                      and str(by_url[t["url"]]["orgId"]) != str(t["orgId"]))
        print(f"(left out: {skipped} org x URL rows whose URL was reviewed for another org)")
        cross = collections.Counter(t["url"] for t in tier)
        print(f"URLs reported for more than one org (kept once per org): "
              f"{sum(1 for n in cross.values() if n > 1)}")

    if args.json:
        json.dump(out, open(args.json, "w"), indent=1)


if __name__ == "__main__":
    asyncio.run(main())

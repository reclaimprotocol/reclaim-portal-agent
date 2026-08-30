"""Genie-V3 · Layer 2 — Local Knowledge Matrix Filter (the token shield).

The crawler returns EVERY anchor on a rendered page — 55 links for BUET, 146 for
Uva Wellassa, and university homepages routinely exceed 300. Sending those raw
to OpenRouter is both expensive and counter-productive: the signal drowns.

This filter sits between them. It is pure regex and set lookups, no network and
no model, so it costs microseconds and strips the obvious noise before anything
reaches the inference phase.

WHY IT KEEPS LEGAL PAGES THAT THE KNOWLEDGE BASE BLACKLISTS
-----------------------------------------------------------
`agent_knowledge_base.json` has a hard `legal_pages` reject group containing
`/privacy`, `/terms`, `terms-of-service`, `politica-de-privacidade`… That rule
is correct in its original context — V2 only ever asked "is this a student
LOGIN?", and a terms page never is.

V3 asks two questions at once: find the portal AND find the terms governing it.
So applying that group as a drop-rule here would destroy every T&C link before
the legal matcher could see it, and `compliance_metrics` would silently come
back null forever. `legal_pages` is therefore REROUTED: its terms feed the legal
matcher instead of the blacklist. Every other hard group still drops on sight.

Soft groups (`webmail`, `content_and_docs`) are not dropped either — they are
demoted, because a soft rule exists precisely for cases where another signal can
overrule it, and dropping removes that chance.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit

logger = logging.getLogger("genie.filters")

DEFAULT_KB_PATH = Path(__file__).resolve().parents[1] / "agent_knowledge_base.json"

#: Blacklist groups whose terms must NOT drop a link. See module docstring.
_REROUTED_TO_LEGAL = {"legal_pages"}

#: Hard groups that must DEMOTE rather than drop.
#:
#: `infrastructure_hosts` exists in the knowledge base to stop a CDN/PaaS root
#: being whitelisted AS A VENDOR — its own comment says "a portal may sit behind
#: them". Using it as a drop-rule is a different and wrong thing: a university
#: can host its real portal on netlify/vercel/azurewebsites, and Google
#: Workspace mail lives on google.com.
#:
#: Measured on a live crawl before this was changed: 37 of BUET's 42 rejects
#: were netlify.app pages, and UWU's student mail login
#: (google.com/a/uwu.ac.lk, anchor text "Click here to login") was deleted
#: outright. Demoting keeps them visible to the model, which can judge them.
_DEMOTE_NOT_DROP = {"infrastructure_hosts"}

#: Core matchers named in the V3 spec, kept explicit so the filter still works
#: if the knowledge base is missing or trimmed.
_SPEC_BLACKLIST = (
    "wp-admin", "wp-login", "alumni", "career", "careers", "job", "jobs",
    "applicant", "faculty-login", "staff-portal", "staff-login", "admission",
)
_SPEC_PORTAL = (
    "portal", "login", "signin", "sign-in", "sso", "erp", "lms", "moodle",
    "canvas", "ums", "mis", "student", "webmail",
)
_SPEC_LEGAL = (
    "privacy", "terms", "legal", "tnc", "t-and-c", "disclaimer", "policy",
    "policies", "condition", "conditions",
)
#: The KB's category names that correspond to the spec's `erp_ums_mis` and `lms`
#: buckets. Those literal keys do not exist — the taxonomy came from ~8,900
#: human-classified rows and uses these names instead.
_ERP_UMS_MIS_CATEGORIES = ("Student Portal", "Student Information System",
                           "Exam/Results", "Fees", "SSO")
_LMS_CATEGORIES = ("LMS",)


def _guard(term: str) -> str:
    """Regex for `term`, boundary-guarded when a bare substring would misfire.

    'apply' inside 'supply' and 'tos' inside 'photos' are real false positives —
    both terms appear in the knowledge base — so alphanumeric terms get
    letter-boundary guards while terms carrying a '/' or '-' are used verbatim.
    """
    t = term.strip()
    if not t:
        return ""
    if any(ch in t for ch in "/.?=&"):
        return re.escape(t)
    return rf"(?<![a-z0-9]){re.escape(t)}(?![a-z0-9])"


def _compile(terms: Iterable[str]) -> re.Pattern[str] | None:
    parts = [p for p in (_guard(t) for t in dict.fromkeys(terms)) if p]
    if not parts:
        return None
    return re.compile("|".join(parts), re.IGNORECASE)


def normalize_url(url: str) -> str:
    """Dedup key: lowercase host, no trailing slash, no fragment.

    The query string is KEPT — `?m=auth` and `?m=logout` are different pages —
    but a bare '#' fragment is dropped. Note the deliberate exception: a
    fragment that ROUTES a single-page app ('#/login', '#!/login') is part of
    the address, and stripping it breaks tenants like core-campus and vmedulife.
    """
    s = urlsplit(url.strip())
    host = (s.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = (s.path or "/").rstrip("/") or "/"
    frag = s.fragment or ""
    keep_frag = frag if frag.startswith(("/", "!")) else ""
    q = f"?{s.query}" if s.query else ""
    return f"{host}{path}{q}{('#' + keep_frag) if keep_frag else ''}"


class LocalKnowledgeMatrixFilter:
    """Regex/set gate between the crawler and the OpenRouter inference phase."""

    def __init__(
        self,
        kb_path: str | Path = DEFAULT_KB_PATH,
        *,
        max_candidates: int = 30,
        reserve_legal: int = 8,
    ) -> None:
        self.kb_path = Path(kb_path)
        self.max_candidates = max_candidates
        # Legal links score lower than logins by nature, so without a reserved
        # slice a link-rich university fills all 30 slots with portals and the
        # compliance half of the answer is silently lost.
        self.reserve_legal = reserve_legal
        self.kb: dict[str, Any] = self._load_kb()

        bl_terms: list[str] = list(_SPEC_BLACKLIST)
        soft_terms: list[str] = []
        legal_terms: list[str] = list(_SPEC_LEGAL)

        for name, group in (self.kb.get("compliance_exclusion_blacklist") or {}).items():
            if name.startswith("_") or not isinstance(group, dict):
                continue
            terms = list(group.get("match") or [])
            terms += [f".{lbl}." for lbl in (group.get("match_host_label") or [])]
            if name in _REROUTED_TO_LEGAL:
                legal_terms += terms          # keep as legal candidates, do not drop
            elif name in _DEMOTE_NOT_DROP or group.get("severity") == "soft":
                soft_terms += terms           # demote, do not drop
            else:
                bl_terms += terms

        portal_terms: list[str] = list(_SPEC_PORTAL)
        rck = self.kb.get("relevance_classification_keywords") or {}
        for cat in _ERP_UMS_MIS_CATEGORIES + _LMS_CATEGORIES:
            entry = rck.get(cat) or {}
            portal_terms += list(entry.get("url") or [])
            portal_terms += list(entry.get("platform") or [])

        self.blacklist_re = _compile(bl_terms)
        self.soft_re = _compile(soft_terms)
        self.portal_re = _compile(portal_terms)
        self.legal_re = _compile(legal_terms)

        # Known third-party SaaS roots — the strongest single signal we hold.
        self.vendor_roots: dict[str, dict[str, Any]] = {
            e["root"].lower(): e
            for e in (self.kb.get("saas_infra_whitelist") or [])
            if e.get("root")
        }
        self.last_stats: dict[str, int] = {}
        logger.info(
            "filters: %d blacklist / %d soft / %d portal / %d legal terms, %d vendor roots",
            len(bl_terms), len(soft_terms), len(portal_terms), len(legal_terms),
            len(self.vendor_roots),
        )

    # ------------------------------------------------------------------ #
    def _load_kb(self) -> dict[str, Any]:
        try:
            return json.loads(self.kb_path.read_text())
        except FileNotFoundError:
            logger.warning("filters: %s not found — running on built-in terms only",
                           self.kb_path)
            return {}
        except Exception as exc:  # noqa: BLE001
            logger.warning("filters: could not parse %s (%s) — built-in terms only",
                           self.kb_path, type(exc).__name__)
            return {}

    def _vendor_hit(self, host: str) -> dict[str, Any] | None:
        host = host[4:] if host.startswith("www.") else host
        parts = host.split(".")
        for i in range(len(parts) - 1):
            cand = ".".join(parts[i:])
            if cand in self.vendor_roots:
                return self.vendor_roots[cand]
        return None

    # ------------------------------------------------------------------ #
    def filter_and_rank_links(
        self, links: list[dict[str, str]], *, max_candidates: int | None = None
    ) -> list[dict[str, Any]]:
        """Drop noise, dedupe, tag, rank, and cap.

        Returns `[{'url', 'text', 'kind', 'score', 'matched'}]` where `kind` is
        'portal' or 'legal'. Both kinds are preserved so the model can map a
        portal to the terms that govern it in one pass.
        """
        cap = max_candidates if max_candidates is not None else self.max_candidates
        stats = {"in": len(links), "blacklisted": 0, "duplicate": 0,
                 "no_signal": 0, "portal": 0, "legal": 0}

        seen: set[str] = set()
        portals: list[dict[str, Any]] = []
        legals: list[dict[str, Any]] = []

        for item in links:
            url = (item.get("url") or "").strip()
            text = (item.get("text") or "").strip()
            if not url:
                continue
            blob = f"{url} {text}"

            if self.blacklist_re and self.blacklist_re.search(blob):
                stats["blacklisted"] += 1
                continue

            key = normalize_url(url)
            if key in seen:
                stats["duplicate"] += 1
                continue
            seen.add(key)

            host = (urlsplit(url).netloc or "").lower()
            vendor = self._vendor_hit(host)
            is_portal = bool(self.portal_re and self.portal_re.search(blob)) or bool(vendor)
            is_legal = bool(self.legal_re and self.legal_re.search(blob))

            if not (is_portal or is_legal):
                stats["no_signal"] += 1
                continue

            score = 0.0
            matched: list[str] = []
            if vendor:
                score += 5.0
                matched.append(f"vendor:{vendor['root']}")
            if is_portal:
                score += 3.0 if self.portal_re and self.portal_re.search(url) else 1.5
                matched.append("portal-kw")
            if is_legal:
                matched.append("legal-kw")
            if self.soft_re and self.soft_re.search(blob):
                score -= 2.0
                matched.append("soft-demoted")
            if re.search(r"/(login|signin|sso|auth|account)(\.|/|$)", url, re.I):
                score += 2.0
                matched.append("login-path")

            row = {"url": url, "text": text[:200],
                   "kind": "portal" if is_portal else "legal",
                   "score": round(score, 2), "matched": matched}
            # A link can look like both; portal wins the bucket because that is
            # the primary target, but the legal keyword stays in `matched`.
            (portals if is_portal else legals).append(row)

        portals.sort(key=lambda r: -r["score"])
        legals.sort(key=lambda r: -r["score"])

        n_legal = min(len(legals), self.reserve_legal, cap)
        n_portal = min(len(portals), cap - n_legal)
        out = portals[:n_portal] + legals[:n_legal]
        # Backfill if one side was short, so the cap is actually used.
        if len(out) < cap:
            spare = [r for r in portals[n_portal:] + legals[n_legal:]]
            spare.sort(key=lambda r: -r["score"])
            out += spare[: cap - len(out)]

        stats["portal"] = sum(1 for r in out if r["kind"] == "portal")
        stats["legal"] = sum(1 for r in out if r["kind"] == "legal")
        stats["out"] = len(out)
        self.last_stats = stats
        logger.info(
            "filters: %d in -> %d out (%d portal / %d legal); dropped %d blacklist, "
            "%d dup, %d no-signal",
            stats["in"], stats["out"], stats["portal"], stats["legal"],
            stats["blacklisted"], stats["duplicate"], stats["no_signal"],
        )
        return out

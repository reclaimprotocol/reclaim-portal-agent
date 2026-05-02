"""Stage A — Portal Discovery.

Hybrid strategy:

* **Pass 1** (rule-based, deterministic) — `discovery_rules.run_searches`
  (two neutral queries: `<name> student login` and `<domain> student
  portal`), then `run_path_probes` / `run_subdomain_probes` on the
  university's own configured domains. No platform-targeted upfront
  queries or `<shortname>.samarth.edu.in`-style URL guessing — Samarth /
  DigitalUniversity / Knimbus / Cognibot URLs are accepted only when they
  surface organically.
* **Pass 2** (Claude fallback) — triggered only when fewer than 2 candidates
  from Pass 1 survive validation.
* **Category inference** — `discovery_rules.infer_category` re-categorises
  each validated candidate from URL host+path signals.
* **ERP gate** — if a candidate ends up classified as ERP (host has erp/sap
  or path has /erp), it must also have a strong student-facing text signal
  in the page body, otherwise it's dropped as a likely staff/HR system.
* **Consolidation** — `discovery_rules.consolidate_candidates` applies
  Filter 2 (university-wide-only with strong-signal override) and Filter 1
  ((host, category) dedup).

Validation has two fallbacks:

* **JS-render** — when static HTML has no login signal but looks like a JS
  shell (Angular/React/Vue SPA, tiny `<div id="app">` body, login-ish path,
  …), we escalate to a shared Playwright / headless-Chromium renderer and
  re-check signals on the fully-rendered DOM.

* **Student-link-follow** — for any homepage (path == "/" or empty) or any
  URL that needed JS-rendering, we scan the page's anchors for "Student
  Login / Student Portal / Student Access" links (with `score_student_anchor`
  in `discovery_rules`), validate the top-3 (>= score 2), and either
  *replace* the homepage candidate (when the discovered link scores >= 3)
  or *add* the discovered URL as an additional candidate (when it scores 2).
  Cost-bounded by `LINK_FOLLOW_BUDGET` per OrgID.
"""
from __future__ import annotations

import logging
import re
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit, urlunsplit

import requests

from ..anthropic_client import AnthropicClient
from ..config import (
    KNOWN_SHARED_PLATFORM_PATTERNS,
    TOTAL_DISCOVERY_BUDGET_SECONDS,
    load_config,
)
from . import discovery_claude, discovery_rules
from .discovery_rules import Candidate

if TYPE_CHECKING:
    from ..pipeline import PipelineContext
    from .js_renderer import JSRenderer

logger = logging.getLogger(__name__)

UNIVERSITY_NAME_COL = "SheerID University Name"
DOMAINS_COL = "SheerID Website Domain"

FILE_EXTENSIONS: tuple[str, ...] = (
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".svg", ".webp",
    ".zip", ".tar", ".gz", ".rar", ".7z",
    ".mp3", ".mp4", ".mov", ".avi", ".wav",
    ".exe", ".dmg", ".iso",
)

LOGIN_TEXT_PATTERNS: tuple[str, ...] = (
    "username", "user id", "user-id", "userid",
    "roll number", "roll no", "roll-number",
    "enrollment", "enrolment",
    "student id", "student-id", "studentid",
)

# Stronger student-facing text patterns. Used as the ERP gate: if a candidate
# is otherwise classified as ERP but the body has none of these, drop it as
# a likely staff/HR system (real student ERPs all mention at least one of
# these in the visible page).
STUDENT_TEXT_PATTERNS: tuple[str, ...] = (
    "student login", "student portal", "student id", "student account",
    "roll number", "enrollment number", "enrollment no",
    "course registration", "registration form",
    "hall ticket", "exam form",
    "academic calendar",
)

NON_PRODUCTION_HOST_SUBSTRINGS: tuple[str, ...] = (
    "staging", "beta", "test", "dev", "sandbox", "uat", "qa", "preprod",
)

ADMISSION_URL_SUBSTRINGS: tuple[str, ...] = ("admission", "apply")

# Hosts whose subdomain label signals a non-student-facing system. Always
# dropped at validation, regardless of category. Matched against per-label
# (not raw substring) so "harvard" doesn't match "hr".
NON_STUDENT_HOST_LABELS: tuple[str, ...] = (
    "hr", "payroll", "finance", "procurement", "vendor",
)

# Per-OrgID link-follow budget. Shared across rule + claude validation passes.
LINK_FOLLOW_BUDGET_DEFAULT: int = 5

# Password-input detection regex lives in `discovery_rules` so the strict
# rule-A body checks can reuse it without a circular import.
_PASSWORD_INPUT_RE = discovery_rules.PASSWORD_INPUT_RE

# --- Hard verification gate ----------------------------------------------
#
# Every URL written to the sheet has to clear this gate, regardless of how
# it was discovered. Motivation: Stage A previously fabricated URLs by
# guessing platform-tenant subdomains (e.g. `<shortname>.samarth.edu.in`);
# many of those returned a 200 from a wildcard / index page and slipped
# through the looser checks. The gate enforces:
#
#   1. DNS resolves (3s timeout)             — `_dns_resolves_with_timeout`
#   2. HTTP final status ∈ {200, 401, 403}   — `_HARD_GATE_OK_STATUSES`
#   3. Body length > 1000 chars
#   4. Body has <form>, password input, OR an <input> with login/sign-in
#      text within ~200 chars                — `_body_has_interactive_form`
#   5. Final URL is not the bare platform root (samarth.edu.in/, …)
#
# Empty result is acceptable. If verification fails the URL is dropped and
# logged at WARNING.
_HARD_GATE_OK_STATUSES: frozenset[int] = frozenset({200, 401, 403})
_HARD_GATE_REJECT_STATUSES: frozenset[int] = frozenset({404, 410})
_HARD_GATE_MIN_BODY_LEN: int = 1000
_HARD_GATE_DNS_TIMEOUT: float = 3.0

# `<input ...>` followed by up to 200 chars of any character that contains
# either "login" or "sign in"; or "login"/"sign in" up to 200 chars before
# an `<input>` (for label-then-input layouts).
_INPUT_NEAR_LOGIN_RE = re.compile(
    r"(?:<input\b[^>]*>[\s\S]{0,200}?(?:login|sign\s*in)"
    r"|(?:login|sign\s*in)[\s\S]{0,200}?<input\b)",
    re.IGNORECASE,
)


def _dns_resolves_with_timeout(host: str, timeout: float = _HARD_GATE_DNS_TIMEOUT) -> tuple[bool, str]:
    """Resolve `host` to an IP via DNS with a hard wall-clock cap.

    `socket.gethostbyname` itself has no timeout knob and will block on the
    OS resolver. Run it in a daemon thread and join with `timeout`; if the
    thread hasn't returned by then, treat as DNS-failure. Avoids the
    process-global `socket.setdefaulttimeout`, which is not thread-safe.
    """
    if not host:
        return False, "empty host"
    err: list[str] = []

    def _resolve() -> None:
        try:
            socket.gethostbyname(host)
        except Exception as e:  # noqa: BLE001 — surface error class+msg
            err.append(f"{type(e).__name__}: {e}")

    t = threading.Thread(target=_resolve, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        return False, f"DNS timeout (>{timeout}s)"
    if err:
        return False, f"DNS: {err[0]}"
    return True, ""


def _body_has_interactive_form(body: str) -> bool:
    """True if the body looks like a real interactive page — has any of:
      * `<form>` tag
      * password input (`_PASSWORD_INPUT_RE`)
      * an `<input>` element with "login" / "sign in" within ~200 chars
    """
    if not body:
        return False
    if _PASSWORD_INPUT_RE.search(body):
        return True
    body_lower = body.lower()
    if "<form" in body_lower:
        return True
    if _INPUT_NEAR_LOGIN_RE.search(body):
        return True
    return False


def _is_bare_platform_root(final_url: str) -> tuple[bool, str]:
    """True if `final_url` is the bare host of a known shared platform —
    i.e. discovery resolved to `https://samarth.edu.in/` instead of staying
    on an institutional subdomain."""
    p = urlsplit(final_url or "")
    host = p.netloc.lower().split(":")[0]
    if not host:
        return False, ""
    if host in KNOWN_SHARED_PLATFORM_PATTERNS:
        return True, f"redirected to bare platform root {host}"
    return False, ""


@dataclass
class _ValResult:
    ok: bool
    final_url: str
    notes: str = ""
    has_password_input: bool = False
    has_login_text: bool = False
    has_student_signal: bool = False
    js_rendered: bool = False
    js_attempted: bool = False
    body: str = ""  # captured for link-follow on homepages / js-rendered URLs
    # When set, the parallel validation path detected a JS shell but did
    # not call Playwright (Playwright's sync API is bound to its creating
    # thread). The serial post-phase handles js-render on these.
    js_render_pending: bool = False


@dataclass
class _LinkFollowBudget:
    remaining: int


@dataclass
class _DiscoveryBudget:
    """Tracks total wall-clock time spent in Stage A. When exceeded, the
    pipeline returns whatever has been validated so far instead of running
    further phases. Logged once when first tripped."""
    deadline_at: float
    tripped: bool = False

    def remaining(self) -> float:
        return max(0.0, self.deadline_at - time.monotonic())

    def expired(self) -> bool:
        return time.monotonic() >= self.deadline_at

    def trip(self, orgid: str, where: str, partial: int) -> None:
        if self.tripped:
            return
        self.tripped = True
        logger.warning(
            "[%s] discovery budget exceeded at phase=%s; returning %d partial candidates",
            orgid, where, partial,
        )


# Max parallelism for candidate validation. 10 keeps load well below the
# session pool ceiling (20) while saturating most slow Indian-uni hosts.
_VALIDATE_MAX_WORKERS: int = 10


def run(ctx: "PipelineContext") -> dict[str, Any]:
    config = load_config()
    orgid = ctx.orgid
    row = ctx.row
    js_renderer: "JSRenderer | None" = ctx.deps.get("js_renderer")

    name = str(row.get(UNIVERSITY_NAME_COL, "")).strip()
    domains = discovery_rules.parse_domains(str(row.get(DOMAINS_COL, "")))

    if not name:
        logger.warning("[%s] discovery: row missing %r; aborting", orgid, UNIVERSITY_NAME_COL)
        return {"portals": [], "reason": "missing university name"}
    if not domains:
        logger.warning("[%s] discovery: row missing any domain in %r; aborting", orgid, DOMAINS_COL)
        return {"portals": [], "reason": "no domains"}

    primary = domains[0]
    overrides = config.domain_overrides.get(str(orgid), {}) or {}
    extra_allowed_labels = [str(x) for x in overrides.get("extra_allowed_subdomains", []) if x]
    extra_allowed_roots = [
        str(x).lower().lstrip(".")
        for x in overrides.get("extra_allowed_root_domains", [])
        if x
    ]
    # `extra_effective_domains`: per-OrgID secondary university-owned
    # domains that SheerID's row doesn't list. Treated as full peers of the
    # primary — they get search queries, path probes, subdomain probes, and
    # off-domain validation allow-listing. Used for universities whose
    # student portals live on a separate owned domain (e.g. HPU Shimla:
    # primary `hpuniv.ac.in`, portals on `hpushimla.in`).
    extra_effective_domains = [
        str(x).lower().lstrip(".")
        for x in overrides.get("extra_effective_domains", [])
        if x
    ]
    # Bug 29/30 strict membership inputs. `state` selects the OrgID's
    # state-platform host set from `STATE_PLATFORM_HINTS`;
    # `exact_shortnames` is the exhaustive list of acceptable tenant-
    # subdomain prefixes when the host is on a state-platform or samarth
    # root. Empty/missing → rules (3)/(4) of `host_belongs_to_org` reject
    # any URL on those platform roots, preserving the cross-contamination
    # invariant.
    org_state: str | None = (
        str(overrides["state"]).strip() if overrides.get("state") else None
    )
    exact_shortnames: list[str] = [
        str(x).lower().strip()
        for x in overrides.get("exact_shortnames", [])
        if x
    ]

    # Full set of university-owned domains: SheerID-listed `domains` plus
    # any per-OrgID `extra_effective_domains`. Drives searches, path probes,
    # subdomain probes, and the validation allow-list.
    owned_domains: list[str] = list(domains)
    for d in extra_effective_domains:
        if d and d not in owned_domains:
            owned_domains.append(d)

    shortname_candidates = discovery_rules.extract_shortname_candidates(
        owned_domains, extra_allowed_roots
    )
    acronym = discovery_rules.compute_acronym(name)
    acronym_candidates: frozenset[str] = frozenset({acronym}) if acronym else frozenset()

    # `effective_domains` is the allow-list used by validation/consolidation
    # to keep candidates on-domain. Platform roots (samarth.edu.in / …) are
    # NOT added: they're not the university's domains, and the only legit
    # way for a platform URL to enter the result is for organic search to
    # have surfaced it (which then passes via `host_is_known_shared_platform`
    # in the off-domain checks, not via this allow-list).
    effective_domains = list(owned_domains)
    for r in extra_allowed_roots:
        if r not in effective_domains:
            effective_domains.append(r)

    logger.info(
        "[%s] discovery starting: name=%r primary=%s domains=%s extra_effective=%s shortnames=%s acronym=%s effective=%s",
        orgid, name, primary, domains, extra_effective_domains,
        sorted(shortname_candidates), acronym, effective_domains,
    )

    t0 = time.monotonic()
    budget = _DiscoveryBudget(deadline_at=t0 + TOTAL_DISCOVERY_BUDGET_SECONDS)

    # Shared link-follow budget across rule + claude passes.
    link_follow_budget = _LinkFollowBudget(remaining=LINK_FOLLOW_BUDGET_DEFAULT)

    # ---- Phase: search (neutral DDG queries) -------------------------------
    # Two neutral query templates — `<name> student login` and `<domain>
    # student portal` — with the domain query fanned out across every
    # university-owned domain (SheerID-listed plus `extra_effective_domains`).
    # Platform-targeted queries (Samarth / DU / Knimbus / Cognibot) are
    # intentionally absent; if a platform URL is the right answer, these
    # neutral queries will surface it organically.
    t_phase = time.monotonic()
    rule_candidates: list[Candidate] = list(discovery_rules.run_searches(
        name,
        domains=owned_domains,
        max_results_per_query=config.discovery_max_results_per_query,
        http_timeout=config.http_timeout_seconds,
        user_agent=config.user_agent,
    ))
    logger.info(
        "[%s] phase=search took=%.1fs candidates=%d",
        orgid, time.monotonic() - t_phase, len(rule_candidates),
    )

    # ---- Phase: path + subdomain probes on the university's OWN domains ----
    # Both probe types target `*.{owned_domain}` only — primary plus any
    # secondary SheerID-listed domains plus `extra_effective_domains`. No
    # `<shortname>.samarth.edu.in`-style guessing across platform roots.
    t_phase = time.monotonic()
    # Subdomain probes accept a primary plus extras; pass the rest of
    # `owned_domains` plus the looser allowed-roots so extras-rooted
    # subdomains (hallticket / lib / certificate / …) are probed too.
    extra_subdomain_targets = owned_domains[1:] + extra_allowed_roots
    with ThreadPoolExecutor(max_workers=2) as exe:
        f_path = exe.submit(
            discovery_rules.run_path_probes,
            owned_domains,
            http_timeout=config.http_timeout_seconds,
            user_agent=config.user_agent,
        )
        # Probe rooted domains too, not just the primary. SheerID configures
        # some rows with a sub-domain as primary (e.g. SPPU → primary
        # `pun.unipune.ac.in`); the canonical subdomains
        # (hallticket / lib / certificate) live under the root (`unipune.ac.in`).
        f_sub = exe.submit(
            discovery_rules.run_subdomain_probes,
            primary,
            http_timeout=config.http_timeout_seconds,
            user_agent=config.user_agent,
            extra_domains=extra_subdomain_targets,
        )
        rule_candidates.extend(f_path.result())
        rule_candidates.extend(f_sub.result())
    rule_candidates = _dedupe(rule_candidates)
    logger.info(
        "[%s] phase=probes took=%.1fs candidates=%d",
        orgid, time.monotonic() - t_phase, len(rule_candidates),
    )

    # ---- Phase: Bug 22/23 sibling-domain discovery -------------------------
    # Walk the primary domain's homepage anchors. Strict portal-anchored
    # links (anchor text full-matches "Student Portal" / "Examination
    # Portal" / etc.) become direct candidates. Every non-blocklisted
    # external host on the primary homepage AND every host returned by
    # the DDG name-based search is treated as a "sibling host" — it gets
    # added to `effective_domains` so URLs on it pass off-domain
    # validation, and SUBDOMAIN_PROBE_LIST gets probed against it
    # (one-hop only). This is what surfaces commercial-TLD university
    # portals like `nsoucebdp.com`, `pcdpcal.com` (NSOU), `bihar-ums.com`,
    # `hpushimla.in` etc. that the homepage may or may not link to
    # directly.
    t_phase = time.monotonic()
    sibling_hosts: set[str] = set()
    # Bug 30 — hosts that came in via a strict portal-pattern anchor on the
    # primary homepage. These are the only sibling hosts that satisfy
    # `host_belongs_to_org` rule (5), so we track them separately from the
    # broader `sibling_hosts` set.
    portal_anchored_hosts: set[str] = set()
    portal_anchor_count = 0
    homepage_walked = False
    homepage_fetch = discovery_rules.fetch_homepage_for_sibling_walk(
        primary,
        http_timeout=config.http_timeout_seconds,
        user_agent=config.user_agent,
    )
    if homepage_fetch is not None:
        homepage_walked = True
        homepage_url, homepage_html = homepage_fetch
        sib_result = discovery_rules.extract_sibling_domains_from_homepage(
            homepage_html,
            base_url=homepage_url,
            primary_host=primary,
        )
        for url, anchor_text in sib_result.portal_anchors:
            rule_candidates.append(Candidate(
                url=url,
                category="Student Portal",
                discovery_source="rule:sibling-anchor",
                discovery_reasoning=(
                    f"sibling anchor on {primary}: text={anchor_text!r}"
                ),
            ))
            portal_anchor_count += 1
            anchor_host = urlsplit(url).netloc.lower().split(":")[0]
            if anchor_host:
                portal_anchored_hosts.add(anchor_host)
        # Bug 30 strict membership filter — only keep sibling hosts that
        # belong to this OrgID via one of rules (1)–(5). Replaces the
        # earlier blanket-add (which let unrelated state-UMS subdomains
        # leak into `effective_domains`).
        for host in sib_result.sibling_hosts:
            if host in owned_domains or host in extra_allowed_roots:
                continue
            ok, reason = discovery_rules.host_belongs_to_org(
                host,
                primary=primary,
                extra_effective_domains=extra_effective_domains,
                state=org_state,
                exact_shortnames=exact_shortnames,
                portal_anchored_hosts=portal_anchored_hosts,
            )
            if not ok:
                logger.warning(
                    "[%s] membership REJECTED %s: %s",
                    orgid, host, reason,
                )
                continue
            sibling_hosts.add(host)
        logger.info(
            "[%s] sibling-walk on %s: %d portal-anchored URLs, %d sibling hosts: %s",
            orgid, primary, portal_anchor_count, len(sib_result.sibling_hosts),
            sorted(sib_result.sibling_hosts),
        )
    else:
        logger.info(
            "[%s] sibling-walk on %s: homepage fetch failed; skipping",
            orgid, primary,
        )

    # Also harvest hosts from DDG-origin candidates (the rule-based
    # search already ran above). DDG hits are NOT trusted unconditionally:
    # the name-based query (`<name> student login`) routinely returns
    # same-shortname-different-university matches (e.g. NSUT vs NSOU,
    # PUP→Punjabi vs PU→Patna). Bug 30 replaces the earlier shortname-
    # fuzzy overlap with the strict per-OrgID membership rule
    # (`discovery_rules.host_belongs_to_org`): a DDG-origin host enters
    # `sibling_hosts` only via rule (1)/(2)/(3)/(4)/(5). State-platform
    # and samarth tenants must strictly match `exact_shortnames`; hosts
    # without a configured override generally fail and stay off-domain.
    ddg_origin_hosts: set[str] = set()
    ddg_rejected_hosts: dict[str, str] = {}
    for c in rule_candidates:
        if not c.discovery_source.startswith("rule"):
            continue
        host = urlsplit(c.url).netloc.lower().split(":")[0]
        if not host:
            continue
        if host in owned_domains or host in extra_allowed_roots:
            continue
        if discovery_rules.host_in_external_blocklist(host):
            continue
        if any(host == s or host.endswith("." + s) for s in sibling_hosts):
            continue
        ok, reason = discovery_rules.host_belongs_to_org(
            host,
            primary=primary,
            extra_effective_domains=extra_effective_domains,
            state=org_state,
            exact_shortnames=exact_shortnames,
            portal_anchored_hosts=portal_anchored_hosts,
        )
        if ok:
            ddg_origin_hosts.add(host)
        else:
            ddg_rejected_hosts[host] = reason
    if ddg_origin_hosts:
        logger.info(
            "[%s] sibling-walk: %d DDG-origin hosts admitted via strict "
            "membership: %s",
            orgid, len(ddg_origin_hosts), sorted(ddg_origin_hosts),
        )
        sibling_hosts.update(ddg_origin_hosts)
    for host, reason in ddg_rejected_hosts.items():
        logger.warning(
            "[%s] membership REJECTED %s: %s", orgid, host, reason,
        )

    # Add sibling hosts to `effective_domains` so the off-domain
    # validation filter accepts URLs on them. The strict A/B/C gate
    # (Bug 19) and audience classifier (Bug 20) still apply per-URL.
    if sibling_hosts:
        for h in sorted(sibling_hosts):
            if h not in effective_domains:
                effective_domains.append(h)
        logger.info(
            "[%s] sibling-walk: effective_domains expanded with %d sibling hosts",
            orgid, len(sibling_hosts),
        )

    # Bug 23 — probe SUBDOMAIN_PROBE_LIST against each sibling host's
    # registrable root. One hop only: we don't recurse into siblings of
    # siblings. We probe roots (e.g. `nsouict.ac.in`) rather than the
    # exact host that the homepage anchor pointed at (`www.nsouict.ac.in`),
    # because SUBDOMAIN_PROBE_LIST is a list of leftmost labels — probing
    # `student.www.nsouict.ac.in` would never resolve, but probing
    # `student.nsouict.ac.in` / `lms.nsouict.ac.in` etc. does.
    sibling_roots: set[str] = set()
    for h in sibling_hosts:
        root = discovery_rules.registrable_root(h)
        if not root:
            continue
        if root in owned_domains:
            continue
        if discovery_rules.host_in_external_blocklist(root):
            continue
        sibling_roots.add(root)
    if sibling_roots:
        sib_probe_count_before = len(rule_candidates)
        rule_candidates.extend(
            discovery_rules.run_subdomain_probes(
                "",  # primary intentionally blank — only extras
                http_timeout=config.http_timeout_seconds,
                user_agent=config.user_agent,
                extra_domains=sorted(sibling_roots),
            )
        )
        rule_candidates = _dedupe(rule_candidates)
        logger.info(
            "[%s] sibling-probe: SUBDOMAIN_PROBE_LIST × %d sibling roots %s; "
            "candidates %d → %d",
            orgid, len(sibling_roots), sorted(sibling_roots),
            sib_probe_count_before, len(rule_candidates),
        )
        # Sibling roots should also pass off-domain validation.
        for r in sorted(sibling_roots):
            if r not in effective_domains:
                effective_domains.append(r)

    logger.info(
        "[%s] phase=sibling_walk took=%.1fs candidates=%d sibling_hosts=%d homepage_walked=%s",
        orgid, time.monotonic() - t_phase, len(rule_candidates),
        len(sibling_hosts), homepage_walked,
    )

    # ---- Phase: same-host student-login probes (parallel) ------------------
    # Restrict to hosts on the university's own domains OR confirmed sibling
    # hosts. Shared-platform tenants (samarth.edu.in / digitaluniversity.ac /
    # …) follow their own URL conventions and don't host
    # `/Login/Login/StudentLogin`-shaped paths.
    t_phase = time.monotonic()
    seed_hosts: set[str] = set()
    sibling_match_targets = list(owned_domains) + sorted(sibling_hosts)
    for c in rule_candidates:
        host = urlsplit(c.url).netloc.lower().split(":")[0]
        if host and _host_matches_domains(host, sibling_match_targets):
            seed_hosts.add(host)
    rule_candidates.extend(
        discovery_rules.run_same_host_student_probes(
            seed_hosts,
            http_timeout=config.http_timeout_seconds,
            user_agent=config.user_agent,
        )
    )
    rule_candidates = _dedupe(rule_candidates)
    logger.info(
        "[%s] phase=same_host_probes took=%.1fs candidates=%d hosts=%d",
        orgid, time.monotonic() - t_phase, len(rule_candidates), len(seed_hosts),
    )

    # ---- Phase: pre-validation filtering -----------------------------------
    # Apply cheap rule-based rejections (non-prod, file extensions, off-domain,
    # admission, non-student-host) BEFORE the expensive HTTP validation step
    # so we don't burn fetches on candidates we'd reject anyway.
    t_phase = time.monotonic()
    pre_n = len(rule_candidates)
    pre_filtered = _pre_validation_filter(
        rule_candidates, domains=effective_domains, orgid=orgid,
    )
    logger.info(
        "[%s] phase=filtering took=%.1fs before=%d after=%d",
        orgid, time.monotonic() - t_phase, pre_n, len(pre_filtered),
    )

    # ---- Phase: validation (parallel HTTP + JS-render fallback) ------------
    if budget.expired():
        budget.trip(orgid, "before-validation", 0)
        rule_validated: list[Candidate] = []
    else:
        t_phase = time.monotonic()
        rule_validated = _validate_candidates(
            pre_filtered,
            domains=effective_domains,
            http_timeout=config.http_timeout_seconds,
            user_agent=config.user_agent,
            orgid=orgid,
            js_renderer=js_renderer,
            js_suspicion_threshold=config.js_rendering_suspicion_threshold,
            link_follow_budget=link_follow_budget,
            budget=budget,
        )
        logger.info(
            "[%s] phase=validation took=%.1fs validated=%d",
            orgid, time.monotonic() - t_phase, len(rule_validated),
        )

    logger.info(
        "[%s] discovery rule-pass found %d candidates, %d passed validation",
        orgid, len(rule_candidates), len(rule_validated),
    )

    all_validated: list[Candidate] = list(rule_validated)

    # ---- Pass 2: Claude fallback ------------------------------------------
    if len(rule_validated) < 2 and not config.enable_claude_fallback:
        logger.info(
            "[%s] claude fallback disabled, accepting rule-pass result (%d portals)",
            orgid, len(rule_validated),
        )
    elif len(rule_validated) < 2 and not budget.expired():
        logger.info(
            "[%s] discovery triggering Claude fallback (only %d portals found)",
            orgid, len(rule_validated),
        )
        anthropic = AnthropicClient(
            api_key=config.anthropic_api_key,
            model=config.anthropic_model,
        )
        claude_raw = discovery_claude.run_claude_fallback(
            anthropic=anthropic,
            model=config.discovery_model,
            university_name=name,
            domains=effective_domains,
            known_portals=rule_validated,
            max_uses=config.discovery_claude_max_uses,
        )
        known_keys = {_normalize_for_dedup(c.url) for c in rule_validated}
        claude_candidates = [
            c for c in claude_raw if _normalize_for_dedup(c.url) not in known_keys
        ]
        claude_filtered = _pre_validation_filter(
            claude_candidates, domains=effective_domains, orgid=orgid,
        )
        claude_validated = _validate_candidates(
            claude_filtered,
            domains=effective_domains,
            http_timeout=config.http_timeout_seconds,
            user_agent=config.user_agent,
            orgid=orgid,
            js_renderer=js_renderer,
            js_suspicion_threshold=config.js_rendering_suspicion_threshold,
            link_follow_budget=link_follow_budget,
            budget=budget,
        )
        logger.info(
            "[%s] discovery claude-pass added %d candidates, %d passed validation",
            orgid, len(claude_candidates), len(claude_validated),
        )
        all_validated.extend(claude_validated)

    all_validated = _dedupe(all_validated)

    # ---- Category inference + ERP gate -------------------------------------
    recategorised: list[Candidate] = []
    for c in all_validated:
        # Don't auto-preserve "ERP" from a search-query origin (the
        # "{name} ERP student login" query). If the URL itself doesn't
        # match any infer_category rule that returns ERP, demote the
        # fallback so the ERP gate below doesn't drop a perfectly good
        # Examination/Other candidate just because it came in via the
        # ERP-themed search query.
        fallback = "Other" if c.category == "ERP" else c.category
        new_cat = discovery_rules.infer_category(c.url, fallback=fallback)
        # ERP gate: ERP candidates must have a strong student-facing signal
        # in the page body, otherwise they're dropped as likely staff/HR.
        if new_cat == "ERP" and not c.has_student_signal:
            logger.info(
                "[%s] validate DROP %s — ERP with no student-facing signals (likely staff/HR system)",
                orgid, c.url,
            )
            continue
        if new_cat != c.category:
            logger.info(
                "[%s] recategorise %s: %s → %s",
                orgid, c.url, c.category, new_cat,
            )
            recategorised.append(replace(c, category=new_cat))
        else:
            recategorised.append(c)

    # ---- Consolidation (Filter 2 + (host, category) dedup) -----------------
    # `extra_allowed_root_domains` here is the union of:
    #   * SheerID-row's domain_overrides `extra_allowed_root_domains` entry
    #   * Bug 22 sibling hosts (homepage anchors + DDG-origin)
    # Without the sibling hosts, Filter 2's host-allow-list check rejects
    # otherwise-valid candidates as "off-domain: <sibling host>" — the
    # exact regression seen on NSOU where lms.nsouict.ac.in /
    # portalscll.nsouict.ac.in / nsoucebdp.com all passed validation but
    # got dropped at consolidation.
    consolidate_extra_roots = list(extra_allowed_roots)
    for h in sorted(sibling_hosts):
        if h not in consolidate_extra_roots:
            consolidate_extra_roots.append(h)
    # Also include sibling roots — `lms.nsouict.ac.in` is a sibling host,
    # but its registrable root `nsouict.ac.in` should also be in the
    # allow-list so peer subdomains discovered via Bug 23 (e.g.
    # `portalscll.nsouict.ac.in`) aren't dropped at consolidation.
    for r in sorted(sibling_roots):
        if r not in consolidate_extra_roots:
            consolidate_extra_roots.append(r)
    consolidated = discovery_rules.consolidate_candidates(
        recategorised,
        allowed_domains=owned_domains,
        extra_allowed_subdomains=extra_allowed_labels,
        extra_allowed_root_domains=consolidate_extra_roots,
        shortname_candidates=shortname_candidates,
        acronym_candidates=acronym_candidates,
        # Bug 30 — strict per-OrgID membership re-check (belt-and-
        # suspenders with the sibling-walk filter above).
        primary_domain=primary,
        extra_effective_domains=extra_effective_domains,
        state=org_state,
        exact_shortnames=exact_shortnames,
        portal_anchored_hosts=portal_anchored_hosts,
        orgid=orgid,
    )
    logger.info(
        "[%s] consolidated: %d candidates → %d final",
        orgid, len(recategorised), len(consolidated),
    )

    for c, score in consolidated:
        logger.info(
            "  - [%s] %s (%s, source=%s, score=%d, js_rendered=%s)",
            orgid, c.url, c.category, c.discovery_source, score, c.js_rendered,
        )

    total = time.monotonic() - t0
    logger.info(
        "[%s] discovery total took=%.1fs candidates_kept=%d budget_tripped=%s",
        orgid, total, len(consolidated), budget.tripped,
    )

    return {
        "portals": [
            {
                "url": c.url,
                "category": c.category,
                "discovery_source": c.discovery_source,
                "discovery_reasoning": c.discovery_reasoning,
                "validation_notes": c.validation_notes,
                "consolidation_score": score,
                "js_rendered": c.js_rendered,
            }
            for c, score in consolidated
        ],
        "university_name": name,
        "domains": domains,
        "completed_with_timeout": budget.tripped,
    }


# ---- validation -----------------------------------------------------------


def _pre_validation_filter(
    candidates: list[Candidate],
    *,
    domains: list[str],
    orgid: str,
) -> list[Candidate]:
    """Apply rule-based rejections before the expensive HTTP-validation step.

    Filters in order:
      1. dedup by normalized URL
      2. file extension reject
      3. non-production host reject (staging/beta/test/...)
      4. admission URL reject
      5. non-student host label reject (hr/payroll/finance/...)
      6. off-domain reject (host not under any allowed effective domain)

    Strong-signal override / college-specific subdomain (Filter 2) and the
    final (host, category) dedup (Filter 1) still run post-validation in
    `consolidate_candidates` — those need has_password_input which is only
    known after the fetch.
    """
    out: list[Candidate] = []
    seen: set[str] = set()
    for c in candidates:
        key = _normalize_for_dedup(c.url)
        if key in seen:
            continue
        seen.add(key)

        url = c.url
        path_lower = urlsplit(url).path.lower()
        if path_lower.endswith(FILE_EXTENSIONS):
            logger.debug("[%s] pre-filter DROP %s — file extension", orgid, url)
            continue
        nonprod = _non_production_match(url)
        if nonprod is not None:
            logger.debug("[%s] pre-filter DROP %s — non-production: %s", orgid, url, nonprod)
            continue
        if _is_admission_url(url):
            logger.debug("[%s] pre-filter DROP %s — admission URL", orgid, url)
            continue
        nonstudent = _non_student_label_match(url)
        if nonstudent is not None:
            logger.debug("[%s] pre-filter DROP %s — non-student label: %s", orgid, url, nonstudent)
            continue
        host = urlsplit(url).netloc.lower().split(":")[0]
        if (
            host
            and not _host_matches_domains(host, domains)
            and not discovery_rules.host_is_known_shared_platform(host)
        ):
            logger.debug("[%s] pre-filter DROP %s — off-domain", orgid, url)
            continue
        out.append(c)
    return out


def _validate_candidates(
    candidates: list[Candidate],
    *,
    domains: list[str],
    http_timeout: float,
    user_agent: str,
    orgid: str,
    js_renderer: "JSRenderer | None" = None,
    js_suspicion_threshold: int = 2,
    link_follow_budget: _LinkFollowBudget | None = None,
    budget: _DiscoveryBudget | None = None,
) -> list[Candidate]:
    seen: set[str] = set()
    out: list[Candidate] = []
    body_by_url: dict[str, str] = {}
    js_rendered_urls: set[str] = set()

    # Every candidate — including ones whose host matches a known shared
    # platform (samarth / digitaluniversity / knimbus / cognibot / …) —
    # must clear the hard verification gate. The previous fast-path
    # short-circuit bypassed validation, which combined with the platform-
    # subdomain probing fabricated URLs that were accepted unverified.
    to_validate: list[Candidate] = []
    for c in candidates:
        key = _normalize_for_dedup(c.url)
        if key in seen:
            logger.debug("[%s] validate SKIP %s — dedup", orgid, c.url)
            continue
        seen.add(key)
        to_validate.append(c)

    # ---- Parallel HTTP validation -------------------------------------
    # ThreadPoolExecutor parallelises the HEAD/GET phase. JS-render
    # escalation is *deferred* in workers (Playwright's sync API is bound
    # to its creating thread); deferred-pending results are processed
    # serially in the main thread immediately after the parallel phase.
    pending_js: list[tuple[Candidate, _ValResult]] = []
    if to_validate:
        max_workers = min(_VALIDATE_MAX_WORKERS, len(to_validate))
        with ThreadPoolExecutor(max_workers=max_workers) as exe:
            futures = {
                exe.submit(
                    _validate_one,
                    c.url,
                    domains=domains,
                    http_timeout=http_timeout,
                    user_agent=user_agent,
                    js_renderer=js_renderer,
                    js_suspicion_threshold=js_suspicion_threshold,
                    orgid=orgid,
                    defer_js_render=True,
                ): c
                for c in to_validate
            }
            for fut in as_completed(futures):
                c = futures[fut]
                if budget is not None and budget.expired():
                    budget.trip(orgid, "validation", len(out))
                    # Cancel any not-yet-started futures.
                    for f in futures:
                        if not f.done():
                            f.cancel()
                    break
                try:
                    r = fut.result()
                except Exception as err:
                    logger.warning(
                        "[%s] validate ERROR %s — %s: %s",
                        orgid, c.url, type(err).__name__, err,
                    )
                    continue
                if r.js_render_pending:
                    pending_js.append((c, r))
                    continue
                if not r.ok:
                    logger.info("[%s] validate DROP %s — %s", orgid, c.url, r.notes)
                    continue
                logger.info("[%s] validate KEEP %s — %s", orgid, r.final_url, r.notes or "ok")

                kept = replace(
                    c,
                    url=r.final_url,
                    discovery_source=c.discovery_source,
                    validation_notes=r.notes,
                    has_password_input=r.has_password_input,
                    has_login_text=r.has_login_text,
                    has_student_signal=r.has_student_signal,
                    js_rendered=False,
                )
                out.append(kept)

                if r.body and discovery_rules.is_homepage_url(r.final_url):
                    body_by_url[r.final_url] = r.body

    # ---- Serial JS-render escalation -----------------------------------
    # Playwright's sync API only works from the thread that created it.
    # Run all deferred render() calls here, in the main thread, in order.
    if js_renderer is not None and pending_js:
        for c, pending in pending_js:
            if budget is not None and budget.expired():
                budget.trip(orgid, "js-render", len(out))
                break
            final_url = pending.final_url
            logger.info(
                "[%s] js-render escalate (serial): %s",
                orgid, final_url,
            )
            rendered = js_renderer.render(final_url)
            if not rendered.ok:
                logger.info(
                    "[%s] validate DROP via js-render %s — js-render failed: %s",
                    orgid, final_url, rendered.error,
                )
                continue
            rbody = rendered.html or ""
            rbody_lower = rbody.lower()
            has_pw_js = bool(_PASSWORD_INPUT_RE.search(rbody))
            has_tx_js = any(pat in rbody_lower for pat in LOGIN_TEXT_PATTERNS)
            has_student_js = any(pat in rbody_lower for pat in STUDENT_TEXT_PATTERNS)
            rendered_final = discovery_rules.strip_session_ids(
                rendered.final_url or final_url
            )
            gate_ok_js, gate_reason_js = discovery_rules.passes_login_signal_gate(
                final_url=rendered_final,
                has_password=has_pw_js,
                has_text=has_tx_js,
            )
            if not gate_ok_js:
                logger.info(
                    "[%s] validate DROP via js-render %s — %s",
                    orgid, rendered_final, gate_reason_js,
                )
                continue
            signals: list[str] = []
            if has_pw_js:
                signals.append("password-input")
            if has_tx_js:
                signals.append("login-text")
            notes = f"signals: {','.join(signals)} (js-rendered); {gate_reason_js}"
            logger.info(
                "[%s] validate KEEP via js-render: %s — %s",
                orgid, rendered_final, notes,
            )
            new_source = c.discovery_source
            if "+js-render" not in new_source:
                new_source = new_source + "+js-render"
            kept = replace(
                c,
                url=rendered_final,
                discovery_source=new_source,
                validation_notes=notes,
                has_password_input=has_pw_js,
                has_login_text=has_tx_js,
                has_student_signal=has_student_js,
                js_rendered=True,
            )
            out.append(kept)
            body_by_url[rendered_final] = rbody
            js_rendered_urls.add(rendered_final)

    # ---- Student-link-follow pass --------------------------------------
    if link_follow_budget is not None and link_follow_budget.remaining > 0 and body_by_url:
        out = _follow_student_links(
            kept=out,
            body_by_url=body_by_url,
            js_rendered_urls=js_rendered_urls,
            seen_keys=seen,
            domains=domains,
            http_timeout=http_timeout,
            user_agent=user_agent,
            orgid=orgid,
            js_renderer=js_renderer,
            js_suspicion_threshold=js_suspicion_threshold,
            budget=link_follow_budget,
        )

    # ---- Bug 9 homepage → specific-login-URL pass ----------------------
    # For any kept candidate whose URL is still a bare host, scan the
    # homepage anchors for login-shaped links (`/user/login`, `/login`,
    # "User Login", "Member Login", etc.) and replace the bare host with
    # the validated specific login URL. Distinct from `_follow_student_links`
    # which only triggered on student-strong (≥2) anchors.
    if link_follow_budget is not None and link_follow_budget.remaining > 0 and body_by_url:
        out = _resolve_homepage_to_login_url(
            kept=out,
            body_by_url=body_by_url,
            seen_keys=seen,
            domains=domains,
            http_timeout=http_timeout,
            user_agent=user_agent,
            orgid=orgid,
            js_renderer=js_renderer,
            js_suspicion_threshold=js_suspicion_threshold,
            budget=link_follow_budget,
        )

    # ---- Bug 19 post-link-follow filter --------------------------------
    # Rule-B candidates that link-follow couldn't upgrade still sit in
    # `out` as homepage URLs with `has_password_input=False`. Those are
    # the false positives the strict A/B/C gate is supposed to catch
    # (e.g. `result.pup.ac.in/` — a result-display page with no login
    # form, just a "Roll Number" input). Drop them with a WARNING.
    # Known shared-platform hosts (rule C) are always preserved — their
    # homepages are real portals even when the static body has no
    # password input.
    final_out: list[Candidate] = []
    for c in out:
        if c.has_password_input:
            final_out.append(c)
            continue
        host = urlsplit(c.url).netloc.lower().split(":")[0]
        if discovery_rules.host_is_known_shared_platform(host):
            final_out.append(c)
            continue
        if not discovery_rules.is_homepage_url(c.url):
            # Non-homepage URL with no password input shouldn't have
            # passed the new gate at all; if one does (e.g. someone
            # bypasses validation), let it through here rather than
            # silently drop.
            final_out.append(c)
            continue
        logger.warning(
            "[%s] REJECTED %s: no login form, no login redirect, not on known platform",
            orgid, c.url,
        )
    return final_out


def _follow_student_links(
    *,
    kept: list[Candidate],
    body_by_url: dict[str, str],
    js_rendered_urls: set[str],
    seen_keys: set[str],
    domains: list[str],
    http_timeout: float,
    user_agent: str,
    orgid: str,
    js_renderer: "JSRenderer | None",
    js_suspicion_threshold: int,
    budget: _LinkFollowBudget,
) -> list[Candidate]:
    """Scan the bodies of homepage / js-rendered candidates for "Student Login"-
    style anchors, validate the top scorers, and merge them into `kept`.

    `kept` is mutated and returned. Strong-scored (>= 3) discoveries replace
    the originating homepage candidate **iff the parent has no password
    input of its own** (i.e. rule B in `passes_login_signal_gate` —
    homepage-with-only-login-text); when the parent already has a password
    input it's a real login page and discovered links are only ADDED, never
    replacing the parent. (Bug surfaced on HPU Shimla: the new portal
    `nstudentportal.hpushimla.in/` had an anchor labelled "Click here to
    access the old student portal!" which scored 8 against the
    `student-portal` strong phrase and replaced the new portal with the
    old.)
    """
    extras: list[Candidate] = []
    replaced_urls: set[str] = set()

    for parent in list(kept):
        if budget.remaining <= 0:
            break
        url = parent.url
        is_eligible = discovery_rules.is_homepage_url(url) or url in js_rendered_urls
        if not is_eligible:
            continue
        body = body_by_url.get(url)
        if not body:
            continue
        # Parents with their own password input are already real login pages;
        # link-follow will still scan their anchors (handy for sibling
        # portals like `lib.../user/login`) but the discoveries are added,
        # not used to replace the parent.
        parent_replaceable = not parent.has_password_input

        links = discovery_rules.extract_top_student_links(
            body, base_url=url, max_n=3, min_score=2,
        )
        if not links:
            continue
        logger.info(
            "[%s] homepage student-link discovery: scoring %d anchors on %s",
            orgid, len(links), url,
        )

        for link_url, score, anchor_text in links:
            if budget.remaining <= 0:
                break
            logger.info(
                "[%s] candidate student link: %s (score=%d, text=%r)",
                orgid, link_url, score, anchor_text,
            )

            norm = _normalize_for_dedup(link_url)
            if norm in seen_keys:
                logger.info(
                    "[%s] candidate student link already seen, skipping: %s",
                    orgid, link_url,
                )
                continue

            link_host = urlsplit(link_url).netloc.lower().split(":")[0]
            if not _host_matches_domains(link_host, domains):
                logger.info(
                    "[%s] candidate student link off-domain, skipping: %s",
                    orgid, link_url,
                )
                continue

            budget.remaining -= 1

            sub_r = _validate_one(
                link_url,
                domains=domains,
                http_timeout=http_timeout,
                user_agent=user_agent,
                js_renderer=js_renderer,
                js_suspicion_threshold=js_suspicion_threshold,
                orgid=orgid,
            )
            if not sub_r.ok:
                logger.info(
                    "[%s] candidate student link validation failed: %s — %s",
                    orgid, link_url, sub_r.notes,
                )
                continue

            seen_keys.add(norm)

            new_source = "link-follow"
            if sub_r.js_rendered:
                new_source += "+js-render"
            new_cand = Candidate(
                url=sub_r.final_url,
                category="Student Portal",
                discovery_source=new_source,
                discovery_reasoning=(
                    f"student link from {url}: text={anchor_text!r} score={score}"
                ),
                validation_notes=sub_r.notes,
                has_password_input=sub_r.has_password_input,
                has_login_text=sub_r.has_login_text,
                has_student_signal=sub_r.has_student_signal,
                js_rendered=sub_r.js_rendered,
            )
            extras.append(new_cand)

            if score >= 3 and parent_replaceable:
                replaced_urls.add(parent.url)
                logger.info(
                    "[%s] student-link validated: %s → real student portal (replaces %s)",
                    orgid, sub_r.final_url, parent.url,
                )
            else:
                reason = "additional" if parent_replaceable else "additional (parent already has password input)"
                logger.info(
                    "[%s] student-link validated: %s → real student portal (%s)",
                    orgid, sub_r.final_url, reason,
                )

    if not extras and not replaced_urls:
        return kept
    return [c for c in kept if c.url not in replaced_urls] + extras


def _resolve_homepage_to_login_url(
    *,
    kept: list[Candidate],
    body_by_url: dict[str, str],
    seen_keys: set[str],
    domains: list[str],
    http_timeout: float,
    user_agent: str,
    orgid: str,
    js_renderer: "JSRenderer | None",
    js_suspicion_threshold: int,
    budget: _LinkFollowBudget,
) -> list[Candidate]:
    """Bug 9 — for any candidate whose URL is a bare host, find the most
    specific login URL via the homepage's anchors and replace it. Applies
    `score_login_path_specificity` so when multiple login anchors exist on
    the same host (e.g. `/Login/Login/StudentLogin` vs
    `/College/CollegeLogin/CollegeLogin`), the student-anchored variant
    wins. Validates the upgrade target via `_validate_one` and only
    accepts it if password-input or login-text is present on the
    destination — never replaces with an unverified URL."""
    new_kept: list[Candidate] = []
    for parent in kept:
        url = parent.url
        if not discovery_rules.is_homepage_url(url):
            new_kept.append(parent)
            continue
        body = body_by_url.get(url)
        if not body or budget.remaining <= 0:
            new_kept.append(parent)
            continue

        candidates = discovery_rules.extract_login_links(
            body, base_url=url, max_n=5,
        )
        if not candidates:
            new_kept.append(parent)
            continue

        replaced = False
        for link_url, score, anchor_text in candidates:
            if budget.remaining <= 0:
                break
            link_host = urlsplit(link_url).netloc.lower().split(":")[0]
            if not _host_matches_domains(link_host, domains):
                continue
            norm = _normalize_for_dedup(link_url)
            if norm in seen_keys:
                continue
            budget.remaining -= 1
            sub_r = _validate_one(
                link_url,
                domains=domains,
                http_timeout=http_timeout,
                user_agent=user_agent,
                js_renderer=js_renderer,
                js_suspicion_threshold=js_suspicion_threshold,
                orgid=orgid,
            )
            if not sub_r.ok:
                logger.info(
                    "[%s] homepage→login validation failed: %s — %s",
                    orgid, link_url, sub_r.notes,
                )
                continue
            if not (sub_r.has_password_input or sub_r.has_login_text):
                logger.info(
                    "[%s] homepage→login no login signal on destination: %s — keeping bare host",
                    orgid, sub_r.final_url,
                )
                continue
            seen_keys.add(norm)
            logger.info(
                "[%s] homepage→login: %s → %s (text=%r, score=%d)",
                orgid, parent.url, sub_r.final_url, anchor_text, score,
            )
            new_source = parent.discovery_source + "+homepage-login"
            if sub_r.js_rendered:
                new_source += "+js-render"
            new_kept.append(replace(
                parent,
                url=sub_r.final_url,
                discovery_source=new_source,
                discovery_reasoning=(
                    f"{parent.discovery_reasoning}; followed login anchor "
                    f"{anchor_text!r} (score={score}) → {sub_r.final_url}"
                ),
                validation_notes=sub_r.notes,
                has_password_input=sub_r.has_password_input,
                has_login_text=sub_r.has_login_text,
                has_student_signal=sub_r.has_student_signal or parent.has_student_signal,
                js_rendered=sub_r.js_rendered or parent.js_rendered,
            ))
            replaced = True
            break
        if not replaced:
            new_kept.append(parent)
    return new_kept


def _validate_one(
    url: str,
    *,
    domains: list[str],
    http_timeout: float,
    user_agent: str,
    js_renderer: "JSRenderer | None" = None,
    js_suspicion_threshold: int = 2,
    orgid: str = "",
    defer_js_render: bool = False,
) -> _ValResult:
    # --- Pre-GET rejects ---
    nonprod = _non_production_match(url)
    if nonprod is not None:
        return _ValResult(ok=False, final_url=url, notes=f"non-production host: {nonprod}")

    if _is_admission_url(url):
        return _ValResult(
            ok=False, final_url=url,
            notes="admission portal — not targeted for enrolled students",
        )

    nonstudent = _non_student_label_match(url)
    if nonstudent is not None:
        return _ValResult(
            ok=False, final_url=url,
            notes=f"non-student ERP ({nonstudent})",
        )

    parsed_in = urlsplit(url)
    if parsed_in.scheme == "http":
        url = urlunsplit(("https", parsed_in.netloc, parsed_in.path, parsed_in.query, parsed_in.fragment))

    path_lower = urlsplit(url).path.lower()
    if path_lower.endswith(FILE_EXTENSIONS):
        return _ValResult(ok=False, final_url=url, notes="file extension")

    # Hard-gate step 1: DNS must resolve within 3s. Saves an 8s HEAD-timeout
    # on dead hosts (the common failure mode for fabricated subdomain URLs).
    host_in = urlsplit(url).netloc.lower().split(":")[0]
    dns_ok, dns_reason = _dns_resolves_with_timeout(host_in)
    if not dns_ok:
        logger.warning("[%s] REJECTED %s: %s", orgid, url, dns_reason)
        return _ValResult(ok=False, final_url=url, notes=dns_reason)

    # HEAD-before-GET: if the host is unreachable / 404 / 5xx, we can skip
    # the (expensive) GET. Many Indian gov sites 405 HEAD; treat 405 as
    # "proceed to GET". Any non-recoverable status terminates here.
    headers = {"User-Agent": user_agent}
    try:
        head_resp = discovery_rules.HTTP_SESSION.head(
            url, headers=headers, timeout=http_timeout, allow_redirects=True,
        )
    except requests.RequestException as err:
        reason = f"unreachable: {type(err).__name__}"
        logger.warning("[%s] REJECTED %s: %s", orgid, url, reason)
        return _ValResult(ok=False, final_url=url, notes=reason)
    head_status = head_resp.status_code
    if head_status in _HARD_GATE_REJECT_STATUSES or 500 <= head_status < 600:
        reason = f"http {head_status} (HEAD)"
        logger.warning("[%s] REJECTED %s: %s", orgid, url, reason)
        return _ValResult(ok=False, final_url=url, notes=reason)
    # 200 / 401 / 403 / 405 / 3xx (already followed) → proceed to GET.

    try:
        resp = discovery_rules.HTTP_SESSION.get(
            url,
            headers=headers,
            timeout=http_timeout,
            allow_redirects=True,
        )
    except requests.RequestException as err:
        reason = f"unreachable: {type(err).__name__}"
        logger.warning("[%s] REJECTED %s: %s", orgid, url, reason)
        return _ValResult(ok=False, final_url=url, notes=reason)

    # Hard-gate step 2: only 200/401/403 are acceptable on the final response.
    # 401/403 are routinely returned by login pages that gate even GET behind
    # auth; 404/410/5xx are dead URLs (common for fabricated subdomains).
    if resp.status_code not in _HARD_GATE_OK_STATUSES:
        reason = f"http {resp.status_code}"
        logger.warning("[%s] REJECTED %s: %s", orgid, url, reason)
        return _ValResult(ok=False, final_url=url, notes=reason)

    final_url = resp.url
    if urlsplit(final_url).scheme != "https":
        reason = "non-https final URL"
        logger.warning("[%s] REJECTED %s: %s", orgid, url, reason)
        return _ValResult(ok=False, final_url=final_url, notes=reason)

    # Hard-gate step 3a: final URL must NOT be the bare root of a known
    # shared platform — that means discovery resolved to e.g.
    # `https://samarth.edu.in/` rather than staying on the institutional
    # subdomain, so the URL doesn't actually point at a portal.
    bare, bare_reason = _is_bare_platform_root(final_url)
    if bare:
        logger.warning("[%s] REJECTED %s: %s", orgid, url, bare_reason)
        return _ValResult(ok=False, final_url=final_url, notes=bare_reason)

    notes: list[str] = []
    orig_scheme = urlsplit(url).scheme
    final_scheme = urlsplit(final_url).scheme
    if orig_scheme != final_scheme:
        notes.append(f"redirected from {orig_scheme} to {final_scheme}")
    elif final_url != url:
        notes.append(f"redirected to {urlsplit(final_url).netloc}{urlsplit(final_url).path}")

    host = urlsplit(final_url).netloc.lower().split(":")[0]
    if (
        not _host_matches_domains(host, domains)
        and not discovery_rules.host_is_known_shared_platform(host)
    ):
        return _ValResult(ok=False, final_url=final_url, notes=f"off-domain: {host}")

    body = resp.text or ""
    body_lower = body.lower()
    has_password = bool(_PASSWORD_INPUT_RE.search(body))
    has_text = any(pat in body_lower for pat in LOGIN_TEXT_PATTERNS)
    has_student = any(pat in body_lower for pat in STUDENT_TEXT_PATTERNS)

    final_url_clean = discovery_rules.strip_session_ids(final_url)

    # Hard-gate steps 3b/3c: body-length and interactive-form checks. Only
    # enforced on the path that would otherwise return ok=True; if the
    # static body looks empty we still let JS-render attempt the page and
    # re-check the gate on the rendered DOM.
    static_gate_ok = (
        len(body) > _HARD_GATE_MIN_BODY_LEN
        and _body_has_interactive_form(body)
    )

    gate_ok, gate_reason = discovery_rules.passes_login_signal_gate(
        final_url=final_url_clean, html=body,
    )

    # Bug 20 — audience classification. If gate-A fired (real login form on
    # the page), check the page's <title>/<h1>/<h2> for staff/admin/employee
    # signals; reject if non-student. Skipped for rule-C (known shared
    # platform — those are pre-classified) and for rule-B (the page itself
    # isn't the login page; the destination will be classified when
    # link-follow validates it).
    audience_reject_reason: str | None = None
    if gate_ok and "rule-A" in gate_reason:
        audience = discovery_rules.classify_login_audience(body)
        if audience == "non_student":
            audience_reject_reason = "non-student audience (staff/admin signals in title/h1)"

    if gate_ok and static_gate_ok and audience_reject_reason is None:
        signals = []
        if has_password:
            signals.append("password-input")
        if has_text:
            signals.append("login-text")
        notes.append("signals: " + ",".join(signals))
        notes.append(gate_reason)
        return _ValResult(
            ok=True, final_url=final_url_clean, notes="; ".join(notes),
            has_password_input=has_password, has_login_text=has_text,
            has_student_signal=has_student,
            body=body,
        )
    if audience_reject_reason is not None:
        logger.warning(
            "[%s] REJECTED %s: %s", orgid, url, audience_reject_reason,
        )
        return _ValResult(
            ok=False, final_url=final_url_clean,
            notes=audience_reject_reason,
        )
    if gate_ok and not static_gate_ok:
        # Login signals matched but body fails the hard gate
        # (length / interactive-form / wildcard-redirect). Don't accept,
        # but allow JS-render to retry below — many SPA login shells fail
        # this on static HTML and pass once Playwright fills them in.
        logger.info(
            "[%s] static body fails hard gate (len=%d has_form=%s); deferring to js-render path",
            orgid, len(body), _body_has_interactive_form(body),
        )

    # --- JS-render escalation ---
    if js_renderer is not None:
        score = discovery_rules.js_shell_suspicion_score(final_url, body)
        if score >= js_suspicion_threshold:
            if defer_js_render:
                # Caller is on a worker thread; Playwright's sync API is bound
                # to its creating thread, so the actual render() must run
                # serially in the main thread. Capture state and let the
                # post-phase loop pick this up.
                logger.info(
                    "[%s] js-render defer: %s (suspicion=%d)",
                    orgid, final_url, score,
                )
                return _ValResult(
                    ok=False, final_url=final_url_clean,
                    notes=f"js-render pending (suspicion={score})",
                    has_password_input=has_password, has_login_text=has_text,
                    has_student_signal=has_student,
                    body=body,
                    js_render_pending=True,
                )
            logger.info(
                "[%s] js-render escalate: %s (suspicion=%d)",
                orgid, final_url, score,
            )
            rendered = js_renderer.render(final_url)
            if rendered.ok:
                rbody = rendered.html or ""
                rbody_lower = rbody.lower()
                has_pw_js = bool(_PASSWORD_INPUT_RE.search(rbody))
                has_tx_js = any(pat in rbody_lower for pat in LOGIN_TEXT_PATTERNS)
                has_student_js = any(pat in rbody_lower for pat in STUDENT_TEXT_PATTERNS)
                rendered_final = discovery_rules.strip_session_ids(
                    rendered.final_url or final_url_clean
                )

                # Hard-gate: rendered output must also clear the bare-platform
                # and body-content checks before we accept it.
                bare_js, bare_js_reason = _is_bare_platform_root(rendered_final)
                if bare_js:
                    logger.warning(
                        "[%s] REJECTED %s: %s (js-rendered)",
                        orgid, url, bare_js_reason,
                    )
                    return _ValResult(
                        ok=False, final_url=rendered_final,
                        notes=f"js-rendered: {bare_js_reason}",
                        js_attempted=True,
                    )
                rendered_gate_ok = (
                    len(rbody) > _HARD_GATE_MIN_BODY_LEN
                    and _body_has_interactive_form(rbody)
                )
                if not rendered_gate_ok:
                    reason = (
                        f"rendered body fails hard gate "
                        f"(len={len(rbody)}, has_form={_body_has_interactive_form(rbody)})"
                    )
                    logger.warning("[%s] REJECTED %s: %s", orgid, url, reason)
                    return _ValResult(
                        ok=False, final_url=rendered_final,
                        notes=f"js-rendered: {reason}",
                        js_attempted=True,
                    )

                gate_ok_js, gate_reason_js = discovery_rules.passes_login_signal_gate(
                    final_url=rendered_final, html=rbody,
                )
                # Bug 20 audience check on rendered DOM as well.
                audience_reject_js: str | None = None
                if gate_ok_js and "rule-A" in gate_reason_js:
                    if discovery_rules.classify_login_audience(rbody) == "non_student":
                        audience_reject_js = (
                            "non-student audience (staff/admin signals in title/h1)"
                        )
                if audience_reject_js is not None:
                    logger.warning(
                        "[%s] REJECTED %s: %s (js-rendered)",
                        orgid, url, audience_reject_js,
                    )
                    return _ValResult(
                        ok=False, final_url=rendered_final,
                        notes=f"js-rendered: {audience_reject_js}",
                        js_attempted=True,
                    )
                if gate_ok_js:
                    signals = []
                    if has_pw_js:
                        signals.append("password-input")
                    if has_tx_js:
                        signals.append("login-text")
                    notes.append("signals: " + ",".join(signals) + " (js-rendered)")
                    notes.append(gate_reason_js)
                    return _ValResult(
                        ok=True, final_url=rendered_final, notes="; ".join(notes),
                        has_password_input=has_pw_js, has_login_text=has_tx_js,
                        has_student_signal=has_student_js,
                        js_rendered=True, js_attempted=True,
                        body=rbody,
                    )
                return _ValResult(
                    ok=False, final_url=rendered_final,
                    notes=f"js-rendered: {gate_reason_js}",
                    js_attempted=True,
                )
            return _ValResult(
                ok=False, final_url=final_url_clean,
                notes=f"no login signal; js-render failed: {rendered.error}",
                js_attempted=True,
            )

    # No JS-render escalation triggered. If the static body failed the
    # hard gate, log REJECTED at WARNING; otherwise the strict A/B/C gate
    # is what dropped the candidate (real page, no login form / login
    # redirect / known platform — Bug 19's canonical rejection).
    if not static_gate_ok:
        reason = (
            f"body fails hard gate "
            f"(len={len(body)}, has_form={_body_has_interactive_form(body)})"
        )
        logger.warning("[%s] REJECTED %s: %s", orgid, url, reason)
        return _ValResult(ok=False, final_url=final_url_clean, notes=reason)
    logger.warning("[%s] REJECTED %s: %s", orgid, url, gate_reason)
    return _ValResult(ok=False, final_url=final_url_clean, notes=gate_reason)


def _host_matches_domains(host: str, domains: list[str]) -> bool:
    for d in domains:
        d = d.lower().lstrip(".")
        if host == d or host.endswith("." + d):
            return True
    return False


def _non_production_match(url: str) -> str | None:
    host = urlsplit(url).netloc.lower()
    for sub in NON_PRODUCTION_HOST_SUBSTRINGS:
        if sub in host:
            return sub
    return None


def _is_admission_url(url: str) -> bool:
    parsed = urlsplit(url)
    host = parsed.netloc.lower()
    path = (parsed.path or "").lower()
    for tok in ADMISSION_URL_SUBSTRINGS:
        if tok in host or tok in path:
            return True
    return False


def _non_student_label_match(url: str) -> str | None:
    """Return a matching token if any *label* of the host is a non-student
    indicator (`hr`, `payroll`, `finance`, `procurement`, `vendor`).

    Label-aware so `harvard.edu` doesn't match `hr` and `application.x.com`
    doesn't match anything spurious. A label matches if it's exactly the
    token, or starts with `token-` / `token_` (e.g. `hr-portal`).
    """
    host = urlsplit(url).netloc.lower()
    for label in host.split("."):
        for token in NON_STUDENT_HOST_LABELS:
            if label == token:
                return token
            if label.startswith(token + "-") or label.startswith(token + "_"):
                return token
    return None


def _normalize_for_dedup(url: str) -> str:
    cleaned = discovery_rules.strip_session_ids(str(url))
    p = urlsplit(cleaned)
    scheme = (p.scheme or "https").lower()
    host = p.netloc.lower().split(":")[0]
    path = p.path.rstrip("/")
    return urlunsplit((scheme, host, path, "", ""))


def _dedupe(candidates: list[Candidate]) -> list[Candidate]:
    seen: set[str] = set()
    out: list[Candidate] = []
    for c in candidates:
        key = _normalize_for_dedup(c.url)
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out

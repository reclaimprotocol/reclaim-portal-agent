"""Genie-V3 · Compliance mapping as weighted bipartite matching.

Replaces the LLM-driven waterfall. The model now only EXTRACTS two flat sets —
portals and legal links — and this module decides which terms govern which
portal, deterministically.

Why the change: over 200 human-reviewed orgs the waterfall returned "None Found"
for 96 of 149 portals, and most of what it did return came from Stage 4, the
apex-root rescue that human review rejects most often. Association is a scoring
problem with an exact optimum; asking a model to do it alongside extraction got
both done poorly and invited invented URLs.

    W(P_i -> C_j) = 0.40*S_domain + 0.40*S_semantic + 0.20*S_distance

THREE PLACES THE SPEC MEETS REALITY
-----------------------------------
1. `networkx.bipartite.maximum_weight_matching` DOES NOT EXIST. networkx 3.6
   ships `bipartite.hopcroft_karp_matching` / `maximum_matching` (both
   UNWEIGHTED) and `minimum_weight_full_matching`. The weighted optimiser is the
   general `nx.max_weight_matching`, which is what this module calls.

2. `nx.max_weight_matching` REJECTS DiGraphs ("not implemented for directed
   type"). The directed graph is still built — it is the auditable record of
   which edges were considered and why — and matching runs on an undirected
   projection of it.

3. A MATCHING IS 1:1, WHICH IS WRONG FOR THIS DOMAIN. One university privacy
   policy legitimately governs every one of its portals: BUET's biis and moodle
   logins share bcc.buet.ac.bd/privacy-policy. A pure matching would award that
   page to ONE portal and return null for the rest — silently losing correct
   answers. So the maximum-weight matching runs first (it resolves genuine
   contention optimally), and any portal left unmatched then takes its own
   best-scoring edge, provided that edge clears the same 0.40 gate. Every
   mapping still has to earn its score; nothing is assigned for free.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence
from urllib.parse import urlsplit

import networkx as nx

logger = logging.getLogger("genie.graph")

#: Composite score below which an edge is cut and the portal reports no terms.
CONFIDENCE_THRESHOLD = 0.40

W_DOMAIN, W_SEMANTIC, W_DISTANCE = 0.40, 0.40, 0.20

#: S_ownership is a MULTIPLIER, not a fourth weighted term.
#:
#: The failure it fixes: a crawl surfaces a DIFFERENT university's Moodle, and
#: that Moodle's own privacy page is an exact-host match to it — S_domain 1.0,
#: S_semantic 1.0, S_distance 1.0. Every additive scheme still clears the gate,
#: because the three signals only ever compare the portal to the legal link and
#: never ask whether either belongs to the ORG we are working on. Measured: 5 of
#: 25 host-mismatches were exactly this (fstm.ac.ma -> univh2c.ma,
#: feg.uh1.ac.ma -> usmba.ac.ma, nipa.ac.zm -> zcas.ac.zm).
#:
#: A tenant on a known SaaS vendor is legitimately off-domain, so it is damped
#: rather than cut.
OWN_SELF, OWN_SAAS, OWN_FOREIGN = 1.0, 0.90, 0.35

#: Crawl-provenance multipliers, per the spec.
DISTANCE_NATIVE_CRAWL = 1.0
DISTANCE_SEARCH_FALLBACK = 0.4

#: Registrable roots needing three labels, so `a.b.sgu.edu.vn` reduces to
#: `sgu.edu.vn` and not the meaningless `edu.vn` — which would make every
#: Vietnamese institution look like a sibling of every other.
_TWO_LABEL_TLDS = frozenset({
    "ac.in", "co.in", "edu.in", "org.in", "net.in", "gov.in", "ac.uk", "co.uk",
    "com.br", "edu.br", "org.br", "gov.br", "com.au", "edu.au", "com.mx",
    "edu.mx", "com.ar", "edu.ar", "com.co", "edu.co", "ac.id", "sch.id",
    "edu.ph", "com.ph", "ac.lk", "edu.lk", "ac.bd", "edu.bd", "edu.pk",
    "edu.ng", "ac.ke", "ac.za", "edu.my", "edu.vn", "com.vn", "ac.vn",
    "ac.th", "edu.eg", "edu.sa", "com.pe", "edu.pe", "com.tr", "edu.tr",
    "edu.ua", "com.ua",
})

# --------------------------------------------------------------------------- #
#  S_semantic — three tiers, language-agnostic                                 #
# --------------------------------------------------------------------------- #
#: TIER 1 (1.0) — universal legal ROOTS, not whole words. Matching on stems
#: ('privac', 'polit', 'term', 'condi') covers privacy/privacidade/privacidad/
#: privatsphäre and política/politique/policy from one pattern, which a
#: word-list cannot. Non-Latin scripts are listed explicitly because they share
#: no roots with the Latin set.
_TIER1_ROOTS = (
    # 'polic' as well as 'polit': the Latin stem covers política/politique, but
    # the plain English "policy" shares no stem with it, so a bare /policy.php
    # or policy.<host> scored 0.1 — found by auditing 152 cached vendor entries.
    "privac", "polit", "polic", "term", "condi", "legal", "regul", "tos", "tnc",
    "disclaim", "datenschutz", "impressum", "aviso", "juridic", "mentions",
    # data-protection phrasings that share no stem with "privacy"
    "protecao-de-dados", "proteccion-de-datos", "protecao_de_dados",
    "protection-des-donnees", "dados-pessoais", "datos-personales",
    "confidentialit", "gdpr", "lgpd", "pdpa", "dataprivacy", "data-protection",
    # Vietnamese · Thai · Indonesian/Malay · Arabic · Chinese · Japanese · Korean
    "bảo mật", "điều khoản", "chính sách",
    "นโยบาย", "ความเป็นส่วนตัว", "ข้อกำหนด",
    "kebijakan", "syarat", "ketentuan", "privasi",
    "الخصوصية", "الشروط", "سياسة",
    "隐私", "条款", "プライバシー", "利用規約", "개인정보", "이용약관",
)
_TIER1_RE = re.compile("|".join(re.escape(t) for t in _TIER1_ROOTS), re.I)

#: Leftmost host labels that ARE the signal: policy.uni.edu/ has an empty path,
#: so a path-only scorer rates a dedicated legal subdomain 0.1.
#:
#: Matched as an EXACT label set, never as a substring of the host. "polit" is a
#: tier-1 root, and a substring test would score every page on politecnico.edu.co
#: and politeknik.ac.id as a legal document.
_LEGAL_HOST_LABELS = frozenset({
    "policy", "policies", "privacy", "privacidade", "privacidad",
    "terms", "termos", "legal", "tnc", "tos", "compliance", "dataprivacy",
})

#: TIER 2 (0.3) — transactional neighbours that OVERRIDE tier 1.
#:
#: This ordering is the whole point. "refund-policy", "cancellation-policy" and
#: "shipping-policy" all contain the tier-1 root 'polic', so a flat matcher
#: scores them 1.0 and a refund page can outrank the real privacy notice.
#: Observed live: org 10002050 was matched to `/refund-cancle` at W=0.92 while
#: the sheet's answer was `/privacy-policy` on the same host — identical domain
#: score, so semantics alone decided it, and semantics got it wrong.
_TIER2_TERMS = (
    "refund", "cancel", "cancella", "return", "shipping", "delivery",
    "billing", "payment", "invoice", "cookie", "sitemap", "accessib",
    "reembolso", "cancelar", "devoluc", "envio", "envío", "facturac",
    "remboursement", "annulation", "erstattung", "stornier",
    # Spanish/Portuguese payment vocabulary. Added after co-learning banked
    # "Política de Pagos" as a legal phrase: it contains the tier-1 root
    # 'polit', so without an exclusion a PAYMENT policy scored a perfect 1.0
    # and could outrank a real privacy notice on the same host.
    "pagos", "pagamento", "pagamentos", "cobranca", "cobrança", "cobro",
    "mensalidade", "boleto", "tarifa",
)
_TIER2_RE = re.compile("|".join(re.escape(t) for t in _TIER2_TERMS), re.I)

SCORE_TIER1, SCORE_TIER2, SCORE_TIER3 = 1.0, 0.3, 0.1


def host_of(url: str) -> str:
    h = url or ""
    if "://" in h:
        h = urlsplit(h).netloc
    h = h.lower().split("@")[-1].split(":")[0].strip().strip(".")
    return h[4:] if h.startswith("www.") else h


#: Generic second-level labels that sit under a 2-letter ccTLD. Any
#: `<name>.<generic>.<cc>` host has a THREE-label registrable root.
#:
#: This replaces the hand-maintained ccTLD list, which was the bug: `ac.ma` and
#: `ac.zm` were simply missing, so registrable_root("fsjes.usmba.ac.ma")
#: returned "ac.ma" and EVERY Moroccan university looked like the same
#: organisation — which is how a foreign university's Moodle scored a perfect
#: "org-root" ownership match. A list can always be missing a nation; this rule
#: covers all of them.
_GENERIC_SLD = frozenset({
    "ac", "edu", "com", "org", "net", "gov", "co", "sch", "mil", "int",
    "biz", "info", "or", "ne", "go", "in", "nom", "web", "gob", "gouv",
})


def registrable_root(url_or_host: str) -> str:
    parts = [p for p in host_of(url_or_host).split(".") if p]
    if len(parts) <= 2:
        return ".".join(parts)
    if len(parts[-1]) == 2 and parts[-2].lower() in _GENERIC_SLD:
        return ".".join(parts[-3:])
    if ".".join(parts[-2:]) in _TWO_LABEL_TLDS:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])



@dataclass
class EdgeScore:
    """Full provenance for one candidate association."""

    portal_url: str
    legal_url: str
    weight: float
    s_domain: float
    s_semantic: float
    s_distance: float
    domain_relation: str
    s_ownership: float = 1.0
    ownership_relation: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "tnc_url": self.legal_url,
            "confidence": round(self.weight, 4),
            "s_domain": round(self.s_domain, 3),
            "s_semantic": round(self.s_semantic, 3),
            "s_distance": round(self.s_distance, 3),
            "s_ownership": round(self.s_ownership, 3),
            "domain_relation": self.domain_relation,
            "ownership_relation": self.ownership_relation,
        }


class GraphComplianceMatcher:
    """Weighted bipartite matcher for portal -> legal-document association."""

    def __init__(self, *, threshold: float = CONFIDENCE_THRESHOLD,
                 saas_roots: Iterable[str] | None = None,
                 learned_keywords: Iterable[str] | None = None) -> None:
        self.threshold = threshold
        # Known multi-tenant SaaS roots. A tenant's terms live on the VENDOR
        # root, which is neither the same host nor a sibling, so without this
        # every SaaS-hosted portal would score S_domain = 0.0.
        self.saas_roots = {r.lower() for r in (saas_roots or ())}
        self.last_graph: nx.DiGraph | None = None
        self.last_edges: list[EdgeScore] = []
        self.learned_keywords: set[str] = set()
        self._learned_re: re.Pattern[str] | None = None
        if learned_keywords:
            self.add_learned_keywords(learned_keywords)

    def add_learned_keywords(self, words: Iterable[str]) -> int:
        """Fold newly-learned native phrases into the tier-1 matcher, live.

        Called mid-run by the co-learning hook, so a language first seen by one
        worker is recognised by the other nineteen without another model call.

        Phrases that trip the tier-2 exclusions are REFUSED. A learned keyword
        is permanent and global, so one bad entry mis-scores every future run in
        that language — and the model does emit them: it flagged "файлів cookie"
        and "Política de Pagos" as primary compliance targets.
        """
        new = {w.strip().lower() for w in words
               if w and len(w.strip()) >= 3 and not _TIER2_RE.search(w)}
        new -= self.learned_keywords
        if not new:
            return 0
        self.learned_keywords |= new
        self._learned_re = re.compile(
            "|".join(re.escape(w) for w in sorted(self.learned_keywords)), re.I)
        return len(new)

    # ------------------------------------------------------------------ #
    #  Signals                                                            #
    # ------------------------------------------------------------------ #
    def score_domain(self, portal_url: str, legal_url: str) -> tuple[float, str]:
        """1.0 exact · 0.8 vertical · 0.7 sibling · 0.5 SaaS · 0.0 unrelated."""
        ph, lh = host_of(portal_url), host_of(legal_url)
        if not ph or not lh:
            return 0.0, "unknown"
        if ph == lh:
            return 1.0, "exact-host"
        pr, lr = registrable_root(ph), registrable_root(lh)
        # SaaS is tested FIRST. A tenant like uni.samarth.edu.in is structurally
        # a subdomain of samarth.edu.in, so the vertical rule would claim it at
        # 0.8 — but a VENDOR's generic terms are weaker evidence than a
        # university's own, which is exactly why the spec rates it 0.5.
        if lr and lr in self.saas_roots and (ph == lr or ph.endswith("." + lr)):
            return 0.5, "saas-ecosystem"
        if pr and pr == lr:
            # One is the other's parent/child => vertical; otherwise both are
            # leaves under a shared root => horizontal siblings.
            if ph.endswith("." + lh) or lh.endswith("." + ph):
                return 0.8, "vertical-parent"
            return 0.7, "sibling-subdomain"
        if lr and (lr in self.saas_roots or pr in self.saas_roots):
            if ph.endswith("." + lr) or pr == lr:
                return 0.5, "saas-ecosystem"
        return 0.0, "unrelated"

    def score_ownership(self, legal_url: str, official_domain: str) -> tuple[float, str]:
        """Does this legal document belong to the ORG we are processing?

        `S_domain` asks whether the document relates to the PORTAL; this asks
        whether it relates to the UNIVERSITY. Both can be needed: a foreign
        institution's Moodle scores a perfect 1.0 against its own privacy page
        while having nothing to do with our org.
        """
        if not official_domain:
            return OWN_SELF, "no-org-domain"        # nothing to check against
        lh, oh = host_of(legal_url), host_of(official_domain)
        if not lh or not oh:
            return OWN_SELF, "unknown"
        if lh == oh or lh.endswith("." + oh) or oh.endswith("." + lh):
            return OWN_SELF, "org-host"
        lr, orr = registrable_root(lh), registrable_root(oh)
        if lr and lr == orr:
            return OWN_SELF, "org-root"
        if lr and lr in self.saas_roots:
            return OWN_SAAS, "org-saas-tenant"
        return OWN_FOREIGN, "foreign-org"

    def score_semantic(self, legal_url: str, anchor_text: str,
                       is_primary: bool = False,
                       native_keyword: str | None = None) -> float:
        """Three-tier score. Tier 2 deliberately OVERRIDES tier 1.

        `is_primary` is the model's own reading of the document in its native
        language — the one signal a regex cannot produce. It promotes to tier 1,
        but it does NOT bypass the tier-2 veto: a model that flags a refund
        policy as primary is still overruled by the explicit exclusion.
        """
        parts = urlsplit(legal_url or "")
        path = parts.path or ""
        blob = f"{path} {anchor_text or ''} {native_keyword or ''}"

        # Tier 2 first — it wins outright.
        if _TIER2_RE.search(blob):
            return SCORE_TIER2

        # A dedicated legal subdomain is tier 1 on its own.
        label = (parts.netloc or "").lower().removeprefix("www.").split(".")[0]
        if label in _LEGAL_HOST_LABELS:
            return SCORE_TIER1

        if is_primary:
            return SCORE_TIER1
        if _TIER1_RE.search(blob):
            return SCORE_TIER1
        # Keywords learned from earlier runs, in languages we shipped without.
        if self._learned_re is not None and self._learned_re.search(blob):
            return SCORE_TIER1
        return SCORE_TIER3

    @staticmethod
    def score_distance(source_crawl_distance: float) -> float:
        return max(0.0, min(1.0, float(source_crawl_distance)))

    # ------------------------------------------------------------------ #
    #  Master method                                                      #
    # ------------------------------------------------------------------ #
    def resolve_optimal_compliance_mappings(
        self,
        discovered_portals: list,
        harvested_legal_links: list,
        source_crawl_distance: float = DISTANCE_NATIVE_CRAWL,
        official_domain: str = "",
    ) -> dict[str, dict[str, Any] | None]:
        """{portal_url: mapping-dict | None} — one decision per portal.

        `None` means every candidate edge scored below the gate; the caller must
        treat that as "no terms found", never as an excuse to guess one.
        """
        portals = [self._portal_url(p) for p in discovered_portals]
        portals = [p for p in portals if p]
        legals = [(self._legal_url(c), self._legal_anchor(c),
                   self._legal_primary(c), self._legal_native(c))
                  for c in harvested_legal_links]
        legals = [t for t in legals if t[0]]

        result: dict[str, dict[str, Any] | None] = {p: None for p in portals}
        self.last_edges = []
        if not portals:
            return result
        if not legals:
            logger.info("graph: %d portal(s), 0 legal links — nothing to match", len(portals))
            return result

        s_dist = self.score_distance(source_crawl_distance)

        # --- 1. directed dense wiring (kept as the audit record) ---------
        G = nx.DiGraph()
        p_nodes = [f"P:{u}" for u in portals]
        c_nodes = [f"C:{t[0]}" for t in legals]
        G.add_nodes_from(p_nodes, bipartite=0)
        G.add_nodes_from(dict.fromkeys(c_nodes), bipartite=1)

        best_by_portal: dict[str, EdgeScore] = {}
        for pu in portals:
            for lu, anchor, primary, native in legals:
                s_dom, rel = self.score_domain(pu, lu)
                # A legal page on an UNRELATED domain is never this portal's,
                # however well its wording scores. Without this the composite
                # formula lets s_semantic(1.0) + s_distance(1.0) reach 0.60 and
                # clear the 0.40 gate on s_domain = 0.0 alone — verified. That is
                # precisely the ownership contamination V2 shipped 41 times
                # (Brazilian orgs carrying a Philippine university's policy), so
                # zero domain affinity is a hard veto, not a low score.
                s_sem = self.score_semantic(lu, anchor, primary, native)
                s_own, own_rel = self.score_ownership(lu, official_domain)
                base = (W_DOMAIN * s_dom + W_SEMANTIC * s_sem
                        + W_DISTANCE * s_dist) if s_dom > 0.0 else 0.0
                w = base * s_own
                es = EdgeScore(pu, lu, w, s_dom, s_sem, s_dist, rel, s_own, own_rel)
                self.last_edges.append(es)
                G.add_edge(f"P:{pu}", f"C:{lu}", weight=w, **es.as_dict())
                cur = best_by_portal.get(pu)
                if cur is None or w > cur.weight:
                    best_by_portal[pu] = es
        self.last_graph = G

        # --- 2. maximum weight matching (undirected projection) ----------
        # Only edges that clear the gate enter the optimiser: a matching will
        # happily pair a portal with a 0.12 edge just to raise the global total,
        # and that pairing would then be discarded anyway.
        U = nx.Graph()
        U.add_nodes_from(p_nodes, bipartite=0)
        U.add_nodes_from(dict.fromkeys(c_nodes), bipartite=1)
        for u, v, d in G.edges(data=True):
            if d["weight"] >= self.threshold:
                U.add_edge(u, v, weight=d["weight"])

        matched: dict[str, str] = {}
        if U.number_of_edges():
            for a, b in nx.max_weight_matching(U, maxcardinality=False, weight="weight"):
                pn, cn = (a, b) if a.startswith("P:") else (b, a)
                matched[pn[2:]] = cn[2:]

        # --- 3. resolve, with shared-document fallback -------------------
        n_matched = n_shared = 0
        for pu in portals:
            lu = matched.get(pu)
            if lu:
                es = next(e for e in self.last_edges
                          if e.portal_url == pu and e.legal_url == lu)
                result[pu] = {**es.as_dict(), "assignment": "matching"}
                n_matched += 1
                continue
            # Unmatched only because the matching is 1:1 — take this portal's
            # own best edge if it independently clears the gate. This is what
            # keeps one shared university privacy policy serving every portal.
            es = best_by_portal.get(pu)
            if es and es.weight >= self.threshold:
                result[pu] = {**es.as_dict(), "assignment": "shared-document"}
                n_shared += 1

        gated = sum(1 for v in result.values() if v is None)
        logger.info("graph: %d portal(s) x %d legal link(s) -> %d matched, "
                    "%d shared, %d gated below %.2f",
                    len(portals), len(legals), n_matched, n_shared, gated, self.threshold)
        return result

    # ------------------------------------------------------------------ #
    @staticmethod
    def _portal_url(p: Any) -> str:
        return (getattr(p, "exact_url", None) or (p.get("exact_url") if isinstance(p, dict) else "") or "").strip()

    @staticmethod
    def _legal_url(c: Any) -> str:
        return (getattr(c, "url", None) or (c.get("url") if isinstance(c, dict) else "") or "").strip()

    @staticmethod
    def _legal_primary(c: Any) -> bool:
        v = getattr(c, "is_primary_compliance_target", None)
        if v is None and isinstance(c, dict):
            v = c.get("is_primary_compliance_target")
        return bool(v)

    @staticmethod
    def _legal_native(c: Any) -> str:
        v = getattr(c, "detected_native_keyword", None)
        if v is None and isinstance(c, dict):
            v = c.get("detected_native_keyword")
        return (v or "").strip()

    @staticmethod
    def _legal_anchor(c: Any) -> str:
        return (getattr(c, "anchor_text", None) or (c.get("anchor_text") if isinstance(c, dict) else "") or "").strip()

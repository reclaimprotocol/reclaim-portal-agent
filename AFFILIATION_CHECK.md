# University Affiliation Checking

**Goal:** Find the **university that grants a college's degrees**. Affiliated colleges rarely run their own student portal — students log in through the **affiliating university's** central ERP / exam / LMS system. Resolving the affiliation gives us a working login URL even when the college's own site has none.

---

## Step 1 — LLM affiliation lookup (Gemini via OpenRouter)

One targeted prompt resolves it directly (a college states its affiliating university on its site / Wikipedia):

```
Which university grants degrees to (affiliates) the college "<NAME>", <CITY>, in India?
Reply with ONLY the affiliating university's official website domain (e.g. mgu.ac.in).
If the institution is itself a university / deemed-to-be / autonomous / central /
national institute that is NOT affiliated to any parent university, reply exactly "NONE".
```

Why it's shaped this way:
- **Ask for the domain, not the name** → avoids collisions ("Kerala University" vs "Kerala University of Health Sciences"). Domains are unique keys.
- **Explicit "NONE"** → autonomous / deemed / central / national institutes (which award their own degrees) aren't wrongly attached to a parent.
- The answer is regex-validated to a real domain (strip `https://`, `www.`, trailing slashes).

---

## Step 2 — Match against curated rules

We keep a hand-curated map of **78 affiliating universities → central portals across 17 states**
(`AFFILIATING_UNIVERSITY_PORTALS` in `agent/config.py`, lines 1347–2616; exported as `affiliation_rules.csv`).

Matching uses three signals:

1. **Domain match** — the LLM's returned domain vs. the map's keys, using a word-boundary match so a short key isn't matched inside a longer domain (e.g. `mu.ac.in` (Mumbai) must not match inside `skmu.ac.in`).
2. **State / district aliases** — each rule lists state + district aliases; the college's location is matched against them (e.g. GNDU covers Punjab + Haryana + Amritsar + Chandigarh + HP + J&K).
3. **Professional vs. general (name tokens)** — college names containing *engineering, institute of technology, polytechnic, medical, nursing, pharmac, dental, ayurved, agricultur, veterinary* route to the **statewide technical / health-science / agricultural university** (AKTU, KUHS, GTU…), **not** the district's general university it happens to sit in. General universities carry a `name_tokens_exclude` so they don't grab a professional college in their district.

---

## Step 3 — Dynamic fallback

If the resolved parent **isn't** in the curated map, we run the **same portal-discovery pipeline on the parent's domain** to find its portals live. This is **recursion-guarded** (the parent's own run can't trigger another affiliation hop) and **cached per parent domain** (many colleges share one university, so it's looked up once).

---

## Rule types (real examples)

| Type | Examples |
|---|---|
| State technical university (engineering / pharmacy) | AKTU (`aktu.ac.in`, 750+ colleges), GTU, VTU Belagavi, IKG-PTU, JNTU-A / K / H |
| State health-science university (medical / nursing / pharmacy) | ABVMU Lucknow, Atal Medical & Research University HP |
| Agricultural / horticulture | Dr. YS Parmar University of Horticulture & Forestry |
| Regional general university (arts / science / commerce) | CCSU Meerut (700+), University of Lucknow, SPPU Pune, University of Mumbai, University of Calcutta |
| Multi-state cluster affiliator | GNDU Amritsar (Punjab + Haryana + Chandigarh + HP + J&K) |

Each mapped portal is the university's **actual central login** (Samarth eGov, a state ERP like AKTU's, or a custom SLC portal like GNDU's), so a matched college inherits a working student-login URL instead of coming back empty.

---

## Coverage by state (78 rules total)

| State | Rules | State | Rules |
|---|---|---|---|
| Jharkhand | 11 | Bihar | 3 |
| Kerala | 10 | Madhya Pradesh | 3 |
| Uttar Pradesh | 8 | Karnataka | 3 |
| Maharashtra | 7 | Andhra Pradesh | 3 |
| Himachal Pradesh | 6 | Telangana | 3 |
| Gujarat | 4 | West Bengal | 2 |
| Rajasthan | 4 | Haryana | 2 |
| Tamil Nadu | 4 | Odisha | 2 |
| Punjab | 3 | | |

---

## Where things live in the code

| Piece | Location |
|---|---|
| The 78 affiliation rules | `agent/config.py` → `AFFILIATING_UNIVERSITY_PORTALS` (lines 1347–2616) |
| Professional-college name tokens | `agent/config.py` → `PROFESSIONAL_COLLEGE_NAME_TOKENS` (~line 1338) |
| Curated-rule matching logic | `agent/stages/discovery.py` → `_resolve_affiliating_portals()` (~line 129), `_affiliation_matches()` (~line 206) |
| Dynamic parent-domain fallback | `agent/stages/discovery.py` → `_discover_parent_portals()` (~line 272) |
| Full rule export (machine-readable) | `affiliation_rules.csv` (domain · state · category · portal_url · state_aliases · note) |

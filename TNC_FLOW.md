# How We Analyze a University's Terms & Conditions

## What this answers

For each university, we need to know: **does its Terms & Conditions allow a
user to log in and pull their own academic data, on their behalf?**

Every university gets one verdict:

- **Yes** — nothing in the T&C prohibits it.
- **No** — the T&C explicitly bans scraping / automated access / data extraction.
- **Maybe** — ambiguous wording, or we couldn't read the page (needs a human look).

## The flow

It runs in three steps: **find the T&C pages → read & score each one →
combine them into one verdict.**

```
1. Find T&C pages   ─►   2. Score each page   ─►   3. Combine into one verdict
```

### Step 1 — Find the T&C pages

We look in **two places, in parallel**:

1. the university's **own website**, and
2. the **login portal** the student actually uses (often a third-party vendor
   like Camu, Knimbus, or MyClassboard — these frequently have their *own*
   terms, sometimes stricter than the university's).

On each site we try the common locations — `/terms-and-conditions`, `/terms`,
`/privacy-policy`, `/disclaimer`, etc. — and we also ask **Gemini** for the
university's terms/privacy URL. Every candidate page is put through a strict
check before we trust it: it must actually load, look like a real terms page
(right title/keywords, not an error or login screen), and — for PDFs — contain
real extractable legal text. We collect **all** the valid pages we find, not
just the first.

### Step 2 — Read & score each page

For each page we extract the text (HTML or PDF) and scan it:

- **Strong prohibitions** → **No**: words like *scrape, crawl, spider,
  data-mine, harvest, reverse-engineer, automated access*.
- **Weaker signals** → No or Maybe depending on how many and the surrounding
  wording.
- We deliberately **ignore** liability disclaimers, copyright/trademark
  boilerplate, and law references so they don't trigger false alarms.
- Nothing prohibitive → **Yes**.

Two things make this robust:

- **JavaScript-rendered pages:** some sites load their terms via JavaScript, so
  a plain fetch looks empty. When that happens we render the page in a real
  headless browser and read the result.
- **Hard cases get a second opinion:** in "hybrid" mode, anything the keyword
  scan finds ambiguous is handed to **Claude**, which reads the full text and
  returns a clear Yes/No/Maybe plus the exact clause it relied on. Clear-cut
  pages skip this and resolve instantly.

We cache each page's result, so the same terms page (or the same vendor's
terms shared across many colleges) is only analyzed once.

### Step 3 — Combine into one verdict

A university often has several pages (terms + privacy + disclaimer). The rule:

- The **Terms of Use page is binding.** If a terms page says **No**, the
  university is **No** — a permissive privacy policy can't override it.
- If there's no dedicated terms page, we go with the majority of what we found.
- If pages exist but **none could be read** (dead links, blocked, scanned-image
  PDFs), we mark it **Maybe** for manual review — we never quietly call an
  unreadable page "Yes."

## Known limits (where a human still helps)

- **Scanned-image PDFs** — picture-of-text with no real text layer; we can't read these (no OCR yet).
- **Blocked / unreachable hosts** — some servers refuse our requests or time out, even from a real browser.
- These land as **Maybe**, flagged for a quick manual check.

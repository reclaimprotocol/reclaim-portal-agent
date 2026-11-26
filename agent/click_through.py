"""Genie-V3 · click-through for logins that only appear after a click.

The static and rendered verifiers read a page; neither touches it. That misses
three common layouts:

  * the Login link sits inside a closed menu (a hamburger, a "Students"
    dropdown) and is not in the DOM, or not visible, until the menu opens;
  * the login is a button with no link: clicking it opens a form in a pop-up
    or on the same page, and the address never changes;
  * the homepage has no login at all, but its "Students" section page does.

This module drives a real browser: it opens menus, clicks the controls that
look like a student sign-in, and judges what each one leads to with the same
rules the link check uses (`guardrails._classify_login_target`).

WHAT IT RETURNS
---------------
    one     exactly one student login was reached at a new address.
            Publish that address.
            When several different logins were reached (student portal and
            LMS, say), the best one is published: student-labelled first,
            then a student system over a library, then the first offered.
    modal   a student login appeared without the address changing.
            Publish the page; the click is how a student gets to the form.
    button  a sign-in button was seen but never opened onto a form. Not
            enough to publish: the gate sends the page to REVIEW.
    none    nothing the page offers leads to a student login.

It is slow (a browser, several navigations), so the hybrid verifier calls it
only when the cheaper checks could not settle the page.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Sequence
from urllib.parse import urlsplit

from agent import guardrails as G
from agent import proxy as _proxy
from agent.admission import is_admission_portal, is_admission_url
from agent.languages import STUDENT_AUDIENCE_RE as _NATIVE_STUDENT_RE

logger = logging.getLogger("genie.click_through")

#: Set GENIE_CLICK_THROUGH=0 to keep the verifiers read-only.
ENABLED = os.getenv("GENIE_CLICK_THROUGH", "1").strip().lower() \
    not in {"0", "false", "no", "off"}
#: Controls followed per page, best first.
MAX_CLICKS = int(os.getenv("GENIE_CLICK_THROUGH_MAX_CLICKS", "4"))
#: Wall-clock ceiling for one page, every click included.
BUDGET_S = int(os.getenv("GENIE_CLICK_THROUGH_BUDGET", "90"))
_MAX_TOGGLES = 5
_MAX_SECTIONS = 2
_NAV_TIMEOUT_MS = 30_000

# Names a student system goes by on a menu. On their own they are weak (a
# "Portal" link is often the public website), so they only qualify a control
# together with student wording.
_SERVICE_WORD_RE = re.compile(
    r"\b(?:portal|erp|intranet|sis|ums|eva|online\s+services?)\b", re.I)
# Names that only ever mean a student system, so they qualify a control alone:
# nobody labels the public website "LMS" or "Campus Virtual".
_STUDENT_SYSTEM_RE = re.compile(
    r"\b(?:lms|moodle|e-?campus|siakad|sigaa|suap|autogesti[oó]n|"
    r"campus\s+virtual|aula\s+virtual|ava|canvas|blackboard|classroom)\b", re.I)
# Pages about students that are not where students sign in.
_NOT_A_STUDENT_SECTION_RE = re.compile(
    r"former|alumni|ex-|outstanding|achiev|outcome|scholarship|admission|"
    r"prospective|testimonial|placement|grievance|feedback|union|council|"
    r"egresad|egress|antig[uo]os?", re.I)
_LIBRARY_RE = re.compile(r"librar|biblioteca|bibliot|koha|opac|knimbus|ezproxy", re.I)
_LOGIN_PATH_RE = re.compile(r"log[-_]?in|sign[-_]?in|logon|auth|sso|entrar|acesso|giris", re.I)
_MENU_TOGGLE_RE = re.compile(
    r"menu|navigation|navbar|toggler|hamburger|men[uú]|navega", re.I)
_NOT_A_PAGE_RE = re.compile(
    r"^(?:mailto:|tel:|sms:|whatsapp:)|facebook\.com|twitter\.com|//x\.com|"
    r"instagram\.com|youtube\.com|linkedin\.com|wa\.me|t\.me/|play\.google\.com|"
    r"apps\.apple\.com|google\.[a-z.]+/maps", re.I)

# Tags every candidate control with `data-genie-ct` so it can be clicked later
# by that id, and reports what the page shows for it.
_JS_COLLECT = r"""
() => {
  const out = [];
  const sel = 'a, button, [role=button], [role=menuitem], input[type=submit], input[type=button]';
  document.querySelectorAll(sel).forEach((el, i) => {
    const r = el.getBoundingClientRect();
    const st = window.getComputedStyle(el);
    const visible = r.width > 0 && r.height > 0
      && st.visibility !== 'hidden' && st.display !== 'none';
    const label = el.getAttribute('aria-label') || el.getAttribute('title') || '';
    const text = (el.innerText || el.value || label || '').trim().replace(/\s+/g, ' ').slice(0, 80);
    const raw = el.getAttribute('href') || '';
    let href = '';
    try { href = raw ? new URL(raw, location.href).href : ''; } catch (e) { href = ''; }
    el.setAttribute('data-genie-ct', String(i));
    out.push({i, text, href, visible, tag: el.tagName.toLowerCase(), label,
              cls: (typeof el.className === 'string' ? el.className : '').slice(0, 120)});
  });
  return out;
}
"""


@dataclass
class ClickResult:
    """What clicking through one page found."""

    outcome: str                      # one | modal | button | none
    url: str = ""                     # the address to publish when outcome == "one"
    note: str = ""
    destinations: list[str] = field(default_factory=list)
    controls: list[str] = field(default_factory=list)
    page: str = ""                    # for modal/button: the page that holds the controls


# --------------------------------------------------------------------------- #
#  Pure logic: which controls to try, and what the clicks add up to            #
# --------------------------------------------------------------------------- #

def _same_page(href: str, base_url: str) -> bool:
    return href.split("#")[0].rstrip("/") == base_url.split("#")[0].rstrip("/")


def is_toggle(control: dict, base_url: str) -> bool:
    """A control that acts on this page instead of navigating away from it."""
    href = (control.get("href") or "").strip()
    return (not href or href.lower().startswith("javascript")
            or ("#" in href and _same_page(href, base_url)))


def _self_link(control: dict, base_url: str) -> bool:
    """A plain link back to the page it is on: clicking it only reloads."""
    href = (control.get("href") or "").strip()
    return bool(href) and "#" not in href and _same_page(href, base_url)


def _student_wording(text: str) -> bool:
    text = unicodedata.normalize("NFC", text or "")
    return bool(G._STUDENT_AUDIENCE_RE.search(text) or _NATIVE_STUDENT_RE.search(text))


def _other_audience(text: str) -> bool:
    return bool(G._NON_STUDENT_AUDIENCE_RE.search(text or "")
                or G._NON_STUDENT_LINK_LABEL_RE.search(text or ""))


def score_control(control: dict, base_url: str) -> float:
    """How promising a control is as the way to a student login; <= 0 = skip."""
    text = (control.get("text") or control.get("label") or "").strip()
    href = (control.get("href") or "").strip()
    if _NOT_A_PAGE_RE.search(href) or _self_link(control, base_url):
        return -1
    student = _student_wording(text)
    login = G._has_login_word(text)
    service = bool(_SERVICE_WORD_RE.search(text))
    system = bool(_STUDENT_SYSTEM_RE.search(text))
    if not (login or system or (service and student)
            or (not text and G._has_login_word(href))):
        return -1
    if _other_audience(text) and not student:
        return -1
    if not is_toggle(control, base_url):
        if not G.portal_url_allowed(href)[0] or is_admission_url(href)[0]:
            return -1
    score = 1.0
    if student:
        score += 2
    if login:
        score += 1
    if system:
        score += 1
    if _LOGIN_PATH_RE.search(urlsplit(href).path if href else ""):
        score += 1
    if control.get("visible"):
        score += 0.5
    return score


def pick_controls(controls: Sequence[dict], base_url: str,
                  limit: int = MAX_CLICKS) -> list[dict]:
    """The controls worth clicking, best first, one per destination.

    Links come before buttons: a link names where it goes, so two controls for
    the same address are recognised as one.
    """
    scored = [(score_control(c, base_url), c) for c in controls]
    links = sorted(((s, c) for s, c in scored if s > 0 and not is_toggle(c, base_url)),
                   key=lambda sc: -sc[0])
    buttons = sorted(((s, c) for s, c in scored
                      if s > 0 and is_toggle(c, base_url)
                      and (c.get("tag") in {"button", "input"} or not c.get("href"))),
                     key=lambda sc: -sc[0])
    picks: list[dict] = []
    seen: set[str] = set()
    for _score, control in links + buttons:
        if is_toggle(control, base_url):
            key = "button:" + (control.get("text") or "").lower()
        else:
            try:
                key = G.portal_dedupe_key(control["href"])
            except ValueError:
                continue
        if key in seen:
            continue
        seen.add(key)
        picks.append(control)
        if len(picks) >= limit:
            break
    return picks


def pick_toggles(controls: Sequence[dict], base_url: str) -> list[dict]:
    """Menus to open before looking for a login: hamburgers, then student menus."""
    hamburgers, menus = [], []
    for control in controls:
        if not control.get("visible") or not is_toggle(control, base_url):
            continue
        text = (control.get("text") or "").strip()
        blob = f"{control.get('label') or ''} {control.get('cls') or ''}"
        if not text and _MENU_TOGGLE_RE.search(blob):
            hamburgers.append(control)
        elif text and len(text) <= 30 \
                and (_student_wording(text) or G._has_login_word(text)) \
                and not _NOT_A_STUDENT_SECTION_RE.search(text) \
                and not (_other_audience(text) and not _student_wording(text)):
            menus.append(control)
    return (hamburgers[:2] + menus)[:_MAX_TOGGLES]


def pick_sections(controls: Sequence[dict], base_url: str) -> list[str]:
    """Same-site "Students" pages worth a second look when the homepage has no login."""
    host = (urlsplit(base_url).hostname or "").lower()
    found: list[tuple[int, str]] = []
    seen: set[str] = set()
    for control in controls:
        href = (control.get("href") or "").split("#")[0]
        text = (control.get("text") or "").strip()
        if (not href.startswith("http") or _same_page(href, base_url)
                or (urlsplit(href).hostname or "").lower() != host):
            continue
        # A menu entry, not a headline: "Students", "Students' Corner",
        # "Alumnos" — and visible, so it is one the site actually offers.
        if not control.get("visible") or not text or len(text) > 30:
            continue
        if not _student_wording(text) or _other_audience(text) \
                or _NOT_A_STUDENT_SECTION_RE.search(f"{text} {href}"):
            continue
        if not G.portal_url_allowed(href)[0] or is_admission_url(href)[0]:
            continue
        if href not in seen:
            seen.add(href)
            found.append((len(text), href))
    return [href for _n, href in sorted(found, key=lambda f: f[0])][:_MAX_SECTIONS]


def publishable_destination(href: str, dest: str) -> str:
    """The reusable address for a login reached from ``href`` at ``dest``.

    A click can land on a one-time transaction or on an identity provider's
    screen. Neither is the portal, so the link that led there is published
    instead (marked by `decide` for a fresh reload); with no such link there
    is nothing stable to publish and "" is returned.
    """
    def portal_like(url: str) -> bool:
        return (not G._has_volatile_auth_state(url) and not G.is_identity_provider_url(url)
                and G.portal_url_allowed(url)[0])

    if dest and portal_like(dest):
        return G.canonical_portal_url(dest)
    if href.startswith("http") and portal_like(href):
        return G.canonical_portal_url(href)
    return ""


def decide(clicks: Sequence[dict], entry_url: str) -> ClickResult:
    """Turn the per-click findings into one outcome.

    Each click is ``{"control", "href", "dest", "verdict", "reason",
    "url_changed"}`` where ``verdict`` comes from
    `guardrails._classify_login_target`.
    """
    good = [c for c in clicks if c.get("verdict") == "student"]
    controls = [str(c.get("control") or c.get("href") or "") for c in clicks]
    if not good:
        tried = "; ".join(dict.fromkeys(
            str(c.get("reason") or "") for c in clicks if c.get("reason")))
        note = (f"clicked {len(clicks)} control(s), none led to a student login"
                + (f" ({tried[:160]})" if tried else "")) if clicks \
            else "no login control to click"
        return ClickResult("none", note=note, controls=controls)

    # The page a student should be sent to when no single login wins: the one
    # the working controls are on. That is the Students page when the homepage
    # itself had none.
    holders = list(dict.fromkeys(str(c.get("found_on") or entry_url) for c in good))
    keep = G.canonical_portal_url(holders[0] if len(holders) == 1 else entry_url)
    here = G.portal_dedupe_key(entry_url)
    destinations: dict[str, str] = {}
    labels: dict[str, str] = {}
    replaced: set[str] = set()        # keys published in place of a one-time URL
    student_labelled: set[str] = set()
    on_page = False
    for click in good:
        url = publishable_destination(click.get("href") or "", click.get("dest") or "") \
            if click.get("url_changed") else ""
        if not url or G.portal_dedupe_key(url) in (
                here, G.portal_dedupe_key(str(click.get("found_on") or entry_url))):
            on_page = True
            continue
        key = G.portal_dedupe_key(url)
        if G._replaces_one_time(url, str(click.get("dest") or "")):
            replaced.add(key)
        destinations.setdefault(key, url)
        labels.setdefault(key, str(click.get("control") or ""))
        if click.get("for_students"):
            student_labelled.add(key)
    found = list(destinations.values())

    def marked(key: str, note: str) -> str:
        return f"{note}; {G.REPLACED_ONE_TIME_NOTE}" if key in replaced else note

    if len(found) == 1 and not on_page:
        key = next(iter(destinations))
        return ClickResult("one", found[0],
                           marked(key, f"login reached by clicking {labels[key]!r}"),
                           found, controls)
    # "Student Login" beside a plain "Login": the page itself says which one is
    # for students, so that is the answer rather than a tie.
    if len(student_labelled) == 1:
        key = next(iter(student_labelled))
        return ClickResult(
            "one", destinations[key],
            marked(key, f"login reached by clicking {labels[key]!r} (the only "
                        f"student-labelled one of {len(found) + (1 if on_page else 0)})"),
            found, controls)
    if on_page:
        # A login opened on the page itself, so the page IS a login; any
        # other logins it links to are listed, not published.
        extra = f"; {len(found)} other login(s) linked" if found else ""
        return ClickResult("modal", note=f"login opens on the page after a click{extra}",
                           page=keep, destinations=found, controls=controls)
    # Several logins at different addresses and none on the page: publish the
    # best of them (a student system over a library, then the first offered),
    # never the page that only links to them.
    order = list(destinations)
    best = min(order, key=lambda k: _destination_rank(labels[k], destinations[k],
                                                       order.index(k)))
    return ClickResult(
        "one", destinations[best],
        marked(best, f"login reached by clicking {labels[best]!r} (best of {len(found)} "
                     "different student logins)"), found, controls)


def _destination_rank(label: str, url: str, order: int) -> tuple:
    """Sort key for logins reached from one page: lower is better."""
    blob = f"{label} {url}"
    return (0 if _student_wording(label) else 1,
            0 if _STUDENT_SYSTEM_RE.search(blob) or _SERVICE_WORD_RE.search(blob) else 1,
            1 if _LIBRARY_RE.search(blob) else 0,
            order)


# --------------------------------------------------------------------------- #
#  Browser driving                                                             #
# --------------------------------------------------------------------------- #

#: Ceiling for one read of a page or frame. Playwright's `evaluate` and
#: `content` have no timeout of their own, and a frame that never finishes
#: loading would otherwise hold a browser slot for good.
_READ_TIMEOUT_S = 8


async def _bounded(awaitable: Any, default: Any, seconds: float = _READ_TIMEOUT_S) -> Any:
    """Await with a ceiling; ``default`` on timeout or error (never on cancel)."""
    try:
        return await asyncio.wait_for(awaitable, timeout=seconds)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — detached or cross-origin frame, stuck script
        return default


def _dismiss_dialogs(page: Any) -> None:
    """An open alert() blocks every script on the page until it is answered."""
    def _on_dialog(dialog: Any) -> None:
        task = asyncio.ensure_future(dialog.dismiss())
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
    page.on("dialog", _on_dialog)


async def _collect(page: Any) -> list[dict]:
    """Every clickable control, including links inside frames."""
    out: list[dict] = []
    for frame in page.frames:
        got = await _bounded(frame.evaluate(_JS_COLLECT), None)
        if got is None:
            continue
        # Controls inside a frame cannot be clicked by id from the main page,
        # but a link there still names a destination that can be opened.
        out += got if frame == page.main_frame else [
            {**g, "in_frame": True} for g in got if g.get("href")]
    return out


async def _html_with_frames(page: Any) -> str:
    """Page HTML followed by each frame's, so a login form in an iframe counts."""
    parts = [await _bounded(page.content(), "")]
    for frame in page.frames:
        if frame == page.main_frame:
            continue
        parts.append(await _bounded(frame.content(), ""))
    return "\n".join(part for part in parts if part)


async def _settled_html(page: Any) -> str:
    """The page's HTML once its login form has had a fair chance to appear.

    Many portals are single-page apps: the address is right at once, but the
    form is drawn by script a few seconds later. When the first read shows no
    form, wait for the network to go quiet and read once more.
    """
    await page.wait_for_timeout(2500)
    html = await _html_with_frames(page)
    if G._login_form_evidence(html)[0]:
        return html
    try:
        await page.wait_for_load_state("networkidle", timeout=7000)
    except Exception:  # noqa: BLE001 — a page that never goes quiet
        pass
    await page.wait_for_timeout(1500)
    return await _html_with_frames(page)


async def _wait_until_drawn(page: Any) -> None:
    """Give a page time to finish building its menus before reading them.

    `domcontentloaded` fires before scripts have drawn a navigation bar, and
    how long that takes varies from load to load. Reading too early is what
    makes the same page yield a Login link on one run and nothing on the next.
    """
    for state, timeout in (("load", 12_000), ("networkidle", 6_000)):
        try:
            await page.wait_for_load_state(state, timeout=timeout)
        except Exception:  # noqa: BLE001 — a page that never settles
            pass
    await page.wait_for_timeout(1000)


async def _open_menus(page: Any, base_url: str) -> None:
    for control in pick_toggles(await _collect(page), base_url):
        locator = page.locator(f'[data-genie-ct="{control["i"]}"]').first
        for action in ("hover", "click"):
            try:
                if action == "hover":
                    await locator.hover(timeout=2000)
                else:
                    await locator.click(timeout=2000, no_wait_after=True)
                await page.wait_for_timeout(600)
            except Exception:  # noqa: BLE001 — hidden, covered or gone
                continue
        if not _same_page(page.url, base_url):
            # The "menu" was a real link after all; go back and stop opening.
            try:
                await page.goto(base_url, wait_until="domcontentloaded",
                                timeout=_NAV_TIMEOUT_MS)
                await page.wait_for_timeout(1200)
            except Exception:  # noqa: BLE001
                pass
            return


async def _follow(context: Any, page: Any, control: dict, base_url: str) -> dict:
    """Follow one control and report where it went and what was there."""
    href = control.get("href") or ""
    label = control.get("text") or control.get("label") or href
    click = {"control": label, "href": href,
             "for_students": _student_wording(str(label))}
    try:
        if href.startswith("http") and not is_toggle(control, base_url):
            tab = await context.new_page()
            try:
                await tab.goto(href, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS)
                html = await _settled_html(tab)
                dest = tab.url
            finally:
                await tab.close()
        else:
            if not _same_page(page.url, base_url):
                await page.goto(base_url, wait_until="domcontentloaded",
                                timeout=_NAV_TIMEOUT_MS)
                await page.wait_for_timeout(1500)
                await _open_menus(page, base_url)
                await _collect(page)          # re-tag after the reload
            locator = page.locator(f'[data-genie-ct="{control["i"]}"]').first
            try:
                async with context.expect_page(timeout=3500) as popup:
                    await locator.click(timeout=3500)
                tab = await popup.value
                await tab.wait_for_load_state("domcontentloaded", timeout=_NAV_TIMEOUT_MS)
                html = await _settled_html(tab)
                dest = tab.url
                await tab.close()
            except Exception:  # noqa: BLE001 — no new tab: the click acted here
                html = await _settled_html(page)
                dest = page.url
    except Exception as exc:  # noqa: BLE001
        return {**click, "verdict": "unknown",
                "reason": f"click failed: {type(exc).__name__}"}
    return {**click, "dest": dest, "html": html,
            "url_changed": not _same_page(dest, base_url)}


async def _explore(context: Any, page: Any, base_url: str, credit_url: str,
                   limit: int) -> list[dict]:
    await _open_menus(page, base_url)
    try:
        # Opening a menu can start a navigation; reading the page mid-load
        # returns nothing.
        await page.wait_for_load_state("domcontentloaded", timeout=5000)
    except Exception:  # noqa: BLE001
        pass
    clicks: list[dict] = []
    for control in pick_controls(await _collect(page), base_url, limit):
        click = await _follow(context, page, control, base_url)
        html = click.pop("html", "")
        if "verdict" not in click:
            verdict, reason = G._classify_login_target(
                click["href"] or click["dest"], click["dest"], html, credit_url,
                str(click.get("control") or ""))
            click.update(verdict=verdict, reason=reason)
        clicks.append(click)
    return clicks


async def _only_public(route: Any) -> None:
    """Abort any browser request to an address outside the public internet.

    Covers every navigation, redirect hop, pop-up and sub-resource, so a page
    cannot use a link, a script or a redirect to reach an internal service.
    """
    url = route.request.url
    if url.startswith(("http://", "https://")):
        host = urlsplit(url).hostname or ""
        if not await G._host_is_public(host):
            logger.info("click-through: blocked request to non-public %s", host)
            await route.abort("blockedbyclient")
            return
    await route.continue_()


async def _run(url: str, proxy_cfg: dict | None, limit: int) -> ClickResult | None:
    """One attempt through one route. ``None`` means the page did not load."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(
                proxy=proxy_cfg, user_agent=G.USER_AGENT,
                viewport={"width": 1366, "height": 900})
            context.set_default_timeout(15_000)
            context.on("page", _dismiss_dialogs)
            await context.route("**/*", _only_public)
            page = await context.new_page()
            try:
                response = await page.goto(url, wait_until="domcontentloaded",
                                           timeout=_NAV_TIMEOUT_MS)
            except Exception as exc:  # noqa: BLE001
                logger.info("click-through: %s did not load (%s)", url,
                            str(exc).split("\n")[0][:120])
                return None
            if response is not None and response.status >= 400:
                return None
            await _wait_until_drawn(page)
            base_url = page.url
            home_html = await _html_with_frames(page)
            # A sparse login on the institution's own site is fine when the
            # page that led to it is itself a student page; otherwise the
            # destination has to say who it is for.
            credit_url = base_url if G._student_audience_evidence(
                home_html, base_url)[0] else ""

            clicks = await _explore(context, page, base_url, credit_url, limit)
            if not any(c.get("verdict") == "student" for c in clicks):
                try:
                    if not _same_page(page.url, base_url):
                        await page.goto(base_url, wait_until="domcontentloaded",
                                        timeout=_NAV_TIMEOUT_MS)
                        await page.wait_for_timeout(1500)
                    sections = pick_sections(await _collect(page), base_url)
                    if not clicks and not sections:
                        # Nothing at all to try usually means the menu had not
                        # been drawn yet, not that the site has no menu.
                        await page.wait_for_timeout(4000)
                        clicks = await _explore(context, page, base_url, credit_url, limit)
                        sections = pick_sections(await _collect(page), base_url)
                except Exception as exc:  # noqa: BLE001
                    logger.info("click-through: %s sections unreadable (%s)", base_url,
                                type(exc).__name__)
                    sections = []
                logger.info("click-through: %s — %d control(s) tried, section pages: %s",
                            base_url, len(clicks), sections)
                for section in sections:
                    try:
                        await page.goto(section, wait_until="domcontentloaded",
                                        timeout=_NAV_TIMEOUT_MS)
                        await _wait_until_drawn(page)
                        here = page.url
                        verdict, reason = G._classify_login_target(
                            section, here, await _html_with_frames(page), credit_url)
                        if verdict == "student":
                            clicks.append({"control": f"section page {section}",
                                           "href": section, "dest": here,
                                           "verdict": verdict, "reason": reason,
                                           "url_changed": True})
                            continue
                        for click in await _explore(context, page, here, credit_url, 3):
                            # The controls are on the section page, so that is
                            # the page to keep if no single login wins.
                            click["found_on"] = here
                            clicks.append(click)
                    except Exception as exc:  # noqa: BLE001
                        logger.info("click-through: section %s failed (%s: %s)", section,
                                    type(exc).__name__, str(exc).split("\n")[0][:120])
                        continue
            result = decide(clicks, base_url)
            if result.outcome == "none" and credit_url:
                # This browser waits longer than the rendered verifier does, so
                # it can see a sign-in button that check missed on a slow
                # single-page app (ems.nu.ac.bd asks for a registration number
                # and a captcha, with no password field at all). Apply the
                # verifier's own rule: a student page with a Login button is
                # accepted as it stands.
                has_button, _links = G._login_links(home_html, base_url)
                if has_button and not is_admission_portal(base_url, home_html)[0]:
                    # Seen, but never opened onto a form: a person decides.
                    return ClickResult(
                        "button", note="login button on the page once it finished "
                                       "drawing, but no form seen behind it",
                        controls=result.controls, page=G.canonical_portal_url(base_url))
            return result
        finally:
            # Closing a browser whose proxy tunnel has died can hang; the slot
            # must come back either way.
            try:
                await asyncio.wait_for(asyncio.shield(browser.close()), timeout=10)
            except BaseException as exc:  # noqa: BLE001
                if isinstance(exc, asyncio.CancelledError):
                    raise


async def find_login_by_clicking(
    url: str, *, country_hint: str = "", max_clicks: int = MAX_CLICKS,
    budget_seconds: int = BUDGET_S,
) -> ClickResult:
    """Click through ``url`` looking for a student login. Never raises."""
    host = urlsplit(url).hostname or ""
    routes = [cfg for _name, cfg in _proxy.playwright_proxy_candidates(country_hint, host)]
    routes = routes or [None]

    # The budget starts once a browser slot is free: time spent queueing behind
    # other pages is not time this page used.
    async with G._get_render_sem():
        async def _all_routes() -> ClickResult:
            for cfg in routes:
                try:
                    result = await _run(url, cfg, max_clicks)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    logger.info("click-through: %s failed (%s)", url, type(exc).__name__)
                    result = None
                if result is not None:
                    return result
            return ClickResult("none", note="page did not load in the browser")

        try:
            return await asyncio.wait_for(_all_routes(), timeout=budget_seconds)
        except asyncio.TimeoutError:
            return ClickResult("none", note=f"click-through timed out at {budget_seconds}s")

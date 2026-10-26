"""Run GenieV3 against a Google Sheet and write normalized portal rows.

Each output row contains exactly one portal URL and that portal's matched legal
URL.  A separate worksheet records university-owned hosts that were blocked by
Cloudflare, Akamai, CAPTCHA, WAF, or another recognized anti-bot response.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

REPO = Path(__file__).resolve().parents[1]
# Always use the checked-out agent by default.  Pointing at an old /private/tmp
# snapshot can silently bypass current proxy routing, guardrails, and fixes.
DEFAULT_AGENT_ROOT = REPO
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
DEFAULT_SHEET_ID = "1S24-nC-ZhNPfFNE2WGVDmsj0NHJJU4x1W6i5dzY5iKU"

PORTAL_HEADERS = (
    "Org ID", "Org Name", "Country", "Website", "Portal URL", "T&C URL",
    "Portal Category", "Portal System", "Source Tab", "Source Row",
    "Run Mode", "Checked At",
)
BLOCK_HEADERS = (
    "Org ID", "Org Name", "Country", "Website", "Portal URL", "T&C URL",
    "Block Type", "Details", "Blocked Host", "Exit Country", "Source Tab",
    "Source Row", "Detected At",
)


def _load_agent(agent_root: Path):
    load_dotenv(REPO / ".env", override=True)
    sys.path.insert(0, str(agent_root))
    from agent.crawler import load_blocks  # noqa: PLC0415
    from agent.lookup import find_portals  # noqa: PLC0415
    from agent.graph_matcher import registrable_root  # noqa: PLC0415
    return find_portals, load_blocks, registrable_root


def _service():
    token = REPO / ".tokens" / "token-write.json"
    creds = (Credentials.from_authorized_user_file(str(token), SCOPES)
             if token.exists() else None)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    if not creds or not creds.valid:
        flow = InstalledAppFlow.from_client_secrets_file(
            str(REPO / "credentials.json"), SCOPES)
        creds = flow.run_local_server(port=0)
    token.parent.mkdir(parents=True, exist_ok=True)
    token.write_text(creds.to_json())
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def _escaped(tab: str) -> str:
    return tab.replace("'", "''")


def _ensure_tab(service, sheet_id: str, tab: str, headers: tuple[str, ...]) -> None:
    meta = service.spreadsheets().get(
        spreadsheetId=sheet_id, fields="sheets.properties.title").execute()
    titles = {item["properties"]["title"] for item in meta.get("sheets", [])}
    if tab not in titles:
        service.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={"requests": [{"addSheet": {"properties": {"title": tab}}}]},
        ).execute()
    current = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{_escaped(tab)}'!1:1").execute()
    values = current.get("values", [])
    if not values:
        service.spreadsheets().values().update(
            spreadsheetId=sheet_id, range=f"'{_escaped(tab)}'!A1",
            valueInputOption="RAW", body={"values": [list(headers)]},
        ).execute()
    elif tuple(values[0][: len(headers)]) != headers:
        raise RuntimeError(
            f"{tab!r} has an unexpected header. Expected {list(headers)!r}; "
            f"found {values[0]!r}"
        )


def _read(service, sheet_id: str, tab: str, a1: str = "A:Z") -> list[list[str]]:
    return service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{_escaped(tab)}'!{a1}",
        valueRenderOption="FORMATTED_VALUE",
    ).execute().get("values", [])


def _split_domains(value: str) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for item in re.split(r"[,;\n]+", value or ""):
        domain = item.strip().strip("`\"'")
        if not domain:
            continue
        host = (urlsplit(domain).hostname if "://" in domain else domain)
        host = (host or "").split("/")[0].split(":")[0].lower().removeprefix("www.")
        if host and host not in seen:
            seen.add(host)
            output.append(host)
    return output


def _cell(row: list[str], index: int) -> str:
    return str(row[index]).strip() if index < len(row) else ""


def _host_related(blocked_host: str, website: str, registrable_root) -> bool:
    blocked = blocked_host.lower().removeprefix("www.")
    site = website.lower().removeprefix("www.")
    return (blocked == site or blocked.endswith("." + site)
            or registrable_root(blocked) == registrable_root(site))


def _existing_keys(rows: list[list[str]], positions: tuple[int, ...]) -> set[tuple[str, ...]]:
    keys: set[tuple[str, ...]] = set()
    for row in rows[1:]:
        keys.add(tuple(_cell(row, pos).rstrip("/").casefold() for pos in positions))
    return keys


def _challenge_kind(reason: str) -> str:
    lowered = reason.casefold()
    if "cloudflare" in lowered or "challenge" in lowered:
        return "cloudflare_challenge"
    if "akamai" in lowered:
        return "akamai"
    if "captcha" in lowered:
        return "captcha"
    if "403" in lowered or "forbidden" in lowered or "access denied" in lowered:
        return "waf_403"
    return "anti_bot"


def _blocked_from_log(
    log_path: Path,
    organisations: list[dict[str, Any]],
    existing: set[tuple[str, ...]],
    source_tab: str,
    registrable_root,
) -> list[list[str]]:
    """Recover exact blocked candidate URLs rejected by endpoint verification.

    Guardrail probes intentionally do not place every 403 into the crawler's
    shared infrastructure cache. The run log retains the exact portal URL and
    rejection reason, so normalize recognized WAF/challenge events here.
    """
    if not log_path.exists():
        return []
    pattern = re.compile(
        r"lookup: (?P<website>\S+) — dropped (?P<url>https?://\S+) "
        r"\(not a verified login page: (?P<reason>[^)]*)\)"
    )
    challenge = re.compile(
        r"cloudflare|akamai|captcha|anti-?bot|bot challenge|\b403\b|"
        r"forbidden|access denied", re.I,
    )
    by_website: dict[str, list[dict[str, Any]]] = {}
    for org in organisations:
        for website in org["websites"]:
            for key in {website.casefold(), registrable_root(website).casefold()}:
                if org not in by_website.setdefault(key, []):
                    by_website[key].append(org)
    output: list[list[str]] = []
    for line in log_path.read_text(errors="replace").splitlines():
        match = pattern.search(line)
        if not match or not challenge.search(match.group("reason")):
            continue
        website = match.group("website").casefold().removeprefix("www.")
        orgs = by_website.get(website) or by_website.get(
            registrable_root(website).casefold()) or []
        if not orgs:
            continue
        portal_url = match.group("url").rstrip("`'\"")
        blocked_host = (urlsplit(portal_url).hostname or website).lower()
        reason = match.group("reason")
        kind = _challenge_kind(reason)
        for org in orgs:
            key = (
                org["org_id"].casefold(), portal_url.rstrip("/").casefold(),
                blocked_host.casefold(), kind.casefold(),
            )
            if key in existing:
                continue
            existing.add(key)
            output.append([
                org["org_id"], org["name"], org["country"], website,
                portal_url, "", kind, reason, blocked_host, "", source_tab,
                str(org["source_row"]),
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
            ])
    return output


async def run(args: argparse.Namespace) -> None:
    find_portals, load_blocks, registrable_root = _load_agent(args.agent_root)
    service = _service()
    output_tab = args.output_tab or f"{args.tab} Portals"
    _ensure_tab(service, args.sheet_id, output_tab, PORTAL_HEADERS)
    _ensure_tab(service, args.sheet_id, args.blocked_tab, BLOCK_HEADERS)

    source = _read(service, args.sheet_id, args.tab)
    if not source:
        raise RuntimeError(f"{args.tab!r} is empty")
    headers = [str(value).strip().casefold() for value in source[0]]

    def find_header(*names: str) -> int:
        for name in names:
            if name.casefold() in headers:
                return headers.index(name.casefold())
        raise RuntimeError(f"Missing required header; expected one of {names!r}")

    indexes = {
        "org_id": find_header("Org ID", "OrgID", "SheerID OrgID"),
        "name": find_header("Org Name", "University Name", "SheerID University Name"),
        "country": find_header("Country"),
        "website": find_header("Website", "Domain", "Email Domains", "SheerID Website Domain"),
    }
    if args.start_row < 2:
        raise ValueError("--start-row must be 2 or greater (row 1 contains headers)")
    if args.end_row is not None and args.end_row < args.start_row:
        raise ValueError("--end-row must be greater than or equal to --start-row")
    source_rows = [
        (number, row) for number, row in enumerate(source[1:], start=2)
        if number >= args.start_row
        and (args.end_row is None or number <= args.end_row)
        and any(str(value).strip() for value in row)
    ]
    pending = source_rows if args.limit <= 0 else source_rows[: args.limit]

    organisations = [{
        "source_row": number,
        "org_id": _cell(row, indexes["org_id"]),
        "name": _cell(row, indexes["name"]),
        "country": _cell(row, indexes["country"]),
        "websites": _split_domains(_cell(row, indexes["website"])),
    } for number, row in pending]

    portal_existing = _existing_keys(
        _read(service, args.sheet_id, output_tab), (0, 4))
    block_existing = _existing_keys(
        _read(service, args.sheet_id, args.blocked_tab), (0, 4, 8, 6))

    if args.sync_log_only:
        recovered = _blocked_from_log(
            args.log, organisations, block_existing, args.tab, registrable_root)
        if recovered:
            service.spreadsheets().values().append(
                spreadsheetId=args.sheet_id,
                range=f"'{_escaped(args.blocked_tab)}'!A:M",
                valueInputOption="RAW", insertDataOption="INSERT_ROWS",
                body={"values": recovered},
            ).execute()
        logging.info("SYNC BLOCKS rows=%d", len(recovered))
        return
    portal_rows: list[list[str]] = []
    block_rows: list[list[str]] = []
    result_lock = asyncio.Lock()
    semaphore = asyncio.Semaphore(max(1, args.concurrency))

    async def one(source_row: int, row: list[str]) -> dict[str, Any]:
        org_id = _cell(row, indexes["org_id"])
        name = _cell(row, indexes["name"])
        country = _cell(row, indexes["country"])
        websites = _split_domains(_cell(row, indexes["website"]))
        started = datetime.now(timezone.utc).isoformat(timespec="seconds")
        found: list[dict[str, Any]] = []
        errors: list[str] = []

        async with semaphore:
            for website in websites:
                try:
                    result = await asyncio.wait_for(
                        find_portals(
                            website, name=name, country=country,
                            portals_only=False,
                            fast_mode=args.mode in ("fast", "stable-fast"),
                            stable_fast=(args.mode == "stable-fast"),
                        ),
                        timeout=args.row_timeout_seconds,
                    )
                    if result.get("error"):
                        errors.append(f"{website}: {result['error']}")
                    for portal in result.get("portals") or []:
                        found.append({"website": website, **portal})
                except TimeoutError:
                    errors.append(f"{website}: deadline exceeded ({args.row_timeout_seconds}s)")
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{website}: {type(exc).__name__}: {exc}")

        checked = datetime.now(timezone.utc).isoformat(timespec="seconds")
        new_portal_rows: list[list[str]] = []
        seen_urls: set[str] = set()
        for portal in found:
            url = str(portal.get("url") or "").strip()
            key = (org_id.casefold(), url.rstrip("/").casefold())
            if not url or url.rstrip("/").casefold() in seen_urls or key in portal_existing:
                continue
            seen_urls.add(url.rstrip("/").casefold())
            portal_existing.add(key)
            new_portal_rows.append([
                org_id, name, country, portal["website"], url,
                str(portal.get("tnc_url") or ""),
                str(portal.get("category") or ""),
                str(portal.get("system") or ""), args.tab, str(source_row),
                args.mode, checked,
            ])

        blocks = load_blocks()
        new_block_rows: list[list[str]] = []
        for blocked_host, entry in blocks.items():
            if str(entry.get("last_seen") or "") < started:
                continue
            related = next((site for site in websites if _host_related(
                blocked_host, site, registrable_root)), "")
            if not related:
                continue
            block_type = str(entry.get("block_type") or "anti_bot")
            key = (org_id.casefold(), "", blocked_host.casefold(), block_type.casefold())
            if key in block_existing:
                continue
            block_existing.add(key)
            matching = next((portal for portal in found
                             if (urlsplit(str(portal.get("url") or "")).hostname or "").lower()
                             == blocked_host.lower()), {})
            new_block_rows.append([
                org_id, name, country, related,
                str(matching.get("url") or ""), str(matching.get("tnc_url") or ""),
                block_type, str(entry.get("detail") or ""), blocked_host,
                str(entry.get("exit_country") or ""), args.tab, str(source_row),
                str(entry.get("last_seen") or checked),
            ])

        async with result_lock:
            portal_rows.extend(new_portal_rows)
            block_rows.extend(new_block_rows)
        summary = {
            "row": source_row, "org_id": org_id, "name": name,
            "portals": len(new_portal_rows), "blocked": len(new_block_rows),
            "errors": errors,
        }
        logging.info("RESULT %s", json.dumps(summary, ensure_ascii=False))
        return summary

    logging.info(
        "START tab=%s rows=%d concurrency=%d output=%s blocked=%s",
        args.tab, len(pending), args.concurrency, output_tab, args.blocked_tab,
    )
    results = await asyncio.gather(*(one(number, row) for number, row in pending))

    if portal_rows:
        service.spreadsheets().values().append(
            spreadsheetId=args.sheet_id, range=f"'{_escaped(output_tab)}'!A:L",
            valueInputOption="RAW", insertDataOption="INSERT_ROWS",
            body={"values": portal_rows},
        ).execute()
    if block_rows:
        service.spreadsheets().values().append(
            spreadsheetId=args.sheet_id, range=f"'{_escaped(args.blocked_tab)}'!A:M",
            valueInputOption="RAW", insertDataOption="INSERT_ROWS",
            body={"values": block_rows},
        ).execute()
    recovered = _blocked_from_log(
        args.log, organisations, block_existing, args.tab, registrable_root)
    if recovered:
        service.spreadsheets().values().append(
            spreadsheetId=args.sheet_id, range=f"'{_escaped(args.blocked_tab)}'!A:M",
            valueInputOption="RAW", insertDataOption="INSERT_ROWS",
            body={"values": recovered},
        ).execute()
        block_rows.extend(recovered)
    logging.info(
        "DONE orgs=%d portal_rows=%d blocked_rows=%d orgs_with_errors=%d",
        len(results), len(portal_rows), len(block_rows),
        sum(bool(item["errors"]) for item in results),
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sheet-id", default=DEFAULT_SHEET_ID)
    parser.add_argument("--tab", required=True)
    parser.add_argument("--output-tab", default="")
    parser.add_argument("--blocked-tab", default="Blocked Sites")
    parser.add_argument(
        "--limit", type=int, default=10,
        help="maximum rows to process within the selected range; 0 means all",
    )
    parser.add_argument("--start-row", type=int, default=2,
                        help="first source sheet row to include (inclusive; default: 2)")
    parser.add_argument("--end-row", type=int, default=None,
                        help="last source sheet row to include (inclusive)")
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--row-timeout-seconds", type=int, default=180)
    parser.add_argument(
        "--mode", choices=("fast", "stable-fast", "deep"), default="stable-fast",
        help="fast skips Chromium; stable-fast adds bounded browser recovery; deep uses full crawl",
    )
    parser.add_argument("--agent-root", type=Path, default=DEFAULT_AGENT_ROOT)
    parser.add_argument("--log", type=Path, default=Path("/private/tmp/geniev3-normalized.log"))
    parser.add_argument("--sync-log-only", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
        handlers=[logging.FileHandler(args.log), logging.StreamHandler()],
    )
    asyncio.run(run(args))


if __name__ == "__main__":
    main()

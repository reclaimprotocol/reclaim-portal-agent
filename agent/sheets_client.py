"""Google Sheets client using OAuth 2.0 user credentials.

Sheet contract
--------------
The spreadsheet has two tabs and the agent treats them very differently:

* **Universities** — owned by SheerID, the source of truth. The agent only
  reads from this tab. No method on this class writes to it; this is
  enforced structurally, not by comment.
* **Portals** — the agent's output. Stage D's happy path is an upsert per
  OrgID (see `agent/stages/sheet_writer.py`): if a row exists for the
  OrgID it's overwritten in place, otherwise a new row is appended.
  `delete_portals_for_orgid` remains for the manual purge tool
  (scripts/purge_orgid.py) but is not called on the happy path.

All per-OrgID run state lives in SQLite (`state.db`); we deliberately do
not mirror it to the sheet.

Rate limits
-----------
All Sheets API calls go through `_execute_with_retry`, which retries with
exponential backoff on 429 / quota-exceeded / 503 responses.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Sequence

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

if TYPE_CHECKING:
    from .config import Config

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

logger = logging.getLogger(__name__)

_RETRYABLE_STATUSES = {429, 500, 502, 503, 504}


def _col_letter(n: int) -> str:
    """Convert 1-based column number to Sheets letters (1→A, 26→Z, 27→AA)."""
    if n < 1:
        raise ValueError(f"column number must be >= 1, got {n}")
    letters = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


class SheetsClient:
    # Canonical Portals-tab column order. ONE row per OrgID. Multi-value
    # columns (Portal URLs, T&C URLs) hold "\n"-joined strings — Google
    # Sheets renders the newline as an in-cell line break, matching the
    # SheerID Universities tab convention. Per-portal debug info (category,
    # JS-rendered, source, per-portal verdict, evidence, reasoning) lives
    # in `state.db` only — it doesn't get written here.
    PORTALS_COLUMNS: tuple[str, ...] = (
        "OrgID",                # 1
        "University Name",      # 2
        "Portal URLs",          # 3  (multiline — all portal URLs joined by "\n")
        "T&C URLs",             # 4  (multiline — unique T&C URLs joined by "\n")
        "Overall T&C Verdict",  # 5  (Yes / Maybe / No / "Yes (No T&C Found)")
    )

    ORGID_COLUMN_CANDIDATES: tuple[str, ...] = (
        "SheerID OrgID",
        "OrgID",
        "Org ID",
        "org_id",
        "orgid",
    )

    def __init__(
        self,
        sheet_id: str,
        universities_tab: str,
        portals_tab: str,
        credentials_path: Path,
        token_path: Path,
        *,
        max_retries: int = 3,
    ) -> None:
        self.sheet_id = sheet_id
        self.universities_tab = universities_tab
        self.portals_tab = portals_tab
        self._credentials_path = credentials_path
        self._token_path = token_path
        self._max_retries = max_retries
        self._service = build("sheets", "v4", credentials=self._load_creds())
        self._portals_sheet_id: int | None = None

    @classmethod
    def from_config(cls, config: "Config") -> "SheetsClient":
        return cls(
            sheet_id=config.google_sheet_id,
            universities_tab=config.universities_tab,
            portals_tab=config.portals_tab,
            credentials_path=config.google_credentials_path,
            token_path=config.google_token_path,
        )

    # ------------------------------------------------------- OrgID helpers

    @classmethod
    def extract_orgid(cls, row: dict[str, Any]) -> str:
        for key in cls.ORGID_COLUMN_CANDIDATES:
            val = row.get(key)
            if val in (None, ""):
                continue
            return cls._as_orgid_str(val)
        return ""

    @staticmethod
    def _as_orgid_str(val: Any) -> str:
        if isinstance(val, bool):
            return str(val).strip()
        if isinstance(val, float) and val.is_integer():
            return str(int(val))
        return str(val).strip()

    # --------------------------------------------------------------- auth

    def _load_creds(self) -> Credentials:
        creds: Credentials | None = None
        if self._token_path.exists():
            creds = Credentials.from_authorized_user_file(str(self._token_path), SCOPES)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                if not self._credentials_path.exists():
                    raise FileNotFoundError(
                        f"OAuth client file not found at {self._credentials_path}."
                    )
                flow = InstalledAppFlow.from_client_secrets_file(
                    str(self._credentials_path), SCOPES
                )
                creds = flow.run_local_server(port=0)
            self._token_path.write_text(creds.to_json())
        return creds

    # ----------------------------------------------------------- retries

    def _execute_with_retry(self, fn: Callable[[], Any], *, label: str) -> Any:
        attempt = 0
        while True:
            try:
                return fn()
            except HttpError as err:
                status = getattr(err.resp, "status", 0)
                if status in _RETRYABLE_STATUSES and attempt < self._max_retries:
                    wait = min(60.0, 2.0 ** attempt + 2.0)
                    logger.warning(
                        "Sheets %s: HTTP %s; backing off %.1fs (attempt %d/%d)",
                        label, status, wait, attempt + 1, self._max_retries,
                    )
                    time.sleep(wait)
                    attempt += 1
                    continue
                raise

    # ------------------------------------------------ Universities (read)

    def read_universities(self) -> list[dict[str, Any]]:
        return self._read_tab_as_dicts(self.universities_tab)

    # -------------------------------------------------- Portals (read+write)

    def read_portals(self) -> list[dict[str, Any]]:
        return self._read_tab_as_dicts(self.portals_tab)

    def read_portals_by_orgid(self, orgid: str) -> list[tuple[int, dict[str, Any]]]:
        """Return [(1-based sheet row, row dict), ...] for the given OrgID.

        Assumes row 1 is the header; data rows start at row 2.
        """
        target = self._as_orgid_str(orgid)
        rows = self._read_tab_as_dicts(self.portals_tab)
        out: list[tuple[int, dict[str, Any]]] = []
        for idx, row in enumerate(rows):
            if self._as_orgid_str(row.get("OrgID", "")) == target:
                out.append((idx + 2, row))
        return out

    def ensure_portals_header(self) -> None:
        """Write / extend the Portals tab header row to match PORTALS_COLUMNS.

        * Empty tab: write the full header.
        * Header matches PORTALS_COLUMNS exactly: no-op.
        * Header is a prefix of PORTALS_COLUMNS (fewer columns): extend in
          place by writing the missing column names to the cells to the right.
        * Header conflicts with PORTALS_COLUMNS at matching positions: log a
          warning; subsequent writes still go positionally.
        """
        existing = self._get_values(self.portals_tab, "1:1")
        header = list(existing[0]) if existing else []
        if not any(str(cell).strip() for cell in header):
            self._execute_with_retry(
                lambda: self._service.spreadsheets().values().update(
                    spreadsheetId=self.sheet_id,
                    range=f"{self.portals_tab}!A1",
                    valueInputOption="USER_ENTERED",
                    body={"values": [list(self.PORTALS_COLUMNS)]},
                ).execute(),
                label="write header",
            )
            logger.info(
                "Portals tab was empty; wrote header row (%d columns)",
                len(self.PORTALS_COLUMNS),
            )
            return

        if header == list(self.PORTALS_COLUMNS):
            return

        if len(header) < len(self.PORTALS_COLUMNS) and header == list(
            self.PORTALS_COLUMNS[: len(header)]
        ):
            missing = list(self.PORTALS_COLUMNS[len(header):])
            start_col = _col_letter(len(header) + 1)
            end_col = _col_letter(len(self.PORTALS_COLUMNS))
            self._execute_with_retry(
                lambda: self._service.spreadsheets().values().update(
                    spreadsheetId=self.sheet_id,
                    range=f"{self.portals_tab}!{start_col}1:{end_col}1",
                    valueInputOption="USER_ENTERED",
                    body={"values": [missing]},
                ).execute(),
                label=f"extend header by {len(missing)} column(s)",
            )
            logger.info(
                "Extended Portals header with %d new column(s): %s",
                len(missing), missing,
            )
            return

        logger.warning(
            "Portals tab header does not match PORTALS_COLUMNS.\n"
            "  sheet:    %s\n"
            "  expected: %s\n"
            "Proceeding with positional writes; the sheet's header row is left unchanged.",
            header, list(self.PORTALS_COLUMNS),
        )

    def delete_portals_for_orgid(self, orgid: str) -> int:
        """Delete every Portals row for this OrgID. Used by scripts/purge_orgid.py,
        NOT by Stage D's happy path (which uses a merge strategy)."""
        target_orgid = self._as_orgid_str(orgid)
        col_a = self._get_values(self.portals_tab, "A2:A100000")
        target_rows: list[int] = []
        for idx, row in enumerate(col_a):
            if row and self._as_orgid_str(row[0]) == target_orgid:
                target_rows.append(idx + 2)
        if not target_rows:
            return 0

        sheet_id = self._portals_tab_id()
        requests = [
            {
                "deleteDimension": {
                    "range": {
                        "sheetId": sheet_id,
                        "dimension": "ROWS",
                        "startIndex": row_num - 1,
                        "endIndex": row_num,
                    }
                }
            }
            for row_num in sorted(target_rows, reverse=True)
        ]
        self._execute_with_retry(
            lambda: self._service.spreadsheets().batchUpdate(
                spreadsheetId=self.sheet_id,
                body={"requests": requests},
            ).execute(),
            label=f"delete {len(target_rows)} rows for orgid={target_orgid}",
        )
        return len(target_rows)

    def append_portal_rows(self, rows: Sequence[dict[str, Any]]) -> int:
        """Bulk-append rows to the Portals tab in PORTALS_COLUMNS order."""
        if not rows:
            return 0
        values = [[row.get(col, "") for col in self.PORTALS_COLUMNS] for row in rows]
        for row, value_row in zip(rows, values):
            value_row[0] = self._as_orgid_str(row.get("OrgID", ""))
        self._execute_with_retry(
            lambda: self._service.spreadsheets().values().append(
                spreadsheetId=self.sheet_id,
                range=f"{self.portals_tab}!A:A",
                valueInputOption="USER_ENTERED",
                insertDataOption="INSERT_ROWS",
                body={"values": values},
            ).execute(),
            label=f"append {len(rows)} rows",
        )
        return len(rows)

    def update_portal_rows(self, ops: Sequence[tuple[int, Sequence[Any]]]) -> int:
        """Overwrite full rows via a single batchUpdate.

        Each op is `(1-based row number, list of values in PORTALS_COLUMNS order)`.
        """
        if not ops:
            return 0
        end_col = _col_letter(len(self.PORTALS_COLUMNS))
        data = [
            {
                "range": f"{self.portals_tab}!A{row_num}:{end_col}{row_num}",
                "values": [list(values)],
            }
            for row_num, values in ops
        ]
        self._execute_with_retry(
            lambda: self._service.spreadsheets().values().batchUpdate(
                spreadsheetId=self.sheet_id,
                body={"valueInputOption": "USER_ENTERED", "data": data},
            ).execute(),
            label=f"batch update {len(ops)} rows",
        )
        return len(ops)

    # --------------------------------------------------------- internals

    def _portals_tab_id(self) -> int:
        if self._portals_sheet_id is not None:
            return self._portals_sheet_id
        meta = self._execute_with_retry(
            lambda: self._service.spreadsheets().get(spreadsheetId=self.sheet_id).execute(),
            label="fetch spreadsheet metadata",
        )
        for sheet in meta.get("sheets", []):
            props = sheet.get("properties", {})
            if props.get("title") == self.portals_tab:
                self._portals_sheet_id = int(props["sheetId"])
                return self._portals_sheet_id
        raise RuntimeError(f"Tab {self.portals_tab!r} not found in spreadsheet")

    def _read_tab_as_dicts(self, tab: str) -> list[dict[str, Any]]:
        header_rows = self._get_values(tab, "1:1")
        if not header_rows:
            return []
        header = header_rows[0]
        data_rows = self._get_values(tab, "2:100000")
        out: list[dict[str, Any]] = []
        for row in data_rows:
            padded = list(row) + [""] * (len(header) - len(row))
            row_dict = dict(zip(header, padded))
            for key in self.ORGID_COLUMN_CANDIDATES:
                if key in row_dict and row_dict[key] not in (None, ""):
                    row_dict[key] = self._as_orgid_str(row_dict[key])
            out.append(row_dict)
        return out

    def _get_values(self, tab: str, a1: str) -> list[list[Any]]:
        resp = self._execute_with_retry(
            lambda: self._service.spreadsheets().values().get(
                spreadsheetId=self.sheet_id,
                range=f"{tab}!{a1}",
            ).execute(),
            label=f"read {tab}!{a1}",
        )
        return resp.get("values", [])

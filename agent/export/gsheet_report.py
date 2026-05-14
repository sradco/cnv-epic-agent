"""Create and read Google Sheets reports for cnv-epic-agent.

Public API
----------
build_gsheet(metadata, tallies, plan_collector, *, google_cfg)
    Create a new Google Sheet with the same structure as the XLSX report
    (Run Info, Release Planning <version>, QE & Docs Stories,
    Observability Stories) and return its URL.

read_gsheet_plan(sheet_url_or_id, *, google_cfg)
    Read approved stories from a Google Sheet and return
    (version, plan_collector) — identical contract to read_xlsx_plan.

Authentication
--------------
Priority order:
  1. google_cfg.credentials_file (path to service-account JSON)
  2. GOOGLE_APPLICATION_CREDENTIALS environment variable
  3. OAuth browser flow (gcloud ADC / installed-app flow)

The required OAuth / service-account scopes are:
  https://www.googleapis.com/auth/spreadsheets
  https://www.googleapis.com/auth/drive.file   (to set file parent)
"""

from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from schemas.config import GoogleConfig
    from schemas.stories import StoryPayload
    from agent.runner import _EpicTally

# ── Constants ─────────────────────────────────────────────────────────────────

_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]

# Palette matching xlsx_report.py
_HEADER_BG   = {"red": 0.122, "green": 0.286, "blue": 0.490}  # #1F497D
_HEADER_FG   = {"red": 1.0,   "green": 1.0,   "blue": 1.0}
_ALT_BG      = {"red": 0.863, "green": 0.902, "blue": 0.945}  # #DCE6F1

_STORY_SHEET_NAMES = ("QE & Docs Stories", "Observability Stories")
_REJECTED_VALUES   = {"no", "rejected", "reject", "n", "skip", "skipped"}

_QE_DOCS_CATEGORIES = {"qe", "docs"}
_OBS_CATEGORIES     = {"metrics", "alerts", "dashboards", "telemetry"}
_NOTABLE_LABELS     = {"cnv-observability"}


# ── Auth helper ───────────────────────────────────────────────────────────────

def _get_credentials(google_cfg: GoogleConfig):  # type: ignore[return]
    """Return Google credentials, trying SA key → env var → OAuth."""
    try:
        from google.oauth2 import service_account
        from google.auth import default as google_auth_default
        from google_auth_oauthlib.flow import InstalledAppFlow  # type: ignore
    except ImportError as exc:
        raise ImportError(
            "Google auth libraries are not installed. "
            "Run: pip install google-api-python-client "
            "google-auth google-auth-httplib2 "
            "google-auth-oauthlib"
        ) from exc

    key_file = google_cfg.credentials_file or os.environ.get(
        "GOOGLE_APPLICATION_CREDENTIALS", ""
    )
    if key_file:
        return service_account.Credentials.from_service_account_file(
            key_file, scopes=_SCOPES,
        )

    # Application Default Credentials (gcloud auth application-default login)
    try:
        creds, _ = google_auth_default(scopes=_SCOPES)
        return creds
    except Exception:
        pass

    # Fallback: installed-app OAuth (requires client_secrets.json in CWD)
    client_secrets = "client_secrets.json"
    if not os.path.exists(client_secrets):
        raise RuntimeError(
            "No Google credentials found.  Set one of:\n"
            "  • google.credentials_file in config.yaml\n"
            "  • GOOGLE_APPLICATION_CREDENTIALS env var\n"
            "  • gcloud auth application-default login\n"
            "  • Place client_secrets.json in the working directory"
        )
    flow = InstalledAppFlow.from_client_secrets_file(client_secrets, _SCOPES)
    return flow.run_local_server(port=0)


def _build_services(google_cfg: GoogleConfig):
    """Return (sheets_service, drive_service)."""
    from googleapiclient.discovery import build  # type: ignore
    creds = _get_credentials(google_cfg)
    sheets = build("sheets", "v4", credentials=creds)
    drive  = build("drive",  "v3", credentials=creds)
    return sheets, drive


# ── Sheet ID extraction ────────────────────────────────────────────────────────

def _extract_sheet_id(url_or_id: str) -> str:
    """Extract the spreadsheet ID from a URL or return the ID directly."""
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", url_or_id)
    if m:
        return m.group(1)
    # Assume it's already a bare ID
    return url_or_id.strip()


# ── Formatting helpers ────────────────────────────────────────────────────────

def _header_fmt(bold: bool = True) -> dict[str, Any]:
    return {
        "textFormat": {
            "bold": bold,
            "foregroundColor": _HEADER_FG,
        },
        "backgroundColor": _HEADER_BG,
        "horizontalAlignment": "CENTER",
        "verticalAlignment": "TOP",
        "wrapStrategy": "WRAP",
    }


def _data_fmt(wrap: bool = False) -> dict[str, Any]:
    fmt: dict[str, Any] = {
        "verticalAlignment": "TOP",
        "wrapStrategy": "WRAP" if wrap else "CLIP",
    }
    return fmt


def _repeat_cell_request(
    sheet_id: int,
    start_row: int,
    end_row: int,
    start_col: int,
    end_col: int,
    cell_format: dict[str, Any],
    fields: str,
) -> dict[str, Any]:
    return {
        "repeatCell": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": start_row,
                "endRowIndex":   end_row,
                "startColumnIndex": start_col,
                "endColumnIndex":   end_col,
            },
            "cell": {"userEnteredFormat": cell_format},
            "fields": fields,
        }
    }


def _freeze_row_request(sheet_id: int) -> dict[str, Any]:
    return {
        "updateSheetProperties": {
            "properties": {
                "sheetId": sheet_id,
                "gridProperties": {"frozenRowCount": 1},
            },
            "fields": "gridProperties.frozenRowCount",
        }
    }


def _col_width_request(
    sheet_id: int, col_idx: int, pixel_width: int,
) -> dict[str, Any]:
    return {
        "updateDimensionProperties": {
            "range": {
                "sheetId": sheet_id,
                "dimension": "COLUMNS",
                "startIndex": col_idx,
                "endIndex":   col_idx + 1,
            },
            "properties": {"pixelSize": pixel_width},
            "fields": "pixelSize",
        }
    }


def _alt_row_requests(
    sheet_id: int, num_data_rows: int,
) -> list[dict[str, Any]]:
    """Return repeatCell requests that shade every other data row."""
    requests = []
    for i in range(1, num_data_rows + 1):
        if i % 2 == 0:
            requests.append({
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": i,
                        "endRowIndex":   i + 1,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "backgroundColor": _ALT_BG,
                        }
                    },
                    "fields": "userEnteredFormat.backgroundColor",
                }
            })
    return requests


# ── Sheet data builders ───────────────────────────────────────────────────────

def _run_info_rows(metadata: dict[str, str]) -> list[list[str]]:
    return [["Field", "Value"]] + [[k, v] for k, v in metadata.items()]


def _summary_rows(
    tallies: list[_EpicTally],
    summary_only: bool = False,
) -> list[list[Any]]:
    has_components = any(t.components for t in tallies)

    cols: list[str] = ["Epic Key", "Summary", "Status"]
    if has_components:
        cols.append("Component")
    cols += ["Fix Version", "Target Version", "Labels"]
    if summary_only:
        cols += ["Dev SP (existing)", "QE SP (existing)", "Docs SP (existing)"]
    else:
        cols += [
            "Dev SP (existing)", "Dev SP (proposed)",
            "QE SP (existing)",  "QE SP (proposed)",
            "Docs SP (existing)", "Docs SP (proposed)",
            "Total Proposed SP",
        ]

    rows: list[list[Any]] = [cols]
    for t in tallies:
        notable = ", ".join(
            lb for lb in getattr(t, "labels", [])
            if lb in _NOTABLE_LABELS
        )
        row: list[Any] = [t.key, t.summary, t.status]
        if has_components:
            row.append(", ".join(t.components))
        row += [t.fix_version or "", t.target_version or "", notable]
        if summary_only:
            row += [
                t.dev_sp_existing,
                "no-qe"  if t.has_no_qe  else t.qe_sp_existing,
                "no-doc" if t.has_no_doc else t.docs_sp_existing,
            ]
        else:
            row += [
                t.dev_sp_existing,  t.dev_sp_proposed,
                "no-qe"  if t.has_no_qe  else t.qe_sp_existing,
                "no-qe"  if t.has_no_qe  else t.qe_sp_proposed,
                "no-doc" if t.has_no_doc else t.docs_sp_existing,
                "no-doc" if t.has_no_doc else t.docs_sp_proposed,
                t.dev_sp_proposed
                + (0 if t.has_no_qe  else t.qe_sp_proposed)
                + (0 if t.has_no_doc else t.docs_sp_proposed),
            ]
        rows.append(row)
    return rows


def _is_obs_sheet_story(story: StoryPayload) -> bool:
    if story.category in _OBS_CATEGORIES:
        return True
    return story.category in _QE_DOCS_CATEGORIES and bool(story.linked_to)


def _story_rows(
    plan_collector: dict[str, list[StoryPayload]],
    tallies: list[_EpicTally],
    obs_sheet: bool,
) -> list[list[Any]]:
    summary_map = {t.key: t.summary for t in tallies}
    comp_map    = {t.key: ", ".join(t.components) for t in tallies}

    cols: list[str] = [
        "Epic Key", "Epic Summary", "Component", "Category",
        "Story Summary", "Story Points", "Reasoning", "Description",
    ]
    if obs_sheet:
        cols.append("Covers proposed story")
    cols.append("Approved?")

    rows: list[list[Any]] = [cols]
    for epic_key, stories in sorted(plan_collector.items()):
        epic_summary = summary_map.get(epic_key, "")
        component    = comp_map.get(epic_key, "")
        for story in stories:
            belongs = (
                _is_obs_sheet_story(story) if obs_sheet
                else (
                    story.category in _QE_DOCS_CATEGORIES
                    and not story.linked_to
                )
            )
            if not belongs:
                continue
            row: list[Any] = [
                epic_key, epic_summary, component,
                story.category, story.summary,
                story.story_points or "", story.reasoning,
                story.description,
            ]
            if obs_sheet:
                row.append(story.linked_to or "")
            row.append("")   # Approved?
            rows.append(row)
    return rows


# ── Public: build_gsheet ──────────────────────────────────────────────────────

def build_gsheet(
    metadata: dict[str, str],
    tallies: list[_EpicTally],
    plan_collector: dict[str, list[StoryPayload]],
    *,
    google_cfg: GoogleConfig,
    summary_only: bool = False,
) -> str:
    """Create a Google Sheet and return its URL.

    Args:
        metadata:       Key/value pairs for the Run Info sheet.
        tallies:        Per-epic tally objects.
        plan_collector: Proposed stories keyed by epic key.
        google_cfg:     GoogleConfig (credentials, drive folder).
        summary_only:   When True omit proposed-SP columns and story sheets.

    Returns:
        The URL of the newly created Google Sheet.
    """
    version = metadata.get("Version", "")
    summary_sheet_name = (
        f"Release Planning {version}"
        if version and version != "(not set)"
        else "Release Planning"
    )

    # ── Collect sheet data ────────────────────────────────────────────
    sheet_defs: list[tuple[str, list[list[Any]]]] = [
        ("Run Info",          _run_info_rows(metadata)),
        (summary_sheet_name,  _summary_rows(tallies, summary_only=summary_only)),
    ]
    if not summary_only:
        sheet_defs += [
            ("QE & Docs Stories",    _story_rows(plan_collector, tallies, obs_sheet=False)),
            ("Observability Stories", _story_rows(plan_collector, tallies, obs_sheet=True)),
        ]

    sheets_svc, drive_svc = _build_services(google_cfg)

    # ── Create workbook ───────────────────────────────────────────────
    title = (
        f"CNV Epic Agent Report — {version} "
        f"({metadata.get('Date', '?')})"
    ).strip(" —")

    body = {
        "properties": {"title": title},
        "sheets": [
            {"properties": {"title": name, "index": i}}
            for i, (name, _) in enumerate(sheet_defs)
        ],
    }
    resp = (
        sheets_svc.spreadsheets()
        .create(body=body, fields="spreadsheetId")
        .execute()
    )
    spreadsheet_id = resp["spreadsheetId"]

    # Move to target Drive folder if configured.
    if google_cfg.drive_folder_id:
        drive_svc.files().update(
            fileId=spreadsheet_id,
            addParents=google_cfg.drive_folder_id,
            removeParents="root",
            fields="id, parents",
        ).execute()

    # Fetch sheet metadata to get real sheetIds.
    ss_meta = (
        sheets_svc.spreadsheets()
        .get(spreadsheetId=spreadsheet_id, fields="sheets.properties")
        .execute()
    )
    sheet_id_map: dict[str, int] = {
        s["properties"]["title"]: s["properties"]["sheetId"]
        for s in ss_meta["sheets"]
    }

    # ── Write data (batchUpdate values) ──────────────────────────────
    value_ranges = []
    for sheet_name, rows in sheet_defs:
        if not rows:
            continue
        # Convert all values to strings for the API (numbers stay numeric).
        api_rows = [
            [str(cell) if not isinstance(cell, (int, float)) else cell
             for cell in row]
            for row in rows
        ]
        value_ranges.append({
            "range": f"'{sheet_name}'!A1",
            "values": api_rows,
        })

    if value_ranges:
        sheets_svc.spreadsheets().values().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={
                "valueInputOption": "USER_ENTERED",
                "data": value_ranges,
            },
        ).execute()

    # ── Apply formatting ──────────────────────────────────────────────
    fmt_requests: list[dict[str, Any]] = []
    for sheet_name, rows in sheet_defs:
        sid = sheet_id_map[sheet_name]
        n_cols = len(rows[0]) if rows else 0
        n_rows = len(rows)

        # Header row formatting.
        fmt_requests.append(_repeat_cell_request(
            sid, 0, 1, 0, n_cols,
            _header_fmt(),
            "userEnteredFormat(textFormat,backgroundColor,"
            "horizontalAlignment,verticalAlignment,wrapStrategy)",
        ))
        # Freeze header row.
        fmt_requests.append(_freeze_row_request(sid))
        # Alt-row shading for data rows.
        fmt_requests.extend(_alt_row_requests(sid, n_rows - 1))

        # Column widths: wide columns for Reasoning and Description.
        if rows:
            headers = rows[0]
            for col_name, px in (("Reasoning", 400), ("Description", 500)):
                if col_name in headers:
                    fmt_requests.append(
                        _col_width_request(sid, headers.index(col_name), px)
                    )
            # Approved? column — narrow.
            if "Approved?" in headers:
                fmt_requests.append(
                    _col_width_request(
                        sid, headers.index("Approved?"), 100,
                    )
                )

    if fmt_requests:
        sheets_svc.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": fmt_requests},
        ).execute()

    return (
        f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit"
    )


# ── Public: read_gsheet_plan ──────────────────────────────────────────────────

def read_gsheet_plan(
    sheet_url_or_id: str,
    *,
    google_cfg: GoogleConfig,
) -> tuple[str, dict[str, list[StoryPayload]]]:
    """Read approved stories from a Google Sheet.

    Reads "QE & Docs Stories" and "Observability Stories" sheets.
    A row is included only when its "Approved?" cell is non-empty and not
    a rejection value (no / rejected / skip / n).

    Returns:
        (version, plan_collector) matching the contract of read_xlsx_plan.
    """
    from schemas.stories import StoryPayload as SP

    sheet_id   = _extract_sheet_id(sheet_url_or_id)
    sheets_svc, _ = _build_services(google_cfg)

    # ── Read version from Run Info ────────────────────────────────────
    version = ""
    try:
        ri = (
            sheets_svc.spreadsheets().values()
            .get(spreadsheetId=sheet_id, range="'Run Info'!A:B")
            .execute()
        )
        for row in ri.get("values", [])[1:]:
            if len(row) >= 2 and str(row[0]).strip() == "Version":
                version = str(row[1]).strip()
                break
    except Exception:
        pass

    # ── Read story sheets ─────────────────────────────────────────────
    plan: dict[str, list[SP]] = {}

    for sheet_name in _STORY_SHEET_NAMES:
        try:
            resp = (
                sheets_svc.spreadsheets().values()
                .get(spreadsheetId=sheet_id, range=f"'{sheet_name}'!A:Z")
                .execute()
            )
        except Exception:
            continue   # sheet doesn't exist or not readable

        all_rows = resp.get("values", [])
        if not all_rows:
            continue

        headers = [str(h).strip() for h in all_rows[0]]

        def _col(name: str) -> int | None:
            try:
                return headers.index(name)
            except ValueError:
                return None

        idx_epic    = _col("Epic Key")
        idx_cat     = _col("Category")
        idx_summary = _col("Story Summary")
        idx_sp      = _col("Story Points")
        idx_reason  = _col("Reasoning")
        idx_desc    = _col("Description")
        idx_linked  = _col("Covers proposed story")
        idx_approv  = _col("Approved?")

        if any(i is None for i in (
            idx_epic, idx_cat, idx_summary, idx_desc, idx_approv,
        )):
            continue   # malformed sheet — skip

        for row in all_rows[1:]:
            def _cell(idx: int | None) -> str:
                if idx is None or idx >= len(row):
                    return ""
                return str(row[idx] or "").strip()

            approved_raw = _cell(idx_approv).lower()
            if not approved_raw or approved_raw in _REJECTED_VALUES:
                continue

            epic_key = _cell(idx_epic)
            if not epic_key:
                continue

            try:
                sp = int(_cell(idx_sp)) if _cell(idx_sp) else None
            except ValueError:
                sp = None

            story = SP(
                category=_cell(idx_cat),
                summary=_cell(idx_summary),
                description=_cell(idx_desc),
                story_points=sp,
                reasoning=_cell(idx_reason),
                linked_to=_cell(idx_linked),
            )
            plan.setdefault(epic_key, []).append(story)

    return version, plan

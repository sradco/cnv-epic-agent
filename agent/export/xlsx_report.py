"""Export agent run results to a multi-sheet XLSX workbook.

Sheets produced:
  Run Info   — metadata (date, version, model, filters, run ID)
  Summary    — one row per epic (component, key, summary, status,
               version, dev/qe/docs SP existing + proposed)
  Stories    — one row per proposed story (epic key, component,
               category, summary, story points, reasoning)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

if TYPE_CHECKING:
    from agent.runner import _EpicTally, _RunCounters
    from schemas.stories import StoryPayload


# ── Palette ──────────────────────────────────────────────────────────────────

_HEADER_FILL = PatternFill("solid", fgColor="1F497D")   # dark blue
_HEADER_FONT = Font(bold=True, color="FFFFFF")
_ALT_FILL    = PatternFill("solid", fgColor="DCE6F1")   # light blue
_BOLD        = Font(bold=True)
_CENTER      = Alignment(horizontal="center", vertical="top", wrap_text=True)
_TOP_LEFT    = Alignment(horizontal="left",   vertical="top", wrap_text=True)


def _header_row(ws: Any, cols: list[str]) -> None:
    ws.append(cols)
    for cell in ws[ws.max_row]:
        cell.font = _HEADER_FONT
        cell.fill = _HEADER_FILL
        cell.alignment = _CENTER


def _auto_width(ws: Any, min_w: int = 10, max_w: int = 60) -> None:
    for col_cells in ws.columns:
        length = max(
            len(str(c.value or "")) for c in col_cells
        )
        ws.column_dimensions[
            get_column_letter(col_cells[0].column)
        ].width = min(max_w, max(min_w, length + 2))


def _freeze_top(ws: Any) -> None:
    ws.freeze_panes = "A2"


def _shade_alt_rows(ws: Any) -> None:
    """Shade every other data row (after the header)."""
    for i, row in enumerate(ws.iter_rows(min_row=2), start=1):
        if i % 2 == 0:
            for cell in row:
                cell.fill = _ALT_FILL


# ── Sheet builders ────────────────────────────────────────────────────────────

def _build_run_info_sheet(
    ws: Any,
    metadata: dict[str, str],
) -> None:
    ws.title = "Run Info"
    ws.append(["Field", "Value"])
    for cell in ws[1]:
        cell.font = _HEADER_FONT
        cell.fill = _HEADER_FILL
        cell.alignment = _CENTER
    for key, val in metadata.items():
        ws.append([key, val])
    _auto_width(ws)
    ws.column_dimensions["B"].width = 60


def _build_summary_sheet(
    ws: Any,
    tallies: list[_EpicTally],
    jira_url: str = "",
) -> None:
    ws.title = "Summary"

    has_components = any(t.components for t in tallies)

    cols = ["Epic Key", "Summary", "Status"]
    if has_components:
        cols.append("Component")
    cols += [
        "Fix Version", "Target Version",
        "Dev SP (existing)", "Dev SP (proposed)",
        "QE SP (existing)", "QE SP (proposed)",
        "Docs SP (existing)", "Docs SP (proposed)",
        "Total Proposed SP",
    ]
    _header_row(ws, cols)

    for tally in tallies:
        row: list[Any] = [tally.key, tally.summary, tally.status]
        if has_components:
            row.append(", ".join(tally.components))
        row += [
            tally.fix_version or "",
            tally.target_version or "",
            tally.dev_sp_existing,
            tally.dev_sp_proposed,
            "no-qe" if tally.has_no_qe else tally.qe_sp_existing,
            "no-qe" if tally.has_no_qe else tally.qe_sp_proposed,
            "no-doc" if tally.has_no_doc else tally.docs_sp_existing,
            "no-doc" if tally.has_no_doc else tally.docs_sp_proposed,
            tally.dev_sp_proposed + (
                0 if tally.has_no_qe else tally.qe_sp_proposed
            ) + (
                0 if tally.has_no_doc else tally.docs_sp_proposed
            ),
        ]
        ws.append(row)

        # Hyperlink the Epic Key cell to Jira if URL is configured.
        if jira_url:
            cell = ws.cell(ws.max_row, 1)
            cell.hyperlink = (
                f"{jira_url.rstrip('/')}/browse/{tally.key}"
            )
            cell.font = Font(color="0563C1", underline="single")

    _freeze_top(ws)
    _shade_alt_rows(ws)
    _auto_width(ws)


def _build_stories_sheet(
    ws: Any,
    plan_collector: dict[str, list[StoryPayload]],
    tallies: list[_EpicTally],
    jira_url: str = "",
) -> None:
    ws.title = "Stories"

    # Build a quick lookup: epic_key → component string
    comp_map: dict[str, str] = {
        t.key: ", ".join(t.components) for t in tallies
    }

    _header_row(ws, [
        "Epic Key", "Component", "Category",
        "Story Summary", "Story Points", "Reasoning",
    ])

    for epic_key, stories in sorted(plan_collector.items()):
        component = comp_map.get(epic_key, "")
        for story in stories:
            ws.append([
                epic_key,
                component,
                story.category,
                story.summary,
                story.story_points or "",
                story.reasoning,
            ])
            # Hyperlink the Epic Key cell.
            if jira_url:
                cell = ws.cell(ws.max_row, 1)
                cell.hyperlink = (
                    f"{jira_url.rstrip('/')}/browse/{epic_key}"
                )
                cell.font = Font(color="0563C1", underline="single")

    _freeze_top(ws)
    _shade_alt_rows(ws)
    _auto_width(ws)
    # Reasoning column is wide — let it wrap.
    ws.column_dimensions["F"].width = 60


# ── Public entry point ────────────────────────────────────────────────────────

def build_xlsx(
    path: str,
    *,
    metadata: dict[str, str],
    tallies: list[_EpicTally],
    plan_collector: dict[str, list[StoryPayload]],
    jira_url: str = "",
) -> None:
    """Write a multi-sheet XLSX workbook to *path*.

    Args:
        path:            Destination file path (e.g. ``report-20260511.xlsx``).
        metadata:        Key/value pairs for the Run Info sheet (date, model…).
        tallies:         Per-epic tally objects from ``_RunCounters``.
        plan_collector:  Proposed stories keyed by epic key.
        jira_url:        Base Jira URL for hyperlinks (optional).
    """
    wb = Workbook()
    wb.remove(wb.active)  # remove default empty sheet

    _build_run_info_sheet(wb.create_sheet("Run Info"), metadata)
    _build_summary_sheet(
        wb.create_sheet("Summary"), tallies, jira_url=jira_url,
    )
    if plan_collector:
        _build_stories_sheet(
            wb.create_sheet("Stories"),
            plan_collector, tallies, jira_url=jira_url,
        )

    wb.save(path)

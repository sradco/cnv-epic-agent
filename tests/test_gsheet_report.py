"""Tests for agent/export/gsheet_report.py.

All Google API calls are mocked so no credentials or network access
are required to run these tests.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch, call
import pytest

from agent.export.gsheet_report import (
    _extract_sheet_id,
    _is_obs_sheet_story,
    _story_rows,
    _summary_rows,
    build_gsheet,
    read_gsheet_plan,
)
from agent.runner import _EpicTally
from schemas.config import GoogleConfig
from schemas.stories import StoryPayload


# ── Helpers ───────────────────────────────────────────────────────────────────

def _gcfg() -> GoogleConfig:
    return GoogleConfig(credentials_file="/fake/key.json")


def _tally(key: str, **kwargs) -> _EpicTally:
    t = _EpicTally(key, status=kwargs.get("status", "groomed"))
    t.summary = kwargs.get("summary", "Epic summary")
    t.components = kwargs.get("components", [])
    t.labels = kwargs.get("labels", [])
    t.fix_version = kwargs.get("fix_version", "")
    t.target_version = kwargs.get("target_version", "")
    t.dev_sp_existing = kwargs.get("dev_ex", 0)
    t.dev_sp_proposed = kwargs.get("dev_pr", 0)
    t.qe_sp_existing  = kwargs.get("qe_ex", 0)
    t.qe_sp_proposed  = kwargs.get("qe_pr", 0)
    t.docs_sp_existing = kwargs.get("docs_ex", 0)
    t.docs_sp_proposed = kwargs.get("docs_pr", 0)
    t.has_no_qe  = kwargs.get("has_no_qe", False)
    t.has_no_doc = kwargs.get("has_no_doc", False)
    return t


def _story(
    category: str = "metrics",
    summary: str = "Add metric X",
    description: str = "desc",
    story_points: int = 3,
    reasoning: str = "r",
    linked_to: str = "",
) -> StoryPayload:
    return StoryPayload(
        category=category,
        summary=summary,
        description=description,
        story_points=story_points,
        reasoning=reasoning,
        linked_to=linked_to,
    )


# ── Unit tests (no API) ───────────────────────────────────────────────────────

class TestExtractSheetId:
    def test_extracts_id_from_full_url(self):
        url = (
            "https://docs.google.com/spreadsheets/d/"
            "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms/edit#gid=0"
        )
        assert _extract_sheet_id(url) == "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms"

    def test_bare_id_returned_unchanged(self):
        assert _extract_sheet_id("1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms") == \
               "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms"


class TestIsObsSheetStory:
    def test_metrics_goes_to_obs(self):
        assert _is_obs_sheet_story(_story("metrics"))

    def test_alerts_goes_to_obs(self):
        assert _is_obs_sheet_story(_story("alerts"))

    def test_qe_without_linked_to_goes_to_qe_docs(self):
        assert not _is_obs_sheet_story(_story("qe"))

    def test_qe_with_linked_to_goes_to_obs(self):
        assert _is_obs_sheet_story(_story("qe", linked_to="Add metric X"))

    def test_docs_with_linked_to_goes_to_obs(self):
        assert _is_obs_sheet_story(_story("docs", linked_to="Add dashboard"))


class TestStoryRows:
    def test_obs_sheet_includes_obs_stories(self):
        stories = {"CNV-100": [_story("metrics", summary="Add metric X")]}
        rows = _story_rows(stories, [_tally("CNV-100")], obs_sheet=True)
        summaries = [r[4] for r in rows[1:]]
        assert "Add metric X" in summaries

    def test_obs_sheet_excludes_unlinked_qe(self):
        stories = {"CNV-100": [_story("qe", summary="QE: test X")]}
        rows = _story_rows(stories, [_tally("CNV-100")], obs_sheet=True)
        assert len(rows) == 1  # header only

    def test_qe_docs_sheet_includes_unlinked_qe(self):
        stories = {"CNV-100": [_story("qe", summary="QE: test X")]}
        rows = _story_rows(stories, [_tally("CNV-100")], obs_sheet=False)
        summaries = [r[4] for r in rows[1:]]
        assert "QE: test X" in summaries

    def test_obs_sheet_has_covers_proposed_story_col(self):
        rows = _story_rows({}, [], obs_sheet=True)
        assert "Covers proposed story" in rows[0]

    def test_qe_docs_sheet_has_no_covers_proposed_story_col(self):
        rows = _story_rows({}, [], obs_sheet=False)
        assert "Covers proposed story" not in rows[0]

    def test_description_included_in_rows(self):
        stories = {
            "CNV-100": [_story("metrics", description="Full body text.")]
        }
        rows = _story_rows(stories, [_tally("CNV-100")], obs_sheet=True)
        all_values = [v for row in rows[1:] for v in row]
        assert "Full body text." in all_values


class TestSummaryRows:
    def test_header_row_present(self):
        rows = _summary_rows([])
        assert rows[0][0] == "Epic Key"

    def test_proposed_cols_absent_in_summary_only(self):
        rows = _summary_rows([], summary_only=True)
        assert "Dev SP (proposed)" not in rows[0]
        assert "Total Proposed SP" not in rows[0]

    def test_proposed_cols_present_normally(self):
        rows = _summary_rows([])
        assert "Dev SP (proposed)" in rows[0]


# ── Integration tests (mocked API) ───────────────────────────────────────────

def _mock_services(
    spreadsheet_id: str = "FAKE_ID",
    version: str = "5.0.0",
):
    """Return (sheets_mock, drive_mock) with sensible defaults."""
    sheets = MagicMock()
    drive  = MagicMock()

    summary_title = (
        f"Release Planning {version}" if version else "Release Planning"
    )

    # spreadsheets().create().execute()
    sheets.spreadsheets.return_value.create.return_value\
        .execute.return_value = {"spreadsheetId": spreadsheet_id}

    # spreadsheets().get().execute() — returns sheet metadata
    sheets.spreadsheets.return_value.get.return_value\
        .execute.return_value = {
        "sheets": [
            {"properties": {"title": "Run Info",           "sheetId": 0}},
            {"properties": {"title": summary_title,        "sheetId": 1}},
            {"properties": {"title": "QE & Docs Stories",  "sheetId": 2}},
            {"properties": {"title": "Observability Stories", "sheetId": 3}},
        ]
    }

    # values().batchUpdate().execute()
    sheets.spreadsheets.return_value.values.return_value\
        .batchUpdate.return_value.execute.return_value = {}

    # batchUpdate().execute() (formatting)
    sheets.spreadsheets.return_value.batchUpdate.return_value\
        .execute.return_value = {}

    return sheets, drive


class TestBuildGsheet:
    @patch("agent.export.gsheet_report._build_services")
    def test_returns_url_with_spreadsheet_id(self, mock_build):
        sheets, drive = _mock_services("MY_SHEET_ID")
        mock_build.return_value = (sheets, drive)

        url = build_gsheet(
            metadata={"Date": "2026-05-14", "Version": "5.0.0"},
            tallies=[_tally("CNV-100")],
            plan_collector={},
            google_cfg=_gcfg(),
        )
        assert "MY_SHEET_ID" in url
        assert "docs.google.com" in url

    @patch("agent.export.gsheet_report._build_services")
    def test_values_batchupdate_called(self, mock_build):
        sheets, drive = _mock_services()
        mock_build.return_value = (sheets, drive)

        build_gsheet(
            metadata={"Date": "2026-05-14", "Version": "5.0.0"},
            tallies=[_tally("CNV-100")],
            plan_collector={},
            google_cfg=_gcfg(),
        )
        assert sheets.spreadsheets.return_value.values.return_value\
               .batchUpdate.called

    @patch("agent.export.gsheet_report._build_services")
    def test_drive_move_called_when_folder_set(self, mock_build):
        sheets, drive = _mock_services("SID", version="5.0")
        mock_build.return_value = (sheets, drive)

        cfg = GoogleConfig(
            credentials_file="/fake/key.json",
            drive_folder_id="FOLDER123",
        )
        build_gsheet(
            metadata={"Date": "2026", "Version": "5.0"},
            tallies=[],
            plan_collector={},
            google_cfg=cfg,
        )
        drive.files.return_value.update.assert_called_once()

    @patch("agent.export.gsheet_report._build_services")
    def test_drive_move_not_called_without_folder(self, mock_build):
        sheets, drive = _mock_services(version="5.0")
        mock_build.return_value = (sheets, drive)

        build_gsheet(
            metadata={"Date": "2026", "Version": "5.0"},
            tallies=[],
            plan_collector={},
            google_cfg=_gcfg(),
        )
        drive.files.return_value.update.assert_not_called()

    @patch("agent.export.gsheet_report._build_services")
    def test_summary_only_skips_story_sheets(self, mock_build):
        sheets, drive = _mock_services(version="5.0")
        mock_build.return_value = (sheets, drive)

        # Capture the body passed to create().
        created_bodies: list[dict] = []
        def _capture_create(body, fields):
            created_bodies.append(body)
            return sheets.spreadsheets.return_value.create.return_value
        sheets.spreadsheets.return_value.create.side_effect = _capture_create

        build_gsheet(
            metadata={"Date": "2026", "Version": "5.0"},
            tallies=[],
            plan_collector={},
            google_cfg=_gcfg(),
            summary_only=True,
        )
        if created_bodies:
            sheet_titles = [
                s["properties"]["title"]
                for s in created_bodies[0].get("sheets", [])
            ]
            assert "QE & Docs Stories" not in sheet_titles
            assert "Observability Stories" not in sheet_titles


class TestReadGsheetPlan:
    def _make_sheets_svc(
        self,
        run_info_rows: list[list],
        qe_docs_rows: list[list],
        obs_rows: list[list],
    ):
        svc = MagicMock()
        def _get_side_effect(spreadsheetId, range):  # noqa: N803
            if "Run Info" in range:
                return MagicMock(execute=MagicMock(
                    return_value={"values": run_info_rows}
                ))
            if "QE & Docs" in range:
                return MagicMock(execute=MagicMock(
                    return_value={"values": qe_docs_rows}
                ))
            return MagicMock(execute=MagicMock(
                return_value={"values": obs_rows}
            ))
        svc.spreadsheets.return_value.values.return_value\
            .get.side_effect = _get_side_effect
        return svc

    @patch("agent.export.gsheet_report._build_services")
    def test_reads_version_from_run_info(self, mock_build):
        svc = self._make_sheets_svc(
            [["Field", "Value"], ["Version", "5.0.0"]],
            [], [],
        )
        mock_build.return_value = (svc, MagicMock())

        version, _ = read_gsheet_plan("FAKE_ID", google_cfg=_gcfg())
        assert version == "5.0.0"

    @patch("agent.export.gsheet_report._build_services")
    def test_approved_row_included(self, mock_build):
        headers = [
            "Epic Key", "Epic Summary", "Component", "Category",
            "Story Summary", "Story Points", "Reasoning",
            "Description", "Approved?",
        ]
        data_row = [
            "CNV-100", "Epic", "", "metrics",
            "Add metric X", "3", "r", "Full desc", "Yes",
        ]
        svc = self._make_sheets_svc(
            [["Field", "Value"], ["Version", "5.0"]],
            [],
            [headers, data_row],
        )
        mock_build.return_value = (svc, MagicMock())

        _, plan = read_gsheet_plan("FAKE_ID", google_cfg=_gcfg())
        assert "CNV-100" in plan
        assert plan["CNV-100"][0].summary == "Add metric X"
        assert plan["CNV-100"][0].description == "Full desc"

    @patch("agent.export.gsheet_report._build_services")
    def test_empty_approved_excluded(self, mock_build):
        headers = [
            "Epic Key", "Epic Summary", "Component", "Category",
            "Story Summary", "Story Points", "Reasoning",
            "Description", "Approved?",
        ]
        data_row = ["CNV-100", "", "", "metrics", "Add metric X", "3", "r", "d", ""]
        svc = self._make_sheets_svc(
            [["Field", "Value"], ["Version", "5.0"]],
            [],
            [headers, data_row],
        )
        mock_build.return_value = (svc, MagicMock())

        _, plan = read_gsheet_plan("FAKE_ID", google_cfg=_gcfg())
        assert plan == {}

    @patch("agent.export.gsheet_report._build_services")
    def test_rejected_excluded(self, mock_build):
        headers = [
            "Epic Key", "Epic Summary", "Component", "Category",
            "Story Summary", "Story Points", "Reasoning",
            "Description", "Approved?",
        ]
        data_row = ["CNV-100", "", "", "metrics", "M", "3", "r", "d", "No"]
        svc = self._make_sheets_svc(
            [["Field", "Value"], ["Version", "5.0"]],
            [],
            [headers, data_row],
        )
        mock_build.return_value = (svc, MagicMock())

        _, plan = read_gsheet_plan("FAKE_ID", google_cfg=_gcfg())
        assert plan == {}

    @patch("agent.export.gsheet_report._build_services")
    def test_linked_to_round_trips(self, mock_build):
        headers = [
            "Epic Key", "Epic Summary", "Component", "Category",
            "Story Summary", "Story Points", "Reasoning",
            "Description", "Covers proposed story", "Approved?",
        ]
        data_row = [
            "CNV-200", "", "", "qe",
            "QE: verify metric", "2", "r", "d",
            "Add metric X", "Approved",
        ]
        svc = self._make_sheets_svc(
            [["Field", "Value"], ["Version", "5.0"]],
            [],
            [headers, data_row],
        )
        mock_build.return_value = (svc, MagicMock())

        _, plan = read_gsheet_plan("FAKE_ID", google_cfg=_gcfg())
        assert "CNV-200" in plan
        assert plan["CNV-200"][0].linked_to == "Add metric X"

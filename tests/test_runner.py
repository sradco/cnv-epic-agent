"""Integration-style tests for agent.runner with mocked Jira and LLM."""

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agent.planner.llm import LLMError


def _fake_jira_issue(
    key, summary, description="", issue_type="Epic", labels=None,
    components=None,
):
    issue = MagicMock()
    issue.key = key
    issue.fields = MagicMock()
    issue.fields.summary = summary
    issue.fields.description = description
    issuetype = MagicMock()
    issuetype.name = issue_type
    issue.fields.issuetype = issuetype
    issue.fields.labels = labels or []
    issue.fields.components = components or []
    return issue


def _minimal_cfg():
    return {
        "jira": {
            "url": "https://example.atlassian.net",
            "default_project": "CNV",
            "default_since_days": 30,
        },
        "agent": {
            "enabled_categories": ["metrics", "alerts"],
            "default_model": "gpt-4o",
            "max_stories_per_run": 10,
            "temperature": 0.2,
            "story_points": {"enabled": False},
        },
        "creation": {
            "project": "CNV",
            "component": "Test Component",
            "story_label": "epic-agent-generated",
            "epic_label": "cnv-observability",
        },
        "repos": {},
    }


class TestRunnerDryRun:
    @patch("agent.runner._load_config")
    @patch("agent.runner.get_jira_client")
    @patch("agent.runner.build_all_inventories", return_value={})
    @patch("agent.runner.fetch_epic_with_children")
    @patch("agent.runner.build_analysis_result")
    @patch("agent.runner.compose_stories")
    @patch("agent.runner.find_or_create_obs_epic")
    @patch("agent.runner.find_existing_obs_stories", return_value=[])
    def test_dry_run_generates_report(
        self,
        mock_find_existing,
        mock_find_epic,
        mock_compose,
        mock_analysis,
        mock_fetch,
        mock_inv,
        mock_jira,
        mock_config,
    ):
        from agent.runner import run
        from schemas.stories import StoryPayload

        mock_config.return_value = _minimal_cfg()
        mock_jira.return_value = MagicMock()

        _LONG_DESC = (
            "This epic covers adding new metrics for VM migration "
            "tracking across the cluster for capacity planning."
        )
        epic = _fake_jira_issue(
            "CNV-100", "Test Epic", description=_LONG_DESC,
        )
        mock_jira.return_value.issue.return_value = epic

        from schemas.issue_doc import IssueDoc
        mock_fetch.return_value = (
            IssueDoc(
                key="CNV-100", summary="Test Epic",
                description=_LONG_DESC,
            ),
            [],
        )
        mock_analysis.return_value = {
            "epic_key": "CNV-100",
            "epic_summary": "Test Epic",
            "epic_description": _LONG_DESC,
            "child_issues": [],
            "domain_keywords": [],
            "need_state": "needed",
            "need_confidence": "high",
            "gaps": ["metrics"],
            "feature_types": [],
            "proposals": {},
            "dashboard_targets": [],
            "telemetry_suggestions": [],
            "recommended_action": "create now",

            "would_create_count": 1,
        }
        mock_compose.return_value = [
            StoryPayload(
                category="metrics",
                summary="[Observability] Add metrics for CNV-100",
                description="Description here",
                story_points=3,
            ),
        ]
        mock_find_epic.return_value = {
            "key": "(DRY-RUN)",
            "summary": "Obs epic",
            "created": False,
        }

        report = run(
            epic_keys=["CNV-100"],
            version="4.18",
            apply=False,
            use_llm=True,
        )

        assert "DRY-RUN" in report
        assert "WOULD CREATE" in report
        assert "CNV-100" in report
        assert "Run ID:" in report
        assert "(3sp)" in report
        assert "Description here" in report
        assert "**Category:** metrics" in report

    @patch("agent.runner._load_config")
    @patch("agent.runner.get_jira_client")
    @patch("agent.runner.build_all_inventories", return_value={})
    @patch("agent.runner.fetch_epic_with_children")
    @patch("agent.runner.build_analysis_result")
    @patch("agent.runner.compose_stories")
    def test_llm_error_surfaced_in_report(
        self,
        mock_compose,
        mock_analysis,
        mock_fetch,
        mock_inv,
        mock_jira,
        mock_config,
    ):
        from agent.runner import run
        from schemas.issue_doc import IssueDoc

        _LONG_DESC = (
            "This epic covers migrating observability components "
            "to a new architecture for better scalability."
        )
        mock_config.return_value = _minimal_cfg()
        mock_jira.return_value = MagicMock()
        mock_jira.return_value.issue.return_value = _fake_jira_issue(
            "CNV-200", "Failing Epic", description=_LONG_DESC,
        )

        mock_fetch.return_value = (
            IssueDoc(
                key="CNV-200", summary="Failing Epic",
                description=_LONG_DESC,
            ),
            [],
        )
        mock_analysis.return_value = {
            "epic_key": "CNV-200",
            "epic_summary": "Failing Epic",
            "epic_description": _LONG_DESC,
            "child_issues": [],
            "domain_keywords": [],
            "need_state": "needed",
            "need_confidence": "high",
            "gaps": ["metrics"],
            "feature_types": [],
            "proposals": {},
            "dashboard_targets": [],
            "telemetry_suggestions": [],
            "recommended_action": "create now",

            "would_create_count": 1,
        }
        mock_compose.side_effect = LLMError("timeout after 600s")

        report = run(
            epic_keys=["CNV-200"],
            apply=False,
            use_llm=True,
        )

        assert "LLM ERROR" in report
        assert "timeout" in report
        assert "1 LLM errors" in report

    @patch("agent.runner._load_config")
    @patch("agent.runner.get_jira_client")
    @patch("agent.runner.build_all_inventories", return_value={})
    @patch("agent.runner.fetch_epic_with_children")
    @patch("agent.runner.build_analysis_result")
    @patch("agent.runner.compose_stories")
    @patch("agent.runner.find_or_create_obs_epic")
    @patch("agent.runner.find_existing_obs_stories")
    def test_dedup_skips_duplicates(
        self,
        mock_find_existing,
        mock_find_epic,
        mock_compose,
        mock_analysis,
        mock_fetch,
        mock_inv,
        mock_jira,
        mock_config,
    ):
        from agent.runner import run
        from schemas.stories import StoryPayload

        _LONG_DESC = (
            "This epic covers adding new deduplication logic for "
            "observability stories to prevent duplicate Jira issues."
        )
        mock_config.return_value = _minimal_cfg()
        mock_jira.return_value = MagicMock()
        mock_jira.return_value.issue.return_value = _fake_jira_issue(
            "CNV-300", "Dedup Epic", description=_LONG_DESC,
        )

        from schemas.issue_doc import IssueDoc
        mock_fetch.return_value = (
            IssueDoc(
                key="CNV-300", summary="Dedup Epic",
                description=_LONG_DESC,
            ),
            [],
        )
        mock_analysis.return_value = {
            "epic_key": "CNV-300",
            "epic_summary": "Dedup Epic",
            "epic_description": _LONG_DESC,
            "child_issues": [],
            "domain_keywords": [],
            "need_state": "needed",
            "need_confidence": "high",
            "gaps": ["metrics"],
            "feature_types": [],
            "proposals": {},
            "dashboard_targets": [],
            "telemetry_suggestions": [],
            "recommended_action": "create now",

            "would_create_count": 1,
        }
        mock_compose.return_value = [
            StoryPayload(
                category="metrics",
                summary="[Observability][metrics] Add metrics for CNV-300",
                description="Desc",
                story_points=3,
            ),
        ]
        mock_find_epic.return_value = {
            "key": "(DRY-RUN)", "summary": "Obs", "created": False,
        }
        mock_find_existing.return_value = [
            {
                "key": "CNV-999",
                "summary": "[Observability][metrics] Add metrics for CNV-300",
                "labels": ["epic-agent-generated"],
                "description": "",
            },
        ]

        report = run(
            epic_keys=["CNV-300"],
            version="4.18",
            apply=False,
            use_llm=True,
        )

        assert "SKIP (dup)" in report
        assert "1 skipped" in report


class TestChildrenCrossCheck:
    """Verify that LLM-generated stories are deduped against
    the source epic's existing child issues."""

    @patch("agent.runner._load_config")
    @patch("agent.runner.get_jira_client")
    @patch("agent.runner.build_all_inventories", return_value={})
    @patch("agent.runner.fetch_epic_with_children")
    @patch("agent.runner.build_analysis_result")
    @patch("agent.runner.compose_stories")
    @patch("agent.runner.find_or_create_obs_epic")
    @patch("agent.runner.find_existing_obs_stories", return_value=[])
    def test_child_summary_match_skips(
        self,
        mock_find_existing,
        mock_find_epic,
        mock_compose,
        mock_analysis,
        mock_fetch,
        mock_inv,
        mock_jira,
        mock_config,
    ):
        """A story whose summary matches a source-epic child is skipped."""
        from agent.runner import run
        from schemas.issue_doc import IssueDoc
        from schemas.stories import StoryPayload

        mock_config.return_value = _minimal_cfg()
        mock_jira.return_value = MagicMock()
        mock_jira.return_value.issue.return_value = _fake_jira_issue(
            "CNV-500", "GPU Metrics Epic",
        )

        child = IssueDoc(
            key="CNV-501",
            summary="Add Prometheus metric kubevirt_vmi_gpu_info",
            description="Implement the GPU info gauge",
        )
        mock_fetch.return_value = (
            IssueDoc(
                key="CNV-500",
                summary="GPU Metrics Epic",
                description="Desc",
            ),
            [child],
        )
        mock_analysis.return_value = {
            "epic_key": "CNV-500",
            "epic_summary": "GPU Metrics Epic",
            "epic_description": "Desc",
            "child_issues": [{"key": "CNV-501", "summary": child.summary}],
            "domain_keywords": [],
            "need_state": "needed",
            "need_confidence": "high",
            "gaps": ["metrics"],
            "feature_types": [],
            "proposals": {},
            "dashboard_targets": [],
            "telemetry_suggestions": [],
            "recommended_action": "create now",

            "would_create_count": 1,
        }
        mock_compose.return_value = [
            StoryPayload(
                category="metrics",
                summary="Add Prometheus metric kubevirt_vmi_gpu_info",
                description="LLM wants to create this",
                story_points=5,
            ),
            StoryPayload(
                category="alerts",
                summary="Add GPU alert rule",
                description="Genuinely new alert",
                story_points=3,
            ),
        ]
        mock_find_epic.return_value = {
            "key": "(DRY-RUN)", "summary": "Obs", "created": False,
        }

        report = run(
            epic_keys=["CNV-500"],
            version="4.22",
            apply=False,
            use_llm=True,
        )

        assert "SKIP (dup)" in report
        assert "kubevirt_vmi_gpu_info" in report
        assert "WOULD CREATE" in report
        assert "GPU alert rule" in report
        assert "1 skipped" in report

    @patch("agent.runner._load_config")
    @patch("agent.runner.get_jira_client")
    @patch("agent.runner.build_all_inventories", return_value={})
    @patch("agent.runner.fetch_epic_with_children")
    @patch("agent.runner.build_analysis_result")
    @patch("agent.runner.compose_stories")
    @patch("agent.runner.find_or_create_obs_epic")
    @patch("agent.runner.find_existing_obs_stories", return_value=[])
    def test_unrelated_child_does_not_skip(
        self,
        mock_find_existing,
        mock_find_epic,
        mock_compose,
        mock_analysis,
        mock_fetch,
        mock_inv,
        mock_jira,
        mock_config,
    ):
        """A story with a different summary than any child is NOT skipped."""
        from agent.runner import run
        from schemas.issue_doc import IssueDoc
        from schemas.stories import StoryPayload

        mock_config.return_value = _minimal_cfg()
        mock_jira.return_value = MagicMock()
        mock_jira.return_value.issue.return_value = _fake_jira_issue(
            "CNV-600", "Networking Epic",
        )

        child = IssueDoc(
            key="CNV-601",
            summary="Implement VNIC hotplug",
            description="Totally unrelated child",
        )
        mock_fetch.return_value = (
            IssueDoc(
                key="CNV-600",
                summary="Networking Epic",
                description="Desc",
            ),
            [child],
        )
        mock_analysis.return_value = {
            "epic_key": "CNV-600",
            "epic_summary": "Networking Epic",
            "epic_description": "Desc",
            "child_issues": [{"key": "CNV-601", "summary": child.summary}],
            "domain_keywords": [],
            "need_state": "needed",
            "need_confidence": "high",
            "gaps": ["alerts"],
            "feature_types": [],
            "proposals": {},
            "dashboard_targets": [],
            "telemetry_suggestions": [],
            "recommended_action": "create now",

            "would_create_count": 1,
        }
        mock_compose.return_value = [
            StoryPayload(
                category="alerts",
                summary="Add alert for networking degradation",
                description="New alert",
                story_points=3,
            ),
        ]
        mock_find_epic.return_value = {
            "key": "(DRY-RUN)", "summary": "Obs", "created": False,
        }

        report = run(
            epic_keys=["CNV-600"],
            version="4.22",
            apply=False,
            use_llm=True,
        )

        assert "SKIP (dup)" not in report
        assert "WOULD CREATE" in report
        assert "networking degradation" in report


class TestChildrenAsDedup:
    def test_converts_issue_docs_to_dedup_entries(self):
        from agent.runner import _children_as_dedup_entries
        from schemas.issue_doc import IssueDoc

        children = [
            IssueDoc(
                key="CNV-10",
                summary="Add GPU metric",
                description="Body",
            ),
            IssueDoc(
                key="CNV-11",
                summary="Implement recording rule",
                description="Another body",
            ),
        ]
        entries = _children_as_dedup_entries(children)
        assert len(entries) == 2
        assert entries[0]["key"] == "CNV-10"
        assert entries[0]["summary"] == "Add GPU metric"
        assert entries[0]["description"] == "Body"
        assert entries[0]["labels"] == []
        assert entries[1]["key"] == "CNV-11"

    def test_empty_children(self):
        from agent.runner import _children_as_dedup_entries

        assert _children_as_dedup_entries([]) == []


class TestLabelBasedCategoryFiltering:
    """Verify that no-doc / no-qe labels remove categories."""

    @patch("agent.runner._load_config")
    @patch("agent.runner.get_jira_client")
    @patch("agent.runner.build_all_inventories", return_value={})
    @patch("agent.runner.fetch_epic_with_children")
    @patch("agent.runner.build_analysis_result")
    @patch("agent.runner.compose_stories")
    @patch("agent.runner.find_or_create_obs_epic")
    @patch("agent.runner.find_existing_obs_stories", return_value=[])
    def test_no_doc_label_skips_docs_stories(
        self,
        mock_find_existing,
        mock_find_epic,
        mock_compose,
        mock_analysis,
        mock_fetch,
        mock_inv,
        mock_jira,
        mock_config,
    ):
        from agent.runner import run
        from schemas.stories import StoryPayload

        cfg = _minimal_cfg()
        cfg["agent"]["enabled_categories"] = [
            "metrics", "docs", "qe",
        ]
        mock_config.return_value = cfg
        mock_jira.return_value = MagicMock()

        _LONG_DESC = (
            "Move observability code between repos without "
            "changing user-facing metrics or alert behavior."
        )
        epic = _fake_jira_issue(
            "CNV-200", "Internal refactor", description=_LONG_DESC,
        )
        mock_jira.return_value.issue.return_value = epic

        from schemas.issue_doc import IssueDoc
        mock_fetch.return_value = (
            IssueDoc(
                key="CNV-200", summary="Internal refactor",
                description=_LONG_DESC, labels=["no-doc"],
            ),
            [],
        )
        mock_analysis.return_value = {
            "epic_key": "CNV-200",
            "epic_summary": "Internal refactor",
            "epic_description": _LONG_DESC,
            "epic_labels": ["no-doc"],
            "child_issues": [],
            "domain_keywords": [],
            "need_state": "needed",
            "need_confidence": "high",
            "gaps": ["metrics"],
            "feature_types": [],
            "proposals": {},
            "dashboard_targets": [],
            "telemetry_suggestions": [],
            "recommended_action": "create now",

            "would_create_count": 1,
        }
        mock_compose.return_value = [
            StoryPayload(
                category="metrics",
                summary="[Observability] Add metric",
                description="desc",
                story_points=3,
            ),
        ]
        mock_find_epic.return_value = {
            "key": "(DRY-RUN)",
            "summary": "Obs epic",
            "created": False,
        }

        report = run(
            epic_keys=["CNV-200"],
            version="4.23",
            apply=False,
            use_llm=True,
        )

        call_args = mock_compose.call_args
        categories_passed = call_args.kwargs.get("categories")
        assert "docs" not in categories_passed
        assert "metrics" in categories_passed
        assert "qe" in categories_passed


class TestGroomingDetection:
    """Verify epics with insufficient detail are flagged for grooming."""

    @patch("agent.runner.build_all_inventories")
    @patch("agent.runner.get_jira_client")
    @patch("agent.runner.search_epics")
    @patch("agent.runner.fetch_epic_with_children")
    @patch("agent.runner._validate_config")
    @patch("agent.runner.yaml")
    def test_dry_run_reports_needs_grooming(
        self, mock_yaml, mock_validate, mock_fetch, mock_search,
        mock_client, mock_inv,
    ):
        from agent.runner import run

        cfg = _minimal_cfg()
        cfg["grooming"] = {
            "label": "grooming",
            "min_description_length": 50,
            "min_children": 1,
        }
        mock_yaml.safe_load.return_value = cfg

        epic_issue = _fake_jira_issue(
            "CNV-300", "Sparse epic", description="Short",
        )
        mock_search.return_value = [epic_issue]

        from schemas.issue_doc import IssueDoc
        sparse_epic = IssueDoc(
            key="CNV-300", summary="Sparse epic",
            description="Short",
        )
        mock_fetch.return_value = (sparse_epic, [])

        report = run(
            epic_keys=["CNV-300"],
            version="4.23",
            apply=False,
            use_llm=False,
        )

        assert "NEEDS GROOMING" in report
        assert "grooming" in report.lower()
        assert "Would add" in report

    @patch("agent.runner.build_all_inventories")
    @patch("agent.runner.get_jira_client")
    @patch("agent.runner.search_epics")
    @patch("agent.runner.fetch_epic_with_children")
    @patch("agent.runner._validate_config")
    @patch("agent.runner.yaml")
    def test_detailed_epic_not_flagged(
        self, mock_yaml, mock_validate, mock_fetch, mock_search,
        mock_client, mock_inv,
    ):
        from agent.runner import run

        cfg = _minimal_cfg()
        cfg["grooming"] = {
            "label": "grooming",
            "min_description_length": 50,
            "min_children": 1,
        }
        mock_yaml.safe_load.return_value = cfg

        detailed_desc = (
            "This epic covers moving all metrics from kubevirt "
            "core into a separate monitoring repository."
        )
        epic_issue = _fake_jira_issue(
            "CNV-301", "Detailed epic", description=detailed_desc,
        )
        mock_search.return_value = [epic_issue]

        from schemas.issue_doc import IssueDoc
        epic = IssueDoc(
            key="CNV-301", summary="Detailed epic",
            description=detailed_desc,
        )
        mock_fetch.return_value = (epic, [])

        report = run(
            epic_keys=["CNV-301"],
            version="4.23",
            apply=False,
            use_llm=False,
        )

        assert "NEEDS GROOMING" not in report


class TestLLMClarityCheck:
    """Verify LLM clarity check integration in the runner."""

    @patch("agent.runner.check_epic_clarity")
    @patch("agent.runner.build_all_inventories")
    @patch("agent.runner.get_jira_client")
    @patch("agent.runner.search_epics")
    @patch("agent.runner.fetch_epic_with_children")
    @patch("agent.runner._validate_config")
    @patch("agent.runner.yaml")
    def test_llm_flags_vague_epic(
        self, mock_yaml, mock_validate, mock_fetch, mock_search,
        mock_client, mock_inv, mock_clarity,
    ):
        from agent.runner import run

        cfg = _minimal_cfg()
        cfg["grooming"] = {
            "label": "grooming",
            "min_description_length": 50,
            "min_children": 1,
            "llm_clarity_check": True,
        }
        mock_yaml.safe_load.return_value = cfg

        _LONG_DESC = (
            "Improve the observability of the system by adding "
            "better monitoring and alerting capabilities."
        )
        epic_issue = _fake_jira_issue(
            "CNV-400", "Improve observability",
            description=_LONG_DESC,
        )
        mock_search.return_value = [epic_issue]

        from schemas.issue_doc import IssueDoc
        epic = IssueDoc(
            key="CNV-400", summary="Improve observability",
            description=_LONG_DESC,
        )
        mock_fetch.return_value = (epic, [])

        mock_clarity.return_value = {
            "verdict": "needs_grooming",
            "reason": (
                "The epic says 'improve observability' but does "
                "not specify which components or metrics."
            ),
        }

        report = run(
            epic_keys=["CNV-400"],
            version="4.23",
            apply=False,
            use_llm=True,
        )

        assert "NEEDS GROOMING" in report
        assert "does not specify which components" in report
        mock_clarity.assert_called_once()

    @patch("agent.runner.compose_stories")
    @patch("agent.runner.check_epic_clarity")
    @patch("agent.runner.build_all_inventories")
    @patch("agent.runner.get_jira_client")
    @patch("agent.runner.search_epics")
    @patch("agent.runner.fetch_epic_with_children")
    @patch("agent.runner.build_analysis_result")
    @patch("agent.runner._validate_config")
    @patch("agent.runner.yaml")
    def test_llm_passes_clear_epic(
        self, mock_yaml, mock_validate, mock_analysis,
        mock_fetch, mock_search, mock_client, mock_inv,
        mock_clarity, mock_compose,
    ):
        from agent.runner import run

        cfg = _minimal_cfg()
        cfg["grooming"] = {
            "label": "grooming",
            "min_description_length": 50,
            "min_children": 1,
            "llm_clarity_check": True,
        }
        mock_yaml.safe_load.return_value = cfg

        _LONG_DESC = (
            "Separate KubeVirt observability components into the "
            "kubevirt/monitoring repository. Move metrics, alerts, "
            "and dashboards. Keep metric names unchanged."
        )
        epic_issue = _fake_jira_issue(
            "CNV-401", "Separate observability repo",
            description=_LONG_DESC,
        )
        mock_search.return_value = [epic_issue]

        from schemas.issue_doc import IssueDoc
        epic = IssueDoc(
            key="CNV-401", summary="Separate observability repo",
            description=_LONG_DESC,
        )
        mock_fetch.return_value = (epic, [])

        mock_clarity.return_value = {
            "verdict": "clear",
            "reason": "Scope is well-defined.",
        }
        mock_analysis.return_value = {
            "epic_key": "CNV-401",
            "epic_summary": "Separate observability repo",
            "epic_description": _LONG_DESC,
            "epic_labels": [],
            "epic_components": [],
            "child_issues": [],
            "domain_keywords": [],
            "need_state": "not_needed",
            "need_confidence": "high",
            "gaps": [],
            "feature_types": [],
            "proposals": {},
            "dashboard_targets": [],
            "telemetry_suggestions": [],
            "recommended_action": "no gaps found",

            "would_create_count": 0,
        }
        mock_compose.return_value = []

        report = run(
            epic_keys=["CNV-401"],
            version="4.23",
            apply=False,
            use_llm=True,
        )

        assert "NEEDS GROOMING" not in report
        assert "Separate observability repo" in report
        assert "No stories generated" in report
        mock_clarity.assert_called_once()
        mock_compose.assert_called_once()

    @patch("agent.runner.compose_stories")
    @patch("agent.runner.build_all_inventories")
    @patch("agent.runner.get_jira_client")
    @patch("agent.runner.search_epics")
    @patch("agent.runner.fetch_epic_with_children")
    @patch("agent.runner.build_analysis_result")
    @patch("agent.runner._validate_config")
    @patch("agent.runner.yaml")
    def test_llm_check_skipped_when_disabled(
        self, mock_yaml, mock_validate, mock_analysis,
        mock_fetch, mock_search, mock_client, mock_inv,
        mock_compose,
    ):
        from agent.runner import run

        cfg = _minimal_cfg()
        cfg["grooming"] = {
            "label": "grooming",
            "min_description_length": 50,
            "min_children": 1,
            "llm_clarity_check": False,
        }
        mock_yaml.safe_load.return_value = cfg

        _LONG_DESC = (
            "Vague epic but clarity check disabled so it should "
            "proceed to analysis without LLM grooming check."
        )
        epic_issue = _fake_jira_issue(
            "CNV-402", "Some epic", description=_LONG_DESC,
        )
        mock_search.return_value = [epic_issue]

        from schemas.issue_doc import IssueDoc
        epic = IssueDoc(
            key="CNV-402", summary="Some epic",
            description=_LONG_DESC,
        )
        mock_fetch.return_value = (epic, [])
        mock_analysis.return_value = {
            "epic_key": "CNV-402",
            "epic_summary": "Some epic",
            "epic_description": _LONG_DESC,
            "epic_labels": [],
            "epic_components": [],
            "child_issues": [],
            "domain_keywords": [],
            "need_state": "not_needed",
            "need_confidence": "high",
            "gaps": [],
            "feature_types": [],
            "proposals": {},
            "dashboard_targets": [],
            "telemetry_suggestions": [],
            "recommended_action": "no gaps found",

            "would_create_count": 0,
        }
        mock_compose.return_value = []

        report = run(
            epic_keys=["CNV-402"],
            version="4.23",
            apply=False,
            use_llm=True,
        )

        assert "NEEDS GROOMING" not in report
        assert "Some epic" in report
        assert "No stories generated" in report
        mock_compose.assert_called_once()


class TestRunnerConfigValidation:
    def test_invalid_config_raises_at_startup(self):
        from agent.runner import ConfigError, _validate_config

        with pytest.raises(ConfigError):
            _validate_config({
                "agent": {
                    "enabled_categories": ["nonexistent"],
                },
            })


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))

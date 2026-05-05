"""Integration-style tests for agent.runner with mocked Jira and LLM."""

from unittest.mock import MagicMock, patch

import pytest

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
    from schemas.config import AppConfig
    return AppConfig.from_dict({
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
    })


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

        assert "## Summary" in report
        assert "Epics processed | 1" in report
        assert "Stories would create | 1" in report
        assert "Agent Proposed Stories" in report
        assert "CNV-100" in report
        assert "groomed" in report

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
        assert "LLM errors | 1" in report

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

        assert "SKIP (dup of" in report
        assert "Stories skipped (dup) | 1" in report


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

        assert "SKIP (dup of" in report
        assert "kubevirt_vmi_gpu_info" in report
        assert "WOULD CREATE" in report
        assert "GPU alert rule" in report
        assert "Stories skipped (dup) | 1" in report

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

        assert "SKIP (dup of" not in report
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
        assert entries[0]["_from_children"] is True
        assert entries[1]["key"] == "CNV-11"
        assert entries[1]["_from_children"] is True

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

        from schemas.config import AppConfig
        cfg_dict = {
            **_minimal_cfg().raw,
            "agent": {
                **_minimal_cfg().raw.get("agent", {}),
                "enabled_categories": ["metrics", "docs", "qe"],
            },
        }
        mock_config.return_value = AppConfig.from_dict(cfg_dict)
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

    @patch("agent.runner.days_since_last_agent_comment",
           return_value=None)
    @patch("agent.runner.build_all_inventories")
    @patch("agent.runner.get_jira_client")
    @patch("agent.runner.search_epics")
    @patch("agent.runner.fetch_epic_with_children")
    @patch("agent.runner._load_config")
    def test_dry_run_reports_needs_grooming(
        self, mock_config, mock_fetch, mock_search,
        mock_client, mock_inv, mock_days,
    ):
        from agent.runner import run
        from schemas.config import AppConfig

        cfg_dict = {
            **_minimal_cfg().raw,
            "grooming": {
                "label": "grooming",
                "min_description_length": 50,
                "min_children": 1,
            },
        }
        mock_config.return_value = AppConfig.from_dict(cfg_dict)

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
        assert "Sparse epic" in report
        assert "grooming" in report.lower()
        assert "Would add" in report
        assert "Epics needing grooming | 1" in report

    @patch("agent.runner.days_since_last_agent_comment",
           return_value=3.0)
    @patch("agent.runner.build_all_inventories")
    @patch("agent.runner.get_jira_client")
    @patch("agent.runner.search_epics")
    @patch("agent.runner.fetch_epic_with_children")
    @patch("agent.runner._load_config")
    def test_grooming_comment_throttled_when_recent(
        self, mock_config, mock_fetch, mock_search,
        mock_client, mock_inv, mock_days,
    ):
        from agent.runner import run
        from schemas.config import AppConfig

        cfg_dict = {
            **_minimal_cfg().raw,
            "grooming": {
                "label": "grooming",
                "min_description_length": 50,
                "min_children": 1,
                "comment_cooldown_days": 7,
            },
        }
        mock_config.return_value = AppConfig.from_dict(cfg_dict)

        epic_issue = _fake_jira_issue(
            "CNV-350", "Sparse epic", description="Short",
        )
        mock_search.return_value = [epic_issue]

        from schemas.issue_doc import IssueDoc
        sparse_epic = IssueDoc(
            key="CNV-350", summary="Sparse epic",
            description="Short",
        )
        mock_fetch.return_value = (sparse_epic, [])

        report = run(
            epic_keys=["CNV-350"],
            version="4.23",
            apply=False,
            use_llm=False,
        )

        assert "NEEDS GROOMING" in report
        assert "comment skipped" in report
        assert "3d ago" in report

    @patch("agent.runner.build_all_inventories")
    @patch("agent.runner.get_jira_client")
    @patch("agent.runner.search_epics")
    @patch("agent.runner.fetch_epic_with_children")
    @patch("agent.runner._load_config")
    def test_detailed_epic_not_flagged(
        self, mock_config, mock_fetch, mock_search,
        mock_client, mock_inv,
    ):
        from agent.runner import run
        from schemas.config import AppConfig

        cfg_dict = {
            **_minimal_cfg().raw,
            "grooming": {
                "label": "grooming",
                "min_description_length": 50,
                "min_children": 1,
            },
        }
        mock_config.return_value = AppConfig.from_dict(cfg_dict)

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

    @patch("agent.runner.days_since_last_agent_comment",
           return_value=None)
    @patch("agent.runner.check_epic_clarity")
    @patch("agent.runner.build_all_inventories")
    @patch("agent.runner.get_jira_client")
    @patch("agent.runner.search_epics")
    @patch("agent.runner.fetch_epic_with_children")
    @patch("agent.runner._load_config")
    def test_llm_flags_vague_epic(
        self, mock_config, mock_fetch, mock_search,
        mock_client, mock_inv, mock_clarity, mock_days,
    ):
        from agent.runner import run
        from schemas.config import AppConfig

        cfg_dict = {
            **_minimal_cfg().raw,
            "grooming": {
                "label": "grooming",
                "min_description_length": 50,
                "min_children": 1,
                "llm_clarity_check": True,
            },
        }
        mock_config.return_value = AppConfig.from_dict(cfg_dict)

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
    @patch("agent.runner._load_config")
    def test_llm_passes_clear_epic(
        self, mock_config, mock_analysis,
        mock_fetch, mock_search, mock_client, mock_inv,
        mock_clarity, mock_compose,
    ):
        from agent.runner import run
        from schemas.config import AppConfig

        cfg_dict = {
            **_minimal_cfg().raw,
            "grooming": {
                "label": "grooming",
                "min_description_length": 50,
                "min_children": 1,
                "llm_clarity_check": True,
            },
        }
        mock_config.return_value = AppConfig.from_dict(cfg_dict)

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
        assert "No stories to create" in report
        assert "nothing to report" in report
        mock_clarity.assert_called_once()
        mock_compose.assert_called_once()

    @patch("agent.runner.compose_stories")
    @patch("agent.runner.build_all_inventories")
    @patch("agent.runner.get_jira_client")
    @patch("agent.runner.search_epics")
    @patch("agent.runner.fetch_epic_with_children")
    @patch("agent.runner.build_analysis_result")
    @patch("agent.runner._load_config")
    def test_llm_check_skipped_when_disabled(
        self, mock_config, mock_analysis,
        mock_fetch, mock_search, mock_client, mock_inv,
        mock_compose,
    ):
        from agent.runner import run
        from schemas.config import AppConfig

        cfg_dict = {
            **_minimal_cfg().raw,
            "grooming": {
                "label": "grooming",
                "min_description_length": 50,
                "min_children": 1,
                "llm_clarity_check": False,
            },
        }
        mock_config.return_value = AppConfig.from_dict(cfg_dict)

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
        assert "No stories to create" in report
        assert "nothing to report" in report
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

    def test_invalid_category_raises(self):
        from agent.runner import _validate_config

        cfg = {
            "agent": {
                "enabled_categories": ["metrics", "invalid_cat"],
            },
        }
        with pytest.raises(ValueError, match="Unknown category"):
            _validate_config(cfg)

    def test_valid_config_passes(self):
        from agent.runner import _validate_config

        cfg = {
            "agent": {
                "enabled_categories": ["metrics", "alerts", "docs"],
                "temperature": 0.3,
                "story_points": {"enabled": True},
            },
            "creation": {"project": "CNV"},
        }
        _validate_config(cfg)

    def test_invalid_temperature_raises(self):
        from agent.runner import _validate_config

        cfg = {
            "agent": {
                "temperature": "not-a-number",
            },
        }
        with pytest.raises(ValueError, match="temperature"):
            _validate_config(cfg)

    def test_apply_without_version_raises(self):
        from agent.runner import ConfigError, run

        with patch("agent.runner._load_config") as m:
            m.return_value = _minimal_cfg()
            with pytest.raises(ConfigError, match="version"):
                run(
                    epic_keys=["CNV-1"],
                    apply=True,
                    version="",
                )

    def test_invalid_cli_category_raises(self):
        from agent.runner import ConfigError, run

        with patch("agent.runner._load_config") as m:
            m.return_value = _minimal_cfg()
            with pytest.raises(ConfigError, match="invalid_cat"):
                run(
                    epic_keys=["CNV-1"],
                    categories=["metrics", "invalid_cat"],
                )

    def test_llm_error_epic_appears_in_summary_table(self):
        from agent.runner import run
        from schemas.issue_doc import IssueDoc

        with (
            patch("agent.runner._load_config") as mock_config,
            patch("agent.runner.get_jira_client") as mock_jira,
            patch("agent.runner.build_all_inventories",
                  return_value={}),
            patch("agent.runner.fetch_epic_with_children")
                as mock_fetch,
            patch("agent.runner.build_analysis_result")
                as mock_analysis,
            patch("agent.runner.compose_stories") as mock_compose,
        ):
            _LONG = "Enough detail " * 10
            mock_config.return_value = _minimal_cfg()
            mock_jira.return_value = MagicMock()
            mock_jira.return_value.issue.return_value = (
                _fake_jira_issue(
                    "CNV-700", "Fail Epic", description=_LONG,
                )
            )
            mock_fetch.return_value = (
                IssueDoc(
                    key="CNV-700", summary="Fail Epic",
                    description=_LONG,
                ),
                [],
            )
            mock_analysis.return_value = {
                "epic_key": "CNV-700",
                "epic_summary": "Fail Epic",
                "epic_description": _LONG,
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
            mock_compose.side_effect = LLMError("model error")

            report = run(
                epic_keys=["CNV-700"],
                apply=False,
                use_llm=True,
            )

        assert "CNV-700" in report
        assert "llm error" in report


class TestConfigParsing:
    def test_parse_int_invalid_raises(self):
        from schemas.config import AppConfig, ConfigError

        with pytest.raises(ConfigError, match="integer"):
            AppConfig.from_dict({
                "grooming": {"comment_cooldown_days": "abc"},
            })

    def test_parse_category_list_string(self):
        from schemas.config import AppConfig

        cfg = AppConfig.from_dict({
            "agent": {"enabled_categories": "metrics,docs"},
        })
        assert cfg.agent.enabled_categories == [
            "metrics", "docs",
        ]

    def test_repos_scalar_string(self):
        from schemas.config import AppConfig

        cfg = AppConfig.from_dict({
            "discovery": {
                "repos": "https://github.com/a/b",
            },
        })
        assert cfg.discovery.repos == [
            "https://github.com/a/b",
        ]

    def test_category_guidance_non_dict_raises(self):
        from schemas.config import AppConfig, ConfigError

        with pytest.raises(ConfigError, match="mapping"):
            AppConfig.from_dict({
                "agent": {"category_guidance": "bad"},
            })

    def test_negative_min_children_raises(self):
        from schemas.config import AppConfig, ConfigError

        with pytest.raises(
            ConfigError, match="min_children",
        ):
            AppConfig.from_dict({
                "grooming": {"min_children": -1},
            })

    def test_temperature_out_of_range_raises(self):
        from schemas.config import AppConfig, ConfigError

        with pytest.raises(ConfigError, match="temperature"):
            AppConfig.from_dict({
                "agent": {"temperature": 3.0},
            })


class TestIssueDocDict:
    def test_from_jira_dict_payload(self):
        from schemas.issue_doc import IssueDoc

        raw = {
            "key": "CNV-42",
            "fields": {
                "summary": "Test summary",
                "description": "Test desc",
                "issuetype": {"name": "Story"},
                "labels": ["label-a"],
                "components": [{"name": "Comp1"}],
            },
        }
        doc = IssueDoc.from_jira(raw)
        assert doc.key == "CNV-42"
        assert doc.summary == "Test summary"
        assert doc.description == "Test desc"
        assert doc.issue_type == "Story"
        assert doc.labels == ["label-a"]
        assert doc.components == ["Comp1"]

    def test_from_jira_dict_flat(self):
        from schemas.issue_doc import IssueDoc

        raw = {
            "key": "CNV-99",
            "summary": "Flat",
            "description": "Flat desc",
        }
        doc = IssueDoc.from_jira(raw)
        assert doc.key == "CNV-99"
        assert doc.summary == "Flat"


class TestFeedbackFooter:
    """Tests for _build_feedback_footer in agent.jira.client."""

    def _footer(self, **kwargs):
        from agent.jira.client import _build_feedback_footer
        defaults = dict(
            summary="Add CDI import metrics",
            source_epic_key="CNV-86474",
            category="metrics",
            run_id="run-abc123",
            feedback_repo="https://github.com/sradco/cnv-epic-agent",
        )
        defaults.update(kwargs)
        return _build_feedback_footer(**defaults)

    def test_no_repo_returns_plain_attribution(self):
        footer = self._footer(feedback_repo="")
        assert "cnv-grooming-agent" in footer
        assert "report issue" not in footer
        assert "github.com" not in footer

    def test_with_repo_contains_link(self):
        footer = self._footer()
        assert "[report issue|" in footer
        assert "github.com/sradco/cnv-epic-agent/issues/new" in footer

    def test_url_contains_template_param(self):
        footer = self._footer()
        assert "template=agent-feedback.yml" in footer

    def test_url_contains_epic_key(self):
        footer = self._footer()
        assert "CNV-86474" in footer

    def test_url_contains_category(self):
        footer = self._footer()
        assert "metrics" in footer

    def test_url_contains_run_id(self):
        footer = self._footer()
        assert "run-abc123" in footer

    def test_title_contains_summary(self):
        footer = self._footer(summary="Add CDI import metrics")
        # URL-encoded space is +  or %20
        assert "Add" in footer
        assert "CDI" in footer

    def test_footer_uses_jira_wiki_markup_separator(self):
        # Jira wiki markup horizontal rule is ----
        footer = self._footer()
        assert "\n----\n" in footer

    def test_footer_does_not_use_markdown_link(self):
        # Markdown [text](url) must NOT appear — Jira uses [text|url]
        footer = self._footer()
        assert "](http" not in footer


class TestClassifyChildCategory:
    """Tests for _classify_child_category heuristic."""

    def _child(self, summary="", labels=None):
        from schemas.issue_doc import IssueDoc
        return IssueDoc(
            key="CNV-1",
            summary=summary,
            labels=labels or [],
        )

    def test_qe_label(self):
        from agent.runner import _classify_child_category
        assert _classify_child_category(
            self._child(labels=["qe"])
        ) == "qe"

    def test_qe_summary_prefix(self):
        from agent.runner import _classify_child_category
        assert _classify_child_category(
            self._child(summary="[QE] test coverage")
        ) == "qe"

    def test_docs_label(self):
        from agent.runner import _classify_child_category
        assert _classify_child_category(
            self._child(labels=["docs"])
        ) == "docs"

    def test_docs_summary_prefix(self):
        from agent.runner import _classify_child_category
        assert _classify_child_category(
            self._child(summary="[Docs] update runbook")
        ) == "docs"

    def test_dev_fallback(self):
        from agent.runner import _classify_child_category
        assert _classify_child_category(
            self._child(summary="Add metric for VM lifecycle")
        ) == "dev"

    def test_dev_fallback_no_labels(self):
        from agent.runner import _classify_child_category
        assert _classify_child_category(
            self._child()
        ) == "dev"


class TestEpicTallySpFields:
    """Tests for _EpicTally SP and label fields."""

    def test_default_sp_zero(self):
        from agent.runner import _EpicTally
        t = _EpicTally("CNV-1")
        assert t.dev_sp_existing == 0
        assert t.dev_sp_proposed == 0
        assert t.qe_sp_existing == 0
        assert t.qe_sp_proposed == 0
        assert t.docs_sp_existing == 0
        assert t.docs_sp_proposed == 0

    def test_default_labels_false(self):
        from agent.runner import _EpicTally
        t = _EpicTally("CNV-1")
        assert t.has_no_qe is False
        assert t.has_no_doc is False

    def test_default_versions_empty(self):
        from agent.runner import _EpicTally
        t = _EpicTally("CNV-1")
        assert t.fix_version == ""
        assert t.target_version == ""


class TestSpCell:
    """Tests for _sp_cell helper."""

    def test_no_proposed(self):
        from agent.runner import _sp_cell
        assert _sp_cell(5, 0) == "5"

    def test_with_proposed(self):
        from agent.runner import _sp_cell
        assert _sp_cell(5, 3) == "5 (+3)"

    def test_both_zero(self):
        from agent.runner import _sp_cell
        assert _sp_cell(0, 0) == "0"

    def test_only_proposed(self):
        from agent.runner import _sp_cell
        assert _sp_cell(0, 8) == "0 (+8)"


class TestBuildReportSummaryTwoTables:
    """Tests for the two-table structure of _build_report_summary."""

    def _make_counters(self, tallies):
        from agent.runner import _RunCounters
        c = _RunCounters()
        c.epic_tallies = tallies
        return c

    def test_planning_overview_header_present(self):
        from agent.runner import _EpicTally, _build_report_summary
        t = _EpicTally("CNV-100", status="groomed")
        c = self._make_counters([t])
        lines = _build_report_summary(c, 1, apply=False)
        text = "\n".join(lines)
        # Unversioned epics go into the "Unversioned Epics" table
        assert "Unversioned Epics" in text
        assert "Fix Ver" in text
        assert "Target Ver" in text
        assert "Dev SP" in text
        assert "QE SP" in text
        assert "Docs SP" in text

    def test_agent_proposed_stories_header_present(self):
        from agent.runner import _EpicTally, _build_report_summary
        t = _EpicTally("CNV-100", status="groomed")
        c = self._make_counters([t])
        lines = _build_report_summary(c, 1, apply=False)
        text = "\n".join(lines)
        assert "Agent Proposed Stories" in text

    def test_no_qe_shown_in_planning(self):
        from agent.runner import _EpicTally, _build_report_summary
        t = _EpicTally("CNV-200", status="groomed")
        t.has_no_qe = True
        c = self._make_counters([t])
        lines = _build_report_summary(c, 1, apply=False)
        text = "\n".join(lines)
        assert "no-qe" in text

    def test_no_doc_shown_in_planning(self):
        from agent.runner import _EpicTally, _build_report_summary
        t = _EpicTally("CNV-300", status="groomed")
        t.has_no_doc = True
        c = self._make_counters([t])
        lines = _build_report_summary(c, 1, apply=False)
        text = "\n".join(lines)
        assert "no-doc" in text

    def test_sp_format_with_proposed(self):
        from agent.runner import _EpicTally, _build_report_summary
        t = _EpicTally("CNV-400", status="groomed")
        t.dev_sp_existing = 10
        t.dev_sp_proposed = 5
        c = self._make_counters([t])
        lines = _build_report_summary(c, 1, apply=False)
        text = "\n".join(lines)
        assert "10 (+5)" in text

    def test_version_fields_shown(self):
        from agent.runner import _EpicTally, _build_report_summary
        t = _EpicTally("CNV-500", status="groomed")
        t.fix_version = "CNV 5.0"
        t.target_version = "CNV v5.0.0"
        c = self._make_counters([t])
        lines = _build_report_summary(c, 1, apply=False)
        text = "\n".join(lines)
        assert "CNV 5.0" in text
        assert "CNV v5.0.0" in text

    def test_no_version_shows_dash(self):
        from agent.runner import _EpicTally, _build_report_summary
        t = _EpicTally("CNV-600", status="groomed")
        c = self._make_counters([t])
        lines = _build_report_summary(c, 1, apply=False)
        text = "\n".join(lines)
        # Both fix and target version default to "-"; row has
        # "| epic | summary | status | - | - | ..." format
        assert "| - | - |" in text
        assert "Unversioned Epics" in text

    def test_epic_key_linked_in_both_tables(self):
        from agent.runner import _EpicTally, _build_report_summary
        t = _EpicTally("CNV-700", status="groomed")
        c = self._make_counters([t])
        lines = _build_report_summary(c, 1, apply=False)
        text = "\n".join(lines)
        assert "[CNV-700](#cnv-700)" in text

    def test_sorted_by_status_errors_first(self):
        from agent.runner import (
            STATUS_ERROR, STATUS_GROOMED, _EpicTally,
            _build_report_summary,
        )
        t1 = _EpicTally("CNV-800", status=STATUS_GROOMED)
        t2 = _EpicTally("CNV-801", status=STATUS_ERROR)
        c = self._make_counters([t1, t2])
        lines = _build_report_summary(c, 2, apply=False)
        text = "\n".join(lines)
        # Both are unversioned; within Unversioned Epics table
        # errors sort before groomed.
        unversioned_section = text[text.index("Unversioned Epics"):]
        pos_err = unversioned_section.index("CNV-801")
        pos_ok = unversioned_section.index("CNV-800")
        assert pos_err < pos_ok


class TestCategoryGates:
    """Tests that no-qe / no-doc labels gate story generation."""

    def _make_epic_issue(self, key="CNV-900", labels=None):
        epic_issue = MagicMock()
        epic_issue.key = key
        epic_issue.fields = MagicMock()
        epic_issue.fields.summary = "Test epic"
        epic_issue.fields.description = (
            "A long description with enough content to pass "
            "grooming checks and analysis heuristics."
        )
        epic_issue.fields.labels = labels or []
        epic_issue.fields.components = []
        epic_issue.fields.issuetype = MagicMock()
        epic_issue.fields.issuetype.name = "Epic"
        epic_issue.fields.fixVersions = []
        return epic_issue

    @patch("agent.runner._load_config")
    @patch("agent.runner.get_jira_client")
    @patch("agent.runner.build_all_inventories", return_value={})
    @patch("agent.runner.fetch_epic_with_children")
    @patch("agent.runner.build_analysis_result")
    @patch("agent.runner.compose_stories")
    @patch("agent.runner.find_broad_matching_stories", return_value=[])
    def test_no_qe_removes_qe_from_compose(
        self,
        mock_broad,
        mock_compose,
        mock_analysis,
        mock_fetch,
        mock_inv,
        mock_jira,
        mock_config,
    ):
        from agent.runner import run
        from schemas.issue_doc import IssueDoc
        from schemas.stories import StoryPayload

        mock_config.return_value = _minimal_cfg()
        mock_jira.return_value = MagicMock()

        epic_issue = self._make_epic_issue(labels=["no-qe"])
        mock_jira.return_value.search_issues.return_value = [epic_issue]

        _LONG_DESC = "A" * 100
        epic_doc = IssueDoc(
            key="CNV-900", summary="Test epic",
            description=_LONG_DESC,
            labels=["no-qe"], components=[],
        )
        mock_fetch.return_value = (epic_doc, [])
        mock_analysis.return_value = {
            "gaps": ["metrics"],
            "epic_labels": ["no-qe"],
            "epic_components": [],
            "domain_keywords": [],
        }
        mock_compose.return_value = [
            StoryPayload(
                category="metrics",
                summary="Add VM metric",
                description="desc",
            )
        ]

        result = run(
            epic_keys=["CNV-900"],
            apply=False,
            use_llm=True,
            categories=["metrics", "alerts", "qe"],
        )

        # compose_stories must have been called with categories
        # that do NOT include "qe"
        call_kwargs = mock_compose.call_args[1]
        cats_used = call_kwargs.get(
            "categories", mock_compose.call_args[0][1]
            if len(mock_compose.call_args[0]) > 1 else []
        )
        assert "qe" not in cats_used

    @patch("agent.runner._load_config")
    @patch("agent.runner.get_jira_client")
    @patch("agent.runner.build_all_inventories", return_value={})
    @patch("agent.runner.fetch_epic_with_children")
    @patch("agent.runner.build_analysis_result")
    @patch("agent.runner.compose_stories")
    @patch("agent.runner.find_broad_matching_stories", return_value=[])
    def test_no_doc_removes_docs_from_compose(
        self,
        mock_broad,
        mock_compose,
        mock_analysis,
        mock_fetch,
        mock_inv,
        mock_jira,
        mock_config,
    ):
        from agent.runner import run
        from schemas.issue_doc import IssueDoc
        from schemas.stories import StoryPayload

        mock_config.return_value = _minimal_cfg()
        mock_jira.return_value = MagicMock()

        epic_issue = self._make_epic_issue(labels=["no-doc"])
        mock_jira.return_value.search_issues.return_value = [epic_issue]

        _LONG_DESC = "A" * 100
        epic_doc = IssueDoc(
            key="CNV-900", summary="Test epic",
            description=_LONG_DESC,
            labels=["no-doc"], components=[],
        )
        mock_fetch.return_value = (epic_doc, [])
        mock_analysis.return_value = {
            "gaps": ["metrics"],
            "epic_labels": ["no-doc"],
            "epic_components": [],
            "domain_keywords": [],
        }
        mock_compose.return_value = [
            StoryPayload(
                category="metrics",
                summary="Add VM metric",
                description="desc",
            )
        ]

        result = run(
            epic_keys=["CNV-900"],
            apply=False,
            use_llm=True,
            categories=["metrics", "docs"],
        )

        call_kwargs = mock_compose.call_args[1]
        cats_used = call_kwargs.get("categories", [])
        assert "docs" not in cats_used

    @patch("agent.runner._load_config")
    @patch("agent.runner.get_jira_client")
    @patch("agent.runner.build_all_inventories", return_value={})
    @patch("agent.runner.fetch_epic_with_children")
    @patch("agent.runner.build_analysis_result")
    @patch("agent.runner.compose_stories")
    @patch("agent.runner.find_broad_matching_stories", return_value=[])
    def test_no_docs_alias_removes_docs(
        self,
        mock_broad,
        mock_compose,
        mock_analysis,
        mock_fetch,
        mock_inv,
        mock_jira,
        mock_config,
    ):
        from agent.runner import run
        from schemas.issue_doc import IssueDoc
        from schemas.stories import StoryPayload

        mock_config.return_value = _minimal_cfg()
        mock_jira.return_value = MagicMock()

        epic_issue = self._make_epic_issue(labels=["no-docs"])
        mock_jira.return_value.search_issues.return_value = [epic_issue]

        _LONG_DESC = "A" * 100
        epic_doc = IssueDoc(
            key="CNV-900", summary="Test epic",
            description=_LONG_DESC,
            labels=["no-docs"], components=[],
        )
        mock_fetch.return_value = (epic_doc, [])
        mock_analysis.return_value = {
            "gaps": ["metrics"],
            "epic_labels": ["no-docs"],
            "epic_components": [],
            "domain_keywords": [],
        }
        mock_compose.return_value = [
            StoryPayload(
                category="metrics",
                summary="Add VM metric",
                description="desc",
            )
        ]

        result = run(
            epic_keys=["CNV-900"],
            apply=False,
            use_llm=True,
            categories=["metrics", "docs"],
        )

        call_kwargs = mock_compose.call_args[1]
        cats_used = call_kwargs.get("categories", [])
        assert "docs" not in cats_used


class TestFeedbackCount:
    """Tests for _fetch_open_feedback_count."""

    def test_empty_repo_returns_none(self):
        from agent.runner import _fetch_open_feedback_count
        assert _fetch_open_feedback_count("") is None

    def test_count_from_response(self):
        from unittest.mock import MagicMock, patch
        from agent.runner import _fetch_open_feedback_count

        mock_resp = MagicMock()
        mock_resp.read.return_value = b'[{}, {}, {}]'
        mock_resp.headers.get.return_value = ""
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            count = _fetch_open_feedback_count(
                "https://github.com/sradco/cnv-epic-agent"
            )
        assert count == 3

    def test_network_error_returns_none(self):
        from agent.runner import _fetch_open_feedback_count
        import urllib.error

        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError("timeout"),
        ):
            result = _fetch_open_feedback_count(
                "https://github.com/sradco/cnv-epic-agent"
            )
        assert result is None

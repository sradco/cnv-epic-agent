"""Tests for prompt templates."""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from prompts.templates import (
    CLARITY_CHECK_JSON_SCHEMA,
    CLARITY_CHECK_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
    SP_ESTIMATION_SYSTEM_PROMPT,
    build_clarity_check_prompt,
    build_story_composition_prompt,
    build_sp_estimation_prompt,
    get_system_prompt,
    strip_jira_markup,
    _trim_existing_items,
)
from schemas.stories import SP_ESTIMATION_JSON_SCHEMA


class TestSystemPrompt:
    """Tests for the base SYSTEM_PROMPT (category-agnostic)."""

    def test_mentions_kubevirt(self):
        assert "kubevirt" in SYSTEM_PROMPT.lower()

    def test_mentions_json(self):
        assert "json" in SYSTEM_PROMPT.lower()

    def test_mentions_acceptance_criteria(self):
        assert "acceptance criteria" in SYSTEM_PROMPT.lower()

    def test_mentions_story_points(self):
        assert "story points" in SYSTEM_PROMPT.lower()

    def test_mentions_fibonacci(self):
        assert "fibonacci" in SYSTEM_PROMPT.lower()

    def test_allows_empty_stories_for_refactoring_epics(self):
        lowered = SYSTEM_PROMPT.lower()
        assert "internal refactoring" in lowered
        assert "do not invent stories just to fill a gap" in lowered

    def test_no_doc_no_qe_label_handling(self):
        lowered = SYSTEM_PROMPT.lower()
        assert "no-doc" in lowered
        assert "no-qe" in lowered

    def test_no_dashboards_for_backlog_epics(self):
        lowered = SYSTEM_PROMPT.lower()
        assert "backlog" in lowered
        assert "organizational containers" in lowered
        assert "child stories" in lowered


class TestGetSystemPromptWithObservability:
    """Tests for observability rules injected via get_system_prompt."""

    _ALL_OBS = ["metrics", "alerts", "dashboards"]

    def test_sre_use_cases_mentioned(self):
        lowered = get_system_prompt(self._ALL_OBS).lower()
        assert "troubleshooting" in lowered
        assert "capacity planning" in lowered
        assert "health assessment" in lowered

    def test_alerts_require_metric_backing(self):
        lowered = get_system_prompt(self._ALL_OBS).lower()
        assert "backed by" in lowered
        assert "metric" in lowered

    def test_dashboards_require_metric_references(self):
        lowered = get_system_prompt(self._ALL_OBS).lower()
        assert "dashboard" in lowered
        assert "metric" in lowered

    def test_dashboards_prefer_existing_over_new(self):
        lowered = get_system_prompt(self._ALL_OBS).lower()
        assert "existing dashboards" in lowered

    def test_dashboards_reject_internal_component_dashboards(self):
        lowered = get_system_prompt(self._ALL_OBS).lower()
        assert "internal component" in lowered
        assert "operator workflow" in lowered

    def test_rejects_presence_check_alerts(self):
        assert "presence" in get_system_prompt(self._ALL_OBS).lower()

    def test_requires_who_benefits_section(self):
        assert "who benefits" in get_system_prompt(self._ALL_OBS).lower()

    def test_requires_why_this_matters_section(self):
        assert "why this matters" in get_system_prompt(self._ALL_OBS).lower()

    def test_requires_how_it_is_used_section(self):
        assert "how it is used" in get_system_prompt(self._ALL_OBS).lower()

    def test_alerts_require_actionable_response(self):
        lowered = get_system_prompt(self._ALL_OBS).lower()
        assert "actionable response" in lowered
        assert "dashboard insight" in lowered

    def test_obs_rules_not_injected_without_obs_categories(self):
        lowered = get_system_prompt(["docs", "qe"]).lower()
        assert "presence" not in lowered
        assert "backed by" not in lowered


class TestGetSystemPromptWithDocs:
    """Tests for docs rules injected via get_system_prompt."""

    def test_docs_only_for_user_facing_changes(self):
        lowered = get_system_prompt(["docs"]).lower()
        assert "user-facing" in lowered
        assert "internal refactoring" in lowered

    def test_docs_rules_not_injected_without_docs(self):
        lowered = get_system_prompt(["metrics"]).lower()
        assert "user-facing feature" not in lowered


class TestGetSystemPromptWithQE:
    """Tests for QE rules injected via get_system_prompt."""

    def test_qe_split_by_test_type(self):
        lowered = get_system_prompt(["qe"]).lower()
        assert "split" in lowered
        assert "monolithic" in lowered

    def test_qe_test_categories_mentioned(self):
        lowered = get_system_prompt(["qe"]).lower()
        assert "metric unit tests" in lowered
        assert "alert rule tests" in lowered
        assert "dashboard verification" in lowered
        assert "end-to-end pipeline" in lowered
        assert "upgrade/rollback" in lowered

    def test_qe_distinguishes_new_vs_migrated(self):
        lowered = get_system_prompt(["qe"]).lower()
        assert "genuinely new" in lowered
        assert "migrated" in lowered
        assert "renamed" in lowered

    def test_qe_rules_not_injected_without_qe(self):
        lowered = get_system_prompt(["metrics"]).lower()
        assert "monolithic" not in lowered


class TestBuildPrompt:
    def _make_analysis(self, **overrides):
        base = {
            "epic_key": "CNV-99999",
            "epic_summary": "Test feature epic",
            "epic_description": "Detailed description of the feature",
            "child_issues": [],
            "domain_keywords": ["migration"],
            "gaps": ["metrics"],
            "feature_types": ["data_path"],
            "proposals": {
                "metrics": {
                    "existing": [],
                    "proposed": [
                        {
                            "name_hint": "kubevirt_vmi_test_total",
                            "rationale": "Track test operations",
                            "user_action": "Alert on errors",
                        },
                    ],
                },
            },
            "dashboard_targets": ["VM Overview (kubevirt, perses-go)"],
            "telemetry_suggestions": [
                {
                    "name": "cnv:test_total:sum",
                    "expr": "sum(kubevirt_vmi_test_total)",
                    "repo": "kubevirt",
                    "file": "rules.yaml",
                    "rationale": "Cluster-level sum",
                },
            ],
            "need_state": "needed",
            "need_confidence": "high",
            "recommended_action": "create now",

            "would_create_count": 1,
            "epic_components": [],
        }
        base.update(overrides)
        return base

    def test_includes_epic_components(self):
        analysis = self._make_analysis(
            epic_components=["CNV Virtualization"],
        )
        prompt = build_story_composition_prompt(analysis)
        assert "CNV Virtualization" in prompt
        assert "Epic components:" in prompt

    def test_no_components_section_when_empty(self):
        analysis = self._make_analysis(epic_components=[])
        prompt = build_story_composition_prompt(analysis)
        assert "Epic components:" not in prompt

    def test_includes_epic_labels(self):
        analysis = self._make_analysis(
            epic_labels=["no-doc", "cnv-4.23"],
        )
        prompt = build_story_composition_prompt(analysis)
        assert "no-doc" in prompt
        assert "cnv-4.23" in prompt
        assert "Epic labels:" in prompt

    def test_no_labels_section_when_empty(self):
        analysis = self._make_analysis(epic_labels=[])
        prompt = build_story_composition_prompt(analysis)
        assert "Epic labels:" not in prompt

    def test_includes_epic_description(self):
        analysis = self._make_analysis()
        prompt = build_story_composition_prompt(analysis)
        assert "Detailed description of the feature" in prompt
        assert "---BEGIN EPIC DESCRIPTION---" in prompt
        assert "---END EPIC DESCRIPTION---" in prompt

    def test_includes_gaps(self):
        analysis = self._make_analysis(gaps=["metrics", "alerts"])
        prompt = build_story_composition_prompt(analysis)
        assert "metrics" in prompt
        assert "alerts" in prompt

    def test_includes_proposals_as_json(self):
        analysis = self._make_analysis()
        prompt = build_story_composition_prompt(analysis)
        assert "kubevirt_vmi_test_total" in prompt
        assert "```json" in prompt

    def test_includes_dashboard_targets(self):
        analysis = self._make_analysis()
        prompt = build_story_composition_prompt(analysis)
        assert "VM Overview" in prompt

    def test_includes_telemetry(self):
        analysis = self._make_analysis()
        prompt = build_story_composition_prompt(analysis)
        assert "cnv:test_total:sum" in prompt

    def test_includes_child_issues(self):
        analysis = self._make_analysis(
            child_issues=[
                {
                    "key": "CNV-10001",
                    "summary": "Implement migration API",
                    "description": "REST endpoint for migration",
                },
            ],
        )
        prompt = build_story_composition_prompt(analysis)
        assert "CNV-10001" in prompt
        assert "Implement migration API" in prompt
        assert "---BEGIN CHILD ISSUES---" in prompt
        assert "---END CHILD ISSUES---" in prompt

    def test_no_gaps_produces_minimal_prompt(self):
        analysis = self._make_analysis(gaps=[], proposals={})
        prompt = build_story_composition_prompt(analysis)
        assert "CNV-99999" in prompt
        assert "Observability gaps" not in prompt

    def test_includes_enabled_categories(self):
        analysis = self._make_analysis()
        prompt = build_story_composition_prompt(
            analysis,
            categories=["metrics", "docs", "qe"],
        )
        assert "Enabled categories: metrics, docs, qe" in prompt

    def test_includes_category_guidance(self):
        analysis = self._make_analysis()
        guidance = {
            "docs": {
                "trigger": "Epic changes user-facing behavior",
                "story_prefix": "[Docs]",
                "acceptance_criteria": [
                    "Documentation PR submitted",
                    "Release notes entry added",
                ],
            },
        }
        prompt = build_story_composition_prompt(
            analysis,
            category_guidance=guidance,
        )
        assert "Category guidance" in prompt
        assert "docs" in prompt
        assert "Epic changes user-facing behavior" in prompt
        assert "[Docs]" in prompt

    def test_includes_story_points_guidance(self):
        analysis = self._make_analysis()
        prompt = build_story_composition_prompt(
            analysis,
            story_points_guidance="1=trivial, 2=small, 3=medium",
        )
        assert "Story points" in prompt
        assert "1=trivial" in prompt

    def test_no_guidance_omits_sections(self):
        analysis = self._make_analysis()
        prompt = build_story_composition_prompt(analysis)
        assert "Category guidance" not in prompt
        assert "Story points:" not in prompt
        assert "Enabled categories" not in prompt


class TestSPEstimationPromptTemplates:
    def test_sp_system_prompt_no_bugs(self):
        assert "bug" in SP_ESTIMATION_SYSTEM_PROMPT.lower()
        assert "not" in SP_ESTIMATION_SYSTEM_PROMPT.lower()

    def test_sp_system_prompt_fibonacci(self):
        assert "1, 2, 3, 5, 8, 13" in SP_ESTIMATION_SYSTEM_PROMPT

    def test_sp_prompt_multiple_stories(self):
        stories = [
            {"key": "CNV-10", "summary": "Story A", "description": "Desc A"},
            {"key": "CNV-11", "summary": "Story B", "description": "Desc B"},
        ]
        prompt = build_sp_estimation_prompt(
            epic_summary="Big feature",
            epic_description="Feature description",
            stories=stories,
        )
        assert "CNV-10" in prompt
        assert "CNV-11" in prompt
        assert "Story A" in prompt
        assert "Story B" in prompt
        assert "Big feature" in prompt
        assert "2" in prompt  # story count

    def test_sp_prompt_includes_schema(self):
        prompt = build_sp_estimation_prompt(
            epic_summary="Test",
            epic_description="",
            stories=[{"key": "X-1", "summary": "S", "description": ""}],
            include_schema=True,
        )
        assert "estimates" in prompt
        assert "issue_key" in prompt
        assert "story_points" in prompt

        prompt_no_schema = build_sp_estimation_prompt(
            epic_summary="Test",
            epic_description="",
            stories=[{"key": "X-1", "summary": "S", "description": ""}],
        )
        assert "Return JSON" in prompt_no_schema
        assert '"estimates"' not in prompt_no_schema

    def test_sp_schema_structure(self):
        assert "estimates" in SP_ESTIMATION_JSON_SCHEMA["properties"]
        items = SP_ESTIMATION_JSON_SCHEMA["properties"]["estimates"]["items"]
        assert "issue_key" in items["properties"]
        assert "rationale" in items["properties"]


class TestStripJiraMarkup:
    def test_strips_headings(self):
        assert strip_jira_markup("h1. Title\nBody") == "Title\nBody"
        assert strip_jira_markup("h3. Section") == "Section"

    def test_strips_links(self):
        assert strip_jira_markup("[Example|https://example.com]") == "Example"

    def test_strips_bold(self):
        assert strip_jira_markup("*bold text*") == "bold text"

    def test_strips_panel_macros(self):
        text = "{panel:title=Info}content{panel}"
        assert "{panel" not in strip_jira_markup(text)
        assert "content" in strip_jira_markup(text)

    def test_plain_text_unchanged(self):
        assert strip_jira_markup("plain text") == "plain text"


class TestTrimExistingItems:
    def test_caps_existing_items(self):
        existing = [{"name": f"metric_{i}"} for i in range(10)]
        proposals = {
            "metrics": {
                "existing": existing,
                "proposed": [{"name": "new_metric"}],
            },
        }
        trimmed = _trim_existing_items(proposals, max_per_category=3)
        result = trimmed["metrics"]
        assert len(result["existing"]) == 4  # 3 items + _note
        assert result["existing"][-1]["_note"]
        assert len(result["proposed"]) == 1

    def test_preserves_small_existing(self):
        existing = [{"name": "m1"}, {"name": "m2"}]
        proposals = {"metrics": {"existing": existing, "proposed": []}}
        trimmed = _trim_existing_items(proposals, max_per_category=5)
        assert len(trimmed["metrics"]["existing"]) == 2

    def test_jira_markup_stripped_from_epic_description(self):
        analysis = {
            "epic_key": "CNV-1",
            "epic_summary": "Test",
            "epic_description": "h2. Overview\n*Important* [link|http://x]",
            "child_issues": [],
            "domain_keywords": [],
            "gaps": ["metrics"],
            "proposals": {},
            "dashboard_targets": [],
            "telemetry_suggestions": [],
        }
        prompt = build_story_composition_prompt(analysis)
        assert "h2." not in prompt
        assert "*Important*" not in prompt
        assert "Important" in prompt


class TestClarityCheckPrompt:
    def test_system_prompt_mentions_clear_and_needs_grooming(self):
        assert "clear" in CLARITY_CHECK_SYSTEM_PROMPT.lower()
        assert "needs grooming" in CLARITY_CHECK_SYSTEM_PROMPT.lower()

    def test_system_prompt_mentions_scope(self):
        assert "scope" in CLARITY_CHECK_SYSTEM_PROMPT.lower()

    def test_backlog_umbrella_epics_accepted(self):
        lowered = CLARITY_CHECK_SYSTEM_PROMPT.lower()
        assert "backlog" in lowered
        assert "umbrella" in lowered
        assert "child stories define the scope" in lowered

    def test_schema_has_verdict_and_reason(self):
        props = CLARITY_CHECK_JSON_SCHEMA["properties"]
        assert "verdict" in props
        assert "reason" in props
        assert props["verdict"]["enum"] == [
            "clear", "needs_grooming",
        ]

    def test_prompt_includes_epic_info(self):
        prompt = build_clarity_check_prompt(
            epic_key="CNV-123",
            epic_summary="Add GPU metrics",
            epic_description="Expose GPU utilization metrics.",
            children=[],
        )
        assert "CNV-123" in prompt
        assert "Add GPU metrics" in prompt
        assert "GPU utilization" in prompt

    def test_prompt_shows_no_description(self):
        prompt = build_clarity_check_prompt(
            epic_key="CNV-124",
            epic_summary="Vague epic",
            epic_description="",
            children=[],
        )
        assert "*(no description)*" in prompt

    def test_prompt_shows_no_children(self):
        prompt = build_clarity_check_prompt(
            epic_key="CNV-125",
            epic_summary="Test",
            epic_description="Some desc",
            children=[],
        )
        assert "*(none)*" in prompt

    def test_prompt_includes_children(self):
        children = [
            {
                "key": "CNV-126",
                "summary": "Add vCPU metric",
                "description": "Expose vCPU count",
            },
        ]
        prompt = build_clarity_check_prompt(
            epic_key="CNV-125",
            epic_summary="Test",
            epic_description="Desc",
            children=children,
        )
        assert "CNV-126" in prompt
        assert "Add vCPU metric" in prompt
        assert "Child issues (1)" in prompt

    def test_prompt_truncates_many_children(self):
        children = [
            {"key": f"CNV-{i}", "summary": f"Story {i}",
             "description": ""}
            for i in range(25)
        ]
        prompt = build_clarity_check_prompt(
            epic_key="CNV-100",
            epic_summary="Big epic",
            epic_description="Lots of work",
            children=children,
        )
        assert "5 more" in prompt


if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-v"]))

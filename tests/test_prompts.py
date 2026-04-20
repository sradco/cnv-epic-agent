"""Tests for prompt templates."""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from prompts.templates import (
    SYSTEM_PROMPT,
    SP_ESTIMATION_SYSTEM_PROMPT,
    build_story_composition_prompt,
    build_sp_estimation_prompt,
    strip_jira_markup,
    _trim_existing_items,
)
from schemas.stories import SP_ESTIMATION_JSON_SCHEMA


class TestSystemPrompt:
    def test_mentions_kubevirt(self):
        assert "kubevirt" in SYSTEM_PROMPT.lower()

    def test_mentions_json(self):
        assert "json" in SYSTEM_PROMPT.lower()

    def test_mentions_acceptance_criteria(self):
        assert "acceptance criteria" in SYSTEM_PROMPT.lower()

    def test_mentions_docs_category(self):
        assert "docs" in SYSTEM_PROMPT.lower()

    def test_mentions_qe_category(self):
        assert "qe" in SYSTEM_PROMPT.lower()

    def test_mentions_story_points(self):
        assert "story points" in SYSTEM_PROMPT.lower()

    def test_mentions_fibonacci(self):
        assert "fibonacci" in SYSTEM_PROMPT.lower()

    def test_alerts_require_metric_backing(self):
        assert "backed by" in SYSTEM_PROMPT.lower()
        assert "metric" in SYSTEM_PROMPT.lower()

    def test_dashboards_require_metric_references(self):
        assert "dashboard" in SYSTEM_PROMPT.lower()
        assert "metric" in SYSTEM_PROMPT.lower()

    def test_sre_use_cases_mentioned(self):
        lowered = SYSTEM_PROMPT.lower()
        assert "troubleshooting" in lowered
        assert "capacity planning" in lowered
        assert "health assessment" in lowered

    def test_rejects_presence_check_alerts(self):
        assert "presence check" in SYSTEM_PROMPT.lower()

    def test_requires_who_benefits_section(self):
        assert "who benefits" in SYSTEM_PROMPT.lower()

    def test_requires_why_this_matters_section(self):
        assert "why this matters" in SYSTEM_PROMPT.lower()

    def test_requires_how_it_is_used_section(self):
        assert "how it is used" in SYSTEM_PROMPT.lower()

    def test_qe_split_by_test_type(self):
        lowered = SYSTEM_PROMPT.lower()
        assert "split qe work" in lowered
        assert "monolithic" in lowered

    def test_qe_test_categories_mentioned(self):
        lowered = SYSTEM_PROMPT.lower()
        assert "metric unit tests" in lowered
        assert "alert rule tests" in lowered
        assert "dashboard verification" in lowered
        assert "end-to-end pipeline" in lowered
        assert "upgrade/rollback" in lowered


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
            "apply_allowed": True,
            "would_create_count": 1,
        }
        base.update(overrides)
        return base

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


if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-v"]))

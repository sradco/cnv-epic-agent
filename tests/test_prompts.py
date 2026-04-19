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
)
from schemas.stories import AnalysisResult, SP_ESTIMATION_JSON_SCHEMA


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
        assert "Documentation PR submitted" in prompt

    def test_includes_story_points_guidance(self):
        analysis = self._make_analysis()
        prompt = build_story_composition_prompt(
            analysis,
            story_points_guidance="1=trivial, 2=small, 3=medium",
        )
        assert "Story point estimation" in prompt
        assert "1=trivial" in prompt

    def test_no_guidance_omits_sections(self):
        analysis = self._make_analysis()
        prompt = build_story_composition_prompt(analysis)
        assert "Category guidance" not in prompt
        assert "Story point estimation" not in prompt
        assert "Enabled categories" not in prompt


class TestAnalysisResultFromDict:
    def test_round_trip(self):
        data = {
            "epic_key": "CNV-1",
            "epic_summary": "Test",
            "epic_description": "Desc",
            "child_issues": [],
            "domain_keywords": ["test"],
            "need_state": "needed",
            "need_confidence": "high",
            "need_score": 5,
            "need_evidence": {},
            "coverage": {},
            "gaps": ["metrics"],
            "feature_types": ["api_controller"],
            "proposals": {},
            "dashboard_targets": [],
            "telemetry_suggestions": [],
            "recommended_action": "create now",
            "apply_allowed": True,
            "would_create_count": 1,
        }

        result = AnalysisResult.from_dict(data)
        assert result.epic_key == "CNV-1"
        assert result.gaps == ["metrics"]
        assert result.apply_allowed is True


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
        )
        assert "estimates" in prompt
        assert "issue_key" in prompt
        assert "story_points" in prompt

    def test_sp_schema_structure(self):
        assert "estimates" in SP_ESTIMATION_JSON_SCHEMA["properties"]
        items = SP_ESTIMATION_JSON_SCHEMA["properties"]["estimates"]["items"]
        assert "issue_key" in items["properties"]
        assert "rationale" in items["properties"]


if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-v"]))

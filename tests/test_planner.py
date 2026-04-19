"""Tests for the agent planner: prompt building and response parsing."""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agent.planner.llm import parse_json_response
from mcpserver.jira.client import _should_set_story_points
from prompts.templates import (
    build_story_composition_prompt,
    build_sp_estimation_prompt,
    SYSTEM_PROMPT,
    SP_ESTIMATION_SYSTEM_PROMPT,
)
from schemas.stories import (
    StoryPayload,
    STORY_JSON_SCHEMA,
    SP_ESTIMATION_JSON_SCHEMA,
)


class TestPromptBuilding:
    def test_prompt_includes_epic_key(self):
        analysis = {
            "epic_key": "CNV-12345",
            "epic_summary": "Add VM snapshot support",
            "epic_description": "Full snapshot lifecycle for VMs",
            "child_issues": [
                {
                    "key": "CNV-12346",
                    "summary": "Implement snapshot API",
                    "description": "REST endpoint for snapshots",
                },
            ],
            "domain_keywords": ["snapshot", "backup"],
            "gaps": ["metrics", "alerts"],
            "proposals": {
                "metrics": {
                    "existing": [],
                    "proposed": [
                        {
                            "name_hint": "kubevirt_vmi_snapshot_duration_seconds",
                            "type": "histogram",
                            "rationale": "Track snapshot duration",
                            "user_action": "Alert on high p99",
                        },
                    ],
                },
            },
            "dashboard_targets": [],
            "telemetry_suggestions": [],
        }
        prompt = build_story_composition_prompt(analysis)

        assert "CNV-12345" in prompt
        assert "VM snapshot support" in prompt
        assert "Full snapshot lifecycle" in prompt
        assert "CNV-12346" in prompt
        assert "snapshot" in prompt
        assert "metrics" in prompt
        assert "alerts" in prompt

    def test_prompt_includes_json_schema(self):
        analysis = {
            "epic_key": "CNV-1",
            "epic_summary": "Test",
            "epic_description": "",
            "child_issues": [],
            "domain_keywords": [],
            "gaps": ["metrics"],
            "proposals": {},
            "dashboard_targets": [],
            "telemetry_suggestions": [],
        }
        prompt = build_story_composition_prompt(analysis)
        assert "json" in prompt.lower()
        assert "stories" in prompt

    def test_system_prompt_nonempty(self):
        assert len(SYSTEM_PROMPT) > 100
        assert "SRE" in SYSTEM_PROMPT or "observability" in SYSTEM_PROMPT.lower()


class TestResponseParsing:
    def test_parse_clean_json(self):
        raw = json.dumps({
            "stories": [
                {
                    "category": "metrics",
                    "summary": "[Observability][metrics] Add metrics",
                    "description": "## Why\nBecause.",
                },
            ],
        })
        parsed = parse_json_response(raw)
        assert "stories" in parsed
        assert len(parsed["stories"]) == 1
        assert parsed["stories"][0]["category"] == "metrics"

    def test_parse_fenced_json(self):
        raw = "```json\n" + json.dumps({
            "stories": [
                {
                    "category": "alerts",
                    "summary": "Add alerts",
                    "description": "Desc",
                },
            ],
        }) + "\n```"
        parsed = parse_json_response(raw)
        assert len(parsed["stories"]) == 1

    def test_parse_json_with_preamble(self):
        raw = (
            "Here are the stories:\n\n"
            + json.dumps({"stories": []})
            + "\n\nDone."
        )
        parsed = parse_json_response(raw)
        assert "stories" in parsed

    def test_invalid_json_raises(self):
        import pytest

        with pytest.raises((json.JSONDecodeError, ValueError)):
            parse_json_response("not json at all")


class TestStoryPayload:
    def test_dataclass_fields(self):
        story = StoryPayload(
            category="metrics",
            summary="Add metrics",
            description="Description",
        )
        assert story.category == "metrics"
        assert story.summary == "Add metrics"
        assert story.story_points is None

    def test_story_points_field(self):
        story = StoryPayload(
            category="docs",
            summary="Update docs",
            description="Desc",
            story_points=3,
        )
        assert story.story_points == 3
        assert story.category == "docs"


class TestJSONSchema:
    def test_schema_has_required_fields(self):
        assert "properties" in STORY_JSON_SCHEMA
        assert "stories" in STORY_JSON_SCHEMA["properties"]
        items = STORY_JSON_SCHEMA["properties"]["stories"]["items"]
        assert "category" in items["properties"]
        assert "summary" in items["properties"]
        assert "description" in items["properties"]
        assert "story_points" in items["properties"]

    def test_schema_category_includes_docs_qe(self):
        items = STORY_JSON_SCHEMA["properties"]["stories"]["items"]
        cat_enum = items["properties"]["category"]["enum"]
        assert "docs" in cat_enum
        assert "qe" in cat_enum
        assert "metrics" in cat_enum

    def test_schema_story_points_is_integer(self):
        items = STORY_JSON_SCHEMA["properties"]["stories"]["items"]
        sp = items["properties"]["story_points"]
        assert sp["type"] == "integer"


class TestSPEstimationSchema:
    def test_schema_has_estimates(self):
        assert "properties" in SP_ESTIMATION_JSON_SCHEMA
        assert "estimates" in SP_ESTIMATION_JSON_SCHEMA["properties"]

    def test_estimate_items_have_required_fields(self):
        items = SP_ESTIMATION_JSON_SCHEMA["properties"]["estimates"]["items"]
        assert "issue_key" in items["properties"]
        assert "story_points" in items["properties"]
        assert "rationale" in items["properties"]
        assert items["required"] == [
            "issue_key", "story_points", "rationale",
        ]


class TestSPEstimationPrompt:
    def test_prompt_includes_epic_context(self):
        prompt = build_sp_estimation_prompt(
            epic_summary="Add VM snapshots",
            epic_description="Full snapshot lifecycle",
            stories=[
                {
                    "key": "CNV-100",
                    "summary": "Implement snapshot API",
                    "description": "REST endpoint",
                },
            ],
        )
        assert "Add VM snapshots" in prompt
        assert "Full snapshot lifecycle" in prompt
        assert "CNV-100" in prompt
        assert "Implement snapshot API" in prompt

    def test_prompt_includes_guidance(self):
        prompt = build_sp_estimation_prompt(
            epic_summary="Test",
            epic_description="",
            stories=[{"key": "CNV-1", "summary": "S", "description": ""}],
            story_points_guidance="1=trivial, 13=epic-sized",
        )
        assert "Sizing guidance" in prompt
        assert "1=trivial" in prompt

    def test_prompt_omits_guidance_when_empty(self):
        prompt = build_sp_estimation_prompt(
            epic_summary="Test",
            epic_description="",
            stories=[{"key": "CNV-1", "summary": "S", "description": ""}],
        )
        assert "Sizing guidance" not in prompt

    def test_system_prompt_mentions_fibonacci(self):
        assert "fibonacci" in SP_ESTIMATION_SYSTEM_PROMPT.lower()

    def test_system_prompt_excludes_bugs(self):
        assert "bug" in SP_ESTIMATION_SYSTEM_PROMPT.lower()

    def test_parse_sp_estimation_response(self):
        raw = json.dumps({
            "estimates": [
                {
                    "issue_key": "CNV-100",
                    "story_points": 3,
                    "rationale": "Moderate complexity",
                },
                {
                    "issue_key": "CNV-101",
                    "story_points": 5,
                    "rationale": "Cross-component work",
                },
            ],
        })
        parsed = parse_json_response(raw)
        assert len(parsed["estimates"]) == 2
        assert parsed["estimates"][0]["issue_key"] == "CNV-100"
        assert parsed["estimates"][0]["story_points"] == 3


class TestShouldSetStoryPoints:
    def test_none_current_allows_update(self):
        assert _should_set_story_points(None, 3) is True

    def test_zero_current_allows_update(self):
        assert _should_set_story_points(0, 5) is True

    def test_jira_default_allows_update(self):
        assert _should_set_story_points(0.42, 3) is True

    def test_existing_value_blocks_update(self):
        assert _should_set_story_points(5, 3) is False

    def test_existing_value_1_blocks_update(self):
        assert _should_set_story_points(1, 8) is False

    def test_none_new_value_blocks_update(self):
        assert _should_set_story_points(None, None) is False

    def test_existing_13_blocks_update(self):
        assert _should_set_story_points(13, 5) is False


if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-v"]))

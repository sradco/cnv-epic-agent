"""Tests for agent.planner: prompt building, response parsing, LLM helpers."""

import json
import unittest.mock as mock

import pytest

from agent.planner.llm import LLMError, parse_json_response
from prompts.templates import (
    SYSTEM_PROMPT,
    SP_ESTIMATION_SYSTEM_PROMPT,
    build_sp_estimation_prompt,
    build_story_composition_prompt,
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
        prompt = build_story_composition_prompt(
            analysis, include_schema=True,
        )
        assert "json" in prompt.lower()
        assert "stories" in prompt

        prompt_no_schema = build_story_composition_prompt(analysis)
        assert "Return JSON" in prompt_no_schema
        assert '"stories"' not in prompt_no_schema

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
        with pytest.raises((json.JSONDecodeError, ValueError)):
            parse_json_response("not json at all")


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
            stories=[
                {"key": "CNV-1", "summary": "S", "description": ""},
            ],
            story_points_guidance="1=trivial, 13=epic-sized",
        )
        assert "Sizing" in prompt
        assert "1=trivial" in prompt

    def test_prompt_omits_guidance_when_empty(self):
        prompt = build_sp_estimation_prompt(
            epic_summary="Test",
            epic_description="",
            stories=[
                {"key": "CNV-1", "summary": "S", "description": ""},
            ],
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


class TestFibonacciValidation:
    def test_clamp_fibonacci_helper(self):
        from agent.planner.planner import _clamp_fibonacci

        assert _clamp_fibonacci(1) == 1
        assert _clamp_fibonacci(2) == 2
        assert _clamp_fibonacci(5) == 5
        assert _clamp_fibonacci(13) == 13
        assert _clamp_fibonacci(4) in {3, 5}
        assert _clamp_fibonacci(7) == 8
        assert _clamp_fibonacci(10) in {8, 13}
        assert _clamp_fibonacci(0) == 1
        assert _clamp_fibonacci(100) == 13

    def test_non_fibonacci_clamped_in_estimation(self):
        from agent.planner.planner import estimate_story_points

        raw_response = json.dumps({
            "estimates": [
                {
                    "issue_key": "CNV-1",
                    "story_points": 7,
                    "rationale": "Medium work",
                },
                {
                    "issue_key": "CNV-2",
                    "story_points": 100,
                    "rationale": "Huge",
                },
            ],
        })

        with mock.patch(
            "agent.planner.planner.complete",
            return_value=raw_response,
        ):
            result = estimate_story_points(
                epic_summary="Test",
                epic_description="",
                stories=[
                    {"key": "CNV-1", "summary": "A", "description": ""},
                    {"key": "CNV-2", "summary": "B", "description": ""},
                ],
            )
        assert result["CNV-1"] == 8
        assert result["CNV-2"] == 13

    def test_fibonacci_values_unchanged(self):
        from agent.planner.planner import estimate_story_points

        raw_response = json.dumps({
            "estimates": [
                {
                    "issue_key": "CNV-1",
                    "story_points": 5,
                    "rationale": "Standard",
                },
            ],
        })

        with mock.patch(
            "agent.planner.planner.complete",
            return_value=raw_response,
        ):
            result = estimate_story_points(
                epic_summary="Test",
                epic_description="",
                stories=[
                    {"key": "CNV-1", "summary": "A", "description": ""},
                ],
            )
        assert result["CNV-1"] == 5

    def test_compose_stories_clamps_sp(self):
        from agent.planner.planner import compose_stories

        raw_response = json.dumps({
            "stories": [
                {
                    "category": "metrics",
                    "summary": "Add metrics",
                    "description": "Desc",
                    "story_points": 7,
                },
            ],
        })

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

        with mock.patch(
            "agent.planner.planner.complete",
            return_value=raw_response,
        ):
            stories = compose_stories(
                analysis, categories=["metrics"],
            )
        assert len(stories) == 1
        assert stories[0].story_points == 8


class TestLLMError:
    def test_llm_error_is_runtime_error(self):
        exc = LLMError("test failure")
        assert isinstance(exc, RuntimeError)
        assert "test failure" in str(exc)

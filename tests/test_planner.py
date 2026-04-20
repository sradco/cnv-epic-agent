"""Tests for the agent planner: prompt building and response parsing."""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agent.planner.llm import parse_json_response
from mcpserver.jira.client import (
    _extract_source_epic,
    _extract_summary_hash,
    _hash_summary,
    _normalize_summary,
    _should_set_story_points,
    build_epic_jql,
    is_duplicate_story,
    needs_grooming,
)
from schemas.issue_doc import IssueDoc
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
        prompt = build_story_composition_prompt(analysis, include_schema=True)
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
        assert "Sizing" in prompt
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


class TestNormalizeSummary:
    def test_strips_brackets(self):
        assert _normalize_summary(
            "[Observability][metrics] Add VM metrics"
        ) == "add vm metrics"

    def test_collapses_whitespace(self):
        assert _normalize_summary("  Add   VM  metrics  ") == "add vm metrics"

    def test_lowercases(self):
        assert _normalize_summary("Add VM Metrics") == "add vm metrics"


class TestIsDuplicateStory:
    def test_duplicate_by_fingerprint(self):
        target_summary = "[Observability][metrics] Add VM metrics"
        s_hash = _hash_summary(target_summary)
        existing = [
            {
                "key": "CNV-100",
                "summary": "[Observability][metrics] Add VM metrics",
                "labels": ["cnv-observability", "epic-agent-generated"],
                "description": (
                    "some desc\n\n---\n"
                    "_auto-generated by cnv-epic-agent | "
                    f"source_epic=CNV-50 | category=metrics | "
                    f"summary_hash={s_hash}_"
                ),
            },
        ]
        assert is_duplicate_story(
            target_summary,
            "CNV-50",
            existing,
        ) is True

    def test_different_summary_same_category_not_dup(self):
        """Two different metrics stories for the same epic are NOT dups."""
        s_hash = _hash_summary("First metrics story")
        existing = [
            {
                "key": "CNV-100",
                "summary": "First metrics story",
                "labels": ["epic-agent-generated"],
                "description": (
                    "desc\n\n---\n"
                    "_auto-generated by cnv-epic-agent | "
                    f"source_epic=CNV-50 | category=metrics | "
                    f"summary_hash={s_hash}_"
                ),
            },
        ]
        assert is_duplicate_story(
            "Second metrics story",
            "CNV-50",
            existing,
        ) is False

    def test_duplicate_by_summary(self):
        existing = [
            {
                "key": "CNV-100",
                "summary": "[Observability][metrics] Add VM metrics",
                "labels": [],
                "description": "",
            },
        ]
        assert is_duplicate_story(
            "[Observability][metrics] Add VM metrics",
            "CNV-50",
            existing,
        ) is True

    def test_not_duplicate(self):
        existing = [
            {
                "key": "CNV-100",
                "summary": "[Observability][alerts] Add alert",
                "labels": [],
                "description": "",
            },
        ]
        assert is_duplicate_story(
            "[Observability][metrics] Add VM metrics",
            "CNV-50",
            existing,
        ) is False

    def test_empty_existing(self):
        assert is_duplicate_story(
            "Any summary", "CNV-1", [],
        ) is False

    def test_duplicate_by_embedded_key(self):
        """LLM embeds a child key like (CNV-51517) in the summary."""
        existing = [
            {
                "key": "CNV-51517",
                "summary": "Add allocated GPU metric for each VM",
                "labels": [],
                "description": "",
            },
        ]
        assert is_duplicate_story(
            "[Observability][metrics] Add Prometheus metric "
            "for allocated GPU per VM (CNV-51517)",
            "CNV-51516",
            existing,
        ) is True

    def test_duplicate_by_containment(self):
        """Child summary is contained within the LLM summary."""
        existing = [
            {
                "key": "CNV-84407",
                "summary": "virt-launcher GPU metrics scraping "
                           "and Prometheus collector",
                "labels": [],
                "description": "",
            },
        ]
        assert is_duplicate_story(
            "[Observability][metrics] Implement virt-launcher "
            "GPU metrics scraping and Prometheus collector "
            "(CNV-84407)",
            "CNV-51516",
            existing,
        ) is True

    def test_no_containment_for_short_summaries(self):
        """Short child summaries should not trigger containment."""
        existing = [
            {
                "key": "CNV-100",
                "summary": "Fix bug",
                "labels": [],
                "description": "",
            },
        ]
        assert is_duplicate_story(
            "Fix bug in GPU metrics controller",
            "CNV-50",
            existing,
        ) is False

    def test_equal_length_different_summaries_not_dup(self):
        """Same-length but different summaries must not false-positive."""
        existing = [
            {
                "key": "CNV-84407",
                "summary": "virt-launcher GPU metrics scraping "
                           "and Prometheus collector",
                "labels": [],
                "description": "",
            },
        ]
        assert is_duplicate_story(
            "Integrate GPU metrics into existing "
            "KubeVirt/CNV dashboards",
            "CNV-51516",
            existing,
        ) is False


class TestFingerprintFormat:
    def test_create_obs_story_embeds_fingerprint(self):
        """Verify create_obs_story embeds a parseable fingerprint."""
        from unittest.mock import MagicMock
        from mcpserver.jira.client import create_obs_story

        mock_client = MagicMock()
        mock_issue = MagicMock()
        mock_issue.key = "CNV-999"
        mock_client.create_issue.return_value = mock_issue

        cfg = {
            "creation": {
                "project": "CNV",
                "component": "Test",
                "story_label": "epic-agent-generated",
                "epic_label": "cnv-observability",
            },
        }
        summary = "[Observability][metrics] Add VM metrics"
        issue, warnings = create_obs_story(
            mock_client, cfg, "CNV-OBS-1", "CNV-50",
            summary, "Body text",
            category="metrics",
        )
        created_fields = mock_client.create_issue.call_args[1]["fields"]
        desc = created_fields["description"]

        fake_story = {"description": desc}
        assert _extract_source_epic(fake_story) == "CNV-50"
        extracted_hash = _extract_summary_hash(fake_story)
        assert extracted_hash == _hash_summary(summary)
        assert "category=metrics" in desc

    def test_hash_summary_deterministic(self):
        h1 = _hash_summary("Same summary text")
        h2 = _hash_summary("Same summary text")
        assert h1 == h2

    def test_hash_summary_differs(self):
        h1 = _hash_summary("First summary")
        h2 = _hash_summary("Second summary")
        assert h1 != h2


class TestFibonacciValidation:
    def test_clamp_fibonacci_helper(self):
        """Test _clamp_fibonacci directly for deterministic results."""
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
        """Verify estimate_story_points clamps non-Fibonacci values."""
        import unittest.mock as mock
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
        """Valid Fibonacci values pass through untouched."""
        import unittest.mock as mock
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
        """Verify compose_stories also clamps non-Fibonacci SP."""
        import unittest.mock as mock
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
                analysis,
                categories=["metrics"],
            )
        assert len(stories) == 1
        assert stories[0].story_points == 8


class TestLLMError:
    def test_llm_error_is_runtime_error(self):
        from agent.planner.llm import LLMError

        exc = LLMError("test failure")
        assert isinstance(exc, RuntimeError)
        assert "test failure" in str(exc)


class TestConfigValidation:
    def test_invalid_category_raises(self):
        import pytest
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
        import pytest
        from agent.runner import _validate_config

        cfg = {
            "agent": {
                "temperature": "not-a-number",
            },
        }
        with pytest.raises(ValueError, match="temperature"):
            _validate_config(cfg)


class TestBuildEpicJql:
    """Verify JQL filter building for epic scans."""

    _CFG = {
        "jira": {
            "default_project": "CNV",
            "default_since_days": 30,
            "jql_template": (
                "project = {project} AND type = Epic"
                " AND created >= -{since_days}d"
            ),
        },
    }

    def test_default_jql(self):
        jql = build_epic_jql(self._CFG)
        assert "project = CNV" in jql
        assert "type = Epic" in jql
        assert "created >= -30d" in jql

    def test_custom_project_and_days(self):
        jql = build_epic_jql(
            self._CFG, project="OCPBUGS", since_days=7,
        )
        assert "project = OCPBUGS" in jql
        assert "-7d" in jql

    def test_component_filter(self):
        jql = build_epic_jql(self._CFG, component="Virtualization")
        assert 'component = "Virtualization"' in jql

    def test_fix_version_filter(self):
        jql = build_epic_jql(self._CFG, fix_version="4.22")
        assert 'fixVersion = "4.22"' in jql

    def test_target_version_filter(self):
        jql = build_epic_jql(
            self._CFG, target_version="4.22.0",
        )
        assert '"Target Version" = "4.22.0"' in jql

    def test_single_label_filter(self):
        jql = build_epic_jql(self._CFG, labels=["gpu"])
        assert 'labels = "gpu"' in jql

    def test_multiple_labels_filter(self):
        jql = build_epic_jql(
            self._CFG, labels=["gpu", "cnv-4.22"],
        )
        assert 'labels = "gpu"' in jql
        assert 'labels = "cnv-4.22"' in jql

    def test_all_filters_combined(self):
        jql = build_epic_jql(
            self._CFG,
            project="CNV",
            since_days=14,
            component="Virtualization",
            fix_version="4.22",
            target_version="4.22.0",
            labels=["gpu"],
        )
        assert "project = CNV" in jql
        assert "-14d" in jql
        assert 'component = "Virtualization"' in jql
        assert 'fixVersion = "4.22"' in jql
        assert '"Target Version" = "4.22.0"' in jql
        assert 'labels = "gpu"' in jql

    def test_no_filters_no_extra_clauses(self):
        jql = build_epic_jql(self._CFG)
        assert "component" not in jql
        assert "fixVersion" not in jql
        assert "Target Version" not in jql
        assert "labels" not in jql


class TestNeedsGrooming:
    """Verify that needs_grooming flags epics with insufficient detail."""

    _CFG = {
        "grooming": {
            "label": "grooming",
            "min_description_length": 50,
            "min_children": 1,
        },
    }

    def test_short_desc_no_children_needs_grooming(self):
        epic = IssueDoc(
            key="CNV-1", summary="Test", description="Short",
        )
        assert needs_grooming(epic, [], self._CFG) is True

    def test_long_desc_no_children_ok(self):
        epic = IssueDoc(
            key="CNV-2", summary="Test",
            description="A" * 60,
        )
        assert needs_grooming(epic, [], self._CFG) is False

    def test_short_desc_with_children_ok(self):
        epic = IssueDoc(
            key="CNV-3", summary="Test", description="Short",
        )
        child = IssueDoc(
            key="CNV-4", summary="Child", description="child desc",
        )
        assert needs_grooming(epic, [child], self._CFG) is False

    def test_empty_desc_no_children_needs_grooming(self):
        epic = IssueDoc(
            key="CNV-5", summary="Test", description="",
        )
        assert needs_grooming(epic, [], self._CFG) is True

    def test_none_desc_no_children_needs_grooming(self):
        epic = IssueDoc(
            key="CNV-6", summary="Test", description=None,
        )
        assert needs_grooming(epic, [], self._CFG) is True

    def test_defaults_when_no_grooming_config(self):
        epic = IssueDoc(
            key="CNV-7", summary="Test", description="Short",
        )
        assert needs_grooming(epic, [], {}) is True


if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-v"]))

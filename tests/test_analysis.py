"""Tests for the analysis module: need assessment, coverage detection, proposals."""

import json
import os
import sys

import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agent.analyzer.analysis import (
    IssueDoc,
    _propose_new_items,
    assess_monitoring_need,
    build_analysis_result,
    detect_feature_types,
    evaluate_coverage,
    extract_domain_keywords,
    propose_for_categories,
    suggest_telemetry,
)
from agent.analyzer.formatter import build_subtask_payloads, format_analysis_result

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.yaml")


def _load_fixture(name: str):
    with open(os.path.join(FIXTURES, name)) as f:
        return json.load(f)


def _load_config():
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def _make_issues(epic_file: str, stories_file: str) -> tuple[IssueDoc, list[IssueDoc]]:
    epic_data = _load_fixture(epic_file)
    epic = IssueDoc.from_dict(epic_data)
    stories_data = _load_fixture(stories_file)
    children = [IssueDoc.from_dict(s) for s in stories_data]
    return epic, children


class TestNeedAssessment:
    def test_runtime_feature_is_needed(self):
        epic, children = _make_issues("sample_epic.json", "sample_stories.json")
        cfg = _load_config()
        need_cfg = cfg["analysis"]["need_assessment"]
        result = assess_monitoring_need(
            [epic] + children,
            needed_terms=need_cfg["needed_terms"],
            not_needed_terms=need_cfg["not_needed_terms"],
        )
        assert result["need_state"] == "needed"
        assert result["confidence"] in ("high", "medium")
        assert result["score"] >= 2
        assert len(result["needed_evidence"]) > 0

    def test_docs_only_is_not_needed(self):
        epic, children = _make_issues(
            "sample_epic_docs_only.json", "sample_stories_docs_only.json"
        )
        cfg = _load_config()
        need_cfg = cfg["analysis"]["need_assessment"]
        result = assess_monitoring_need(
            [epic] + children,
            needed_terms=need_cfg["needed_terms"],
            not_needed_terms=need_cfg["not_needed_terms"],
        )
        assert result["need_state"] in ("not_needed", "uncertain")
        assert result["score"] <= 1


class TestCoverageDetection:
    def test_no_coverage_when_no_keywords(self):
        epic, children = _make_issues("sample_epic.json", "sample_stories.json")
        cfg = _load_config()
        coverage = evaluate_coverage(
            [epic] + children, cfg["analysis"]["coverage_keywords"]
        )
        for category in ("metrics", "alerts", "dashboards"):
            assert not coverage[category]["present"]

    def test_coverage_detected_when_keyword_present(self):
        issue = IssueDoc(
            key="CNV-99999",
            summary="Add Prometheus metrics for hotplug",
            description="Expose a histogram metric for hotplug latency",
        )
        cfg = _load_config()
        coverage = evaluate_coverage([issue], cfg["analysis"]["coverage_keywords"])
        assert coverage["metrics"]["present"]
        assert not coverage["alerts"]["present"]


class TestFeatureTypeDetection:
    def test_detects_controller_and_data_path(self):
        epic, children = _make_issues("sample_epic.json", "sample_stories.json")
        cfg = _load_config()
        ftypes = detect_feature_types(
            [epic] + children, cfg["proposals"]["feature_type_signals"]
        )
        assert "api_controller" in ftypes
        assert "data_path" in ftypes


class TestProposalGeneration:
    def test_proposals_return_two_section_structure(self):
        from mcp.github.discover import (
            MetricInfo,
            AlertRuleInfo,
            ObservabilityInventory,
        )

        inv = ObservabilityInventory(
            repo_path="test",
            metrics=[
                MetricInfo(
                    name="kubevirt_api_request_total",
                    help="Total API requests",
                    metric_type="counter",
                    file="metrics.go", line=1, repo="kubevirt",
                ),
            ],
            alerts=[
                AlertRuleInfo(
                    name="VMControllerDown",
                    expr="up == 0",
                    severity="critical",
                    file="alerts.go", line=1,
                    source_format="go", repo="kubevirt",
                ),
            ],
        )

        issues = [
            IssueDoc(
                key="CNV-1",
                summary="Improve API request handling",
                description="",
            ),
        ]

        cfg = _load_config()
        patterns_cfg = cfg.get("observability_patterns", {})

        proposals = propose_for_categories(
            missing_categories=["metrics", "alerts"],
            feature_types=["api_controller"],
            inventory=inv,
            issues=issues,
            patterns_cfg=patterns_cfg,
        )

        assert "metrics" in proposals
        assert "existing" in proposals["metrics"]
        assert "proposed" in proposals["metrics"]

        existing_metrics = proposals["metrics"]["existing"]
        assert len(existing_metrics) >= 1
        assert any(
            e["name"] == "kubevirt_api_request_total" for e in existing_metrics
        )
        for e in existing_metrics:
            assert "rationale" in e
            assert len(e["rationale"]) > 0

    def test_existing_items_have_rationale_with_matched_keyword(self):
        from mcp.github.discover import MetricInfo, ObservabilityInventory

        inv = ObservabilityInventory(
            repo_path="test",
            metrics=[
                MetricInfo(
                    name="kubevirt_vmi_migration_succeeded",
                    help="Successful migrations count",
                    metric_type="gauge",
                    file="m.go", line=1, repo="kubevirt",
                ),
            ],
        )
        issues = [
            IssueDoc(
                key="CNV-1",
                summary="Support IP Multicast live migration",
                description="",
            ),
        ]

        proposals = propose_for_categories(
            missing_categories=["metrics"],
            feature_types=["data_path"],
            inventory=inv,
            issues=issues,
        )

        existing = proposals["metrics"]["existing"]
        assert len(existing) == 1
        assert "migration" in existing[0]["rationale"]

    def test_proposed_items_from_patterns(self):
        cfg = _load_config()
        patterns_cfg = cfg.get("observability_patterns", {})

        proposals = propose_for_categories(
            missing_categories=["metrics"],
            feature_types=["data_path"],
            inventory=None,
            issues=[
                IssueDoc(
                    key="CNV-1",
                    summary="Support IP Multicast live migration",
                    description="",
                ),
            ],
            patterns_cfg=patterns_cfg,
        )

        proposed = proposals["metrics"]["proposed"]
        assert len(proposed) >= 1
        for p in proposed:
            assert "name_hint" in p
            assert "rationale" in p
            assert "user_action" in p
            assert len(p["rationale"]) > 0

    def test_no_inventory_produces_empty_existing(self):
        proposals = propose_for_categories(
            missing_categories=["dashboards"],
            feature_types=["api_controller"],
            inventory=None,
        )
        assert "dashboards" in proposals
        assert proposals["dashboards"]["existing"] == []

    def test_empty_inventory_produces_empty_existing(self):
        from mcp.github.discover import ObservabilityInventory

        inv = ObservabilityInventory(repo_path="test")
        issues = [
            IssueDoc(
                key="CNV-1",
                summary="Enable hotplug for disks",
                description="",
            ),
        ]
        proposals = propose_for_categories(
            missing_categories=["metrics"],
            feature_types=["api_controller"],
            inventory=inv,
            issues=issues,
        )
        assert "metrics" in proposals
        assert proposals["metrics"]["existing"] == []


class TestProposeNewItems:
    def test_migration_pattern_produces_metrics(self):
        cfg = _load_config()
        patterns_cfg = cfg.get("observability_patterns", {})

        items = _propose_new_items(
            category="metrics",
            domain_keywords=["migration", "live migration"],
            feature_types=["data_path"],
            epic_summary="Support IP Multicast live migration",
            patterns_cfg=patterns_cfg,
        )

        assert len(items) >= 2
        for item in items:
            assert "name_hint" in item
            assert "rationale" in item
            assert "user_action" in item
            assert "type" in item
            assert "{domain}" not in item["name_hint"]
            assert "{domain}" not in item["rationale"]

    def test_api_controller_pattern_produces_alerts(self):
        cfg = _load_config()
        patterns_cfg = cfg.get("observability_patterns", {})

        items = _propose_new_items(
            category="alerts",
            domain_keywords=["controller", "reconciler"],
            feature_types=["api_controller"],
            epic_summary="Add new controller for VM snapshots",
            patterns_cfg=patterns_cfg,
        )

        assert len(items) >= 1
        for item in items:
            assert "name_hint" in item
            assert "rationale" in item

    def test_no_matching_pattern_returns_empty(self):
        items = _propose_new_items(
            category="metrics",
            domain_keywords=["zzz-no-match"],
            feature_types=["zzz-no-type"],
            epic_summary="Completely unrelated documentation update",
            patterns_cfg={},
        )
        assert items == []

    def test_dashboard_pattern_uses_panel_hint(self):
        cfg = _load_config()
        patterns_cfg = cfg.get("observability_patterns", {})

        items = _propose_new_items(
            category="dashboards",
            domain_keywords=["storage", "disk"],
            feature_types=["data_path"],
            epic_summary="Improve storage I/O for disk hotplug",
            patterns_cfg=patterns_cfg,
        )

        for item in items:
            assert "panel_hint" in item
            assert "rationale" in item


class TestTelemetryRationale:
    def test_suggest_telemetry_includes_rationale(self):
        from mcp.github.discover import ObservabilityInventory, RecordingRuleInfo

        inv = ObservabilityInventory(
            repo_path="test",
            recording_rules=[
                RecordingRuleInfo(
                    name="cnv:vmi_migration_phase:sum",
                    expr='sum(kubevirt_vmi_migration_phase)',
                    file="rules.yaml", line=1, repo="kubevirt",
                    source_format="yaml",
                ),
            ],
            telemetry_allowlist=[],
        )

        issues = [
            IssueDoc(
                key="CNV-1",
                summary="Support IP Multicast live migration",
                description="",
            ),
        ]

        candidates = suggest_telemetry(inventory=inv, issues=issues)
        assert len(candidates) == 1
        assert "rationale" in candidates[0]
        assert len(candidates[0]["rationale"]) > 0
        assert "sum" in candidates[0]["rationale"].lower()

    def test_rationale_mentions_cluster_prefix(self):
        from mcp.github.discover import ObservabilityInventory, RecordingRuleInfo

        inv = ObservabilityInventory(
            repo_path="test",
            recording_rules=[
                RecordingRuleInfo(
                    name="cluster:vmi_count",
                    expr='count(kubevirt_vmi_info)',
                    file="rules.yaml", line=1, repo="kubevirt",
                    source_format="yaml",
                ),
            ],
            telemetry_allowlist=[],
        )

        candidates = suggest_telemetry(inventory=inv)
        assert len(candidates) == 1
        assert "cluster level" in candidates[0]["rationale"].lower()


class TestFullPipeline:
    def test_runtime_epic_produces_gaps_and_proposals(self):
        epic, children = _make_issues("sample_epic.json", "sample_stories.json")
        cfg = _load_config()
        result = build_analysis_result(epic, children, cfg)

        assert result["need_state"] == "needed"
        assert len(result["gaps"]) > 0
        assert len(result["proposals"]) > 0
        assert result["apply_allowed"]
        assert result["would_create_count"] > 0
        assert result["recommended_action"] in ("create now", "review first")

    def test_docs_epic_produces_no_gaps(self):
        epic, children = _make_issues(
            "sample_epic_docs_only.json", "sample_stories_docs_only.json"
        )
        cfg = _load_config()
        result = build_analysis_result(epic, children, cfg)

        assert result["need_state"] in ("not_needed", "uncertain")
        assert len(result["gaps"]) == 0
        assert result["recommended_action"] in ("skip", "review first")

    def test_proposals_have_two_section_structure(self):
        epic, children = _make_issues("sample_epic.json", "sample_stories.json")
        cfg = _load_config()
        result = build_analysis_result(epic, children, cfg)

        for category, data in result["proposals"].items():
            assert isinstance(data, dict), (
                f"proposals[{category}] should be a dict"
            )
            assert "existing" in data
            assert "proposed" in data
            for e in data["existing"]:
                assert "rationale" in e
            for p in data["proposed"]:
                assert "rationale" in p

    def test_formatter_produces_markdown(self):
        epic, children = _make_issues("sample_epic.json", "sample_stories.json")
        cfg = _load_config()
        result = build_analysis_result(epic, children, cfg)
        text = format_analysis_result(result)

        assert "# Observability Analysis:" in text
        assert "Monitoring needed:" in text
        assert "What we should add" in text
        assert "Recommended action:" in text

    def test_subtask_payloads(self):
        epic, children = _make_issues("sample_epic.json", "sample_stories.json")
        cfg = _load_config()
        result = build_analysis_result(epic, children, cfg)
        payloads = build_subtask_payloads(result, cfg)

        assert len(payloads) > 0
        for p in payloads:
            assert "[Observability]" in p["summary"]
            assert "Why this is needed" in p["description"]
            assert "Acceptance criteria" in p["description"]


class TestAnalysisDataOutput:
    """Tests for the enriched build_analysis_result output used by
    the get_analysis_data MCP tool (AI-assisted workflow)."""

    def test_includes_epic_description(self):
        epic, children = _make_issues("sample_epic.json", "sample_stories.json")
        cfg = _load_config()
        result = build_analysis_result(epic, children, cfg)

        assert "epic_description" in result
        assert result["epic_description"] == epic.description

    def test_includes_child_issues_with_text(self):
        epic, children = _make_issues("sample_epic.json", "sample_stories.json")
        cfg = _load_config()
        result = build_analysis_result(epic, children, cfg)

        assert "child_issues" in result
        assert isinstance(result["child_issues"], list)
        assert len(result["child_issues"]) == len(children)

        for child_data, child_doc in zip(result["child_issues"], children):
            assert child_data["key"] == child_doc.key
            assert child_data["summary"] == child_doc.summary
            assert child_data["description"] == child_doc.description

    def test_includes_domain_keywords(self):
        epic, children = _make_issues("sample_epic.json", "sample_stories.json")
        cfg = _load_config()
        result = build_analysis_result(epic, children, cfg)

        assert "domain_keywords" in result
        assert isinstance(result["domain_keywords"], list)
        assert len(result["domain_keywords"]) > 0

    def test_json_serializable(self):
        epic, children = _make_issues("sample_epic.json", "sample_stories.json")
        cfg = _load_config()
        result = build_analysis_result(epic, children, cfg)

        serialized = json.dumps(result, default=str)
        parsed = json.loads(serialized)

        assert parsed["epic_key"] == result["epic_key"]
        assert parsed["epic_summary"] == result["epic_summary"]
        assert parsed["epic_description"] == result["epic_description"]
        assert parsed["child_issues"] == result["child_issues"]
        assert parsed["domain_keywords"] == result["domain_keywords"]
        assert parsed["gaps"] == result["gaps"]
        assert parsed["proposals"] == result["proposals"]

    def test_docs_epic_still_includes_enriched_fields(self):
        epic, children = _make_issues(
            "sample_epic_docs_only.json", "sample_stories_docs_only.json"
        )
        cfg = _load_config()
        result = build_analysis_result(epic, children, cfg)

        assert "epic_description" in result
        assert "child_issues" in result
        assert "domain_keywords" in result


if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-v"]))

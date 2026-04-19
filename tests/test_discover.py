"""Tests for the code discovery module."""

import fnmatch
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from mcp.github.discover import (
    ObservabilityInventory,
    TelemetryAllowlistEntry,
    _parse_cmo_telemetry_yaml,
    _repo_name,
    _scan_cmo_telemetry_configmap,
    _scan_perses_yaml,
    build_all_inventories,
    discover_observability,
    extract_metric_names_from_promql,
    find_unvisualized_metrics,
    format_inventory,
    invalidate_cache,
    scan_go_file,
)

FAKE_REPO = os.path.join(os.path.dirname(__file__), "fixtures", "fake_repo")
FAKE_REPO_B = os.path.join(os.path.dirname(__file__), "fixtures", "fake_repo_b")


class TestGoMetricScanning:
    def test_finds_operator_metrics(self):
        fpath = os.path.join(FAKE_REPO, "pkg/monitoring/metrics/operator_metrics.go")
        metrics, _, _, _ = scan_go_file(fpath)
        assert len(metrics) >= 1
        names = {m.name for m in metrics}
        assert "kubevirt_virt_operator_leading_status" in names

    def test_metric_type_detected(self):
        fpath = os.path.join(FAKE_REPO, "pkg/monitoring/metrics/operator_metrics.go")
        metrics, _, _, _ = scan_go_file(fpath)
        for m in metrics:
            if m.name == "kubevirt_virt_operator_leading_status":
                assert m.metric_type in ("counter", "gauge", "histogram", "summary")


class TestGoAlertScanning:
    def test_finds_alerts(self):
        fpath = os.path.join(FAKE_REPO, "pkg/monitoring/rules/alerts/vms.go")
        _, alerts, _, _ = scan_go_file(fpath)
        assert len(alerts) >= 1
        names = {a.name for a in alerts}
        assert "VirtLauncherPodsStuckFailed" in names

    def test_severity_extracted(self):
        fpath = os.path.join(FAKE_REPO, "pkg/monitoring/rules/alerts/vms.go")
        _, alerts, _, _ = scan_go_file(fpath)
        for a in alerts:
            if a.name == "VirtLauncherPodsStuckFailed":
                assert a.severity == "critical"

    def test_cross_repo_alerts(self):
        fpath = os.path.join(FAKE_REPO_B, "pkg/monitoring/rules/alerts/cdi_alerts.go")
        _, alerts, _, _ = scan_go_file(fpath)
        assert len(alerts) >= 1


class TestGoRecordingRuleScanning:
    def test_finds_recording_rules(self):
        fpath = os.path.join(FAKE_REPO, "pkg/monitoring/rules/recordingrules/operator.go")
        _, _, recording, _ = scan_go_file(fpath)
        assert len(recording) >= 1
        names = {r.name for r in recording}
        assert "kubevirt_hyperconverged_operator_health_status" in names


class TestYamlPrometheusRule:
    def test_finds_yaml_alerts_and_recordings(self):
        inv = discover_observability(FAKE_REPO)
        yaml_alerts = [a for a in inv.alerts if a.source_format == "yaml"]
        yaml_recordings = [r for r in inv.recording_rules if r.source_format == "yaml"]
        assert len(yaml_alerts) >= 1
        assert len(yaml_recordings) >= 1

    def test_yaml_alert_severity(self):
        inv = discover_observability(FAKE_REPO)
        yaml_alerts = [a for a in inv.alerts if a.source_format == "yaml"]
        for a in yaml_alerts:
            assert a.severity in ("critical", "warning", "info", "unknown")


class TestFullDiscovery:
    def test_discovers_everything(self):
        inv = discover_observability(FAKE_REPO)
        assert len(inv.metrics) >= 1
        assert len(inv.alerts) >= 1
        assert len(inv.recording_rules) >= 1
        assert len(inv.dashboards) >= 1

    def test_summary(self):
        inv = discover_observability(FAKE_REPO)
        summary = inv.summary()
        assert "metrics" in summary
        assert "alerts" in summary

    def test_to_dict(self):
        inv = discover_observability(FAKE_REPO)
        d = inv.to_dict()
        assert isinstance(d, dict)
        assert "metrics" in d
        assert all(isinstance(m, dict) for m in d["metrics"])


class TestPersesDashboards:
    def test_finds_go_dashboard(self):
        inv = discover_observability(FAKE_REPO)
        go_dashboards = [d for d in inv.dashboards if d.dashboard_type == "perses-go"]
        assert len(go_dashboards) >= 1

    def test_finds_perses_yaml_dashboard(self):
        inv = discover_observability(FAKE_REPO)
        yaml_dashboards = [d for d in inv.dashboards if d.dashboard_type == "perses-yaml"]
        assert len(yaml_dashboards) >= 1

    def test_panel_scanning(self):
        inv = discover_observability(FAKE_REPO)
        assert len(inv.panels) >= 1


class TestPromQLExtraction:
    def test_basic_extraction(self):
        expr = 'sum(rate(kubevirt_vmi_network_traffic_bytes_total{type="rx"}[5m]))'
        names = extract_metric_names_from_promql(expr)
        assert "kubevirt_vmi_network_traffic_bytes_total" in names

    def test_multiple_metrics(self):
        expr = 'kubevirt_vmi_memory_used_bytes{} / kubevirt_vmi_memory_available_bytes{}'
        names = extract_metric_names_from_promql(expr)
        assert "kubevirt_vmi_memory_used_bytes" in names
        assert "kubevirt_vmi_memory_available_bytes" in names

    def test_excludes_functions(self):
        expr = 'sum(rate(my_metric{job="test"}[5m]))'
        names = extract_metric_names_from_promql(expr)
        assert "sum" not in names
        assert "rate" not in names
        assert "my_metric" in names


class TestUnvisualizedMetrics:
    def test_finds_unvisualized(self):
        inv = discover_observability(FAKE_REPO)
        unviz = find_unvisualized_metrics(inv)
        assert isinstance(unviz, list)


class TestFormatInventory:
    def test_produces_markdown(self):
        inv = discover_observability(FAKE_REPO)
        text = format_inventory(inv)
        assert "# Observability Inventory" in text
        assert "Prometheus Metrics" in text


class TestRepoName:
    def test_url(self):
        assert _repo_name("https://github.com/kubevirt/kubevirt") == "kubevirt"

    def test_path(self):
        assert _repo_name("/home/user/repos/kubevirt") == "kubevirt"

    def test_git_suffix(self):
        assert _repo_name("https://github.com/kubevirt/kubevirt.git") == "kubevirt"


class TestCMOTelemetry:
    def test_parse_configmap(self):
        fpath = os.path.join(
            FAKE_REPO, "manifests/cmo/0000_50_cluster-monitoring-operator_04-config.yaml"
        )
        if os.path.isfile(fpath):
            entries = _scan_cmo_telemetry_configmap(fpath)
            assert isinstance(entries, list)

    def test_parse_yaml_content(self):
        sample = """
kind: ConfigMap
metadata:
  name: telemetry-config
data:
  metrics.yaml: |
    matches:
      - '{__name__="cnv_abnormal", reason=~".*"}'
      - '{__name__="kubevirt_vmi_phase_count"}'
"""
        entries = _parse_cmo_telemetry_yaml(sample, source="test")
        assert len(entries) >= 2
        names = {e.metric_name for e in entries}
        assert "cnv_abnormal" in names
        assert "kubevirt_vmi_phase_count" in names


class TestBuildAllInventories:
    def test_with_local_config(self):
        cfg = {
            "discovery": {
                "repos": [FAKE_REPO],
            },
            "telemetry": {},
        }
        invalidate_cache("")
        inv = build_all_inventories(cfg)
        assert len(inv.metrics) >= 1
        assert len(inv.alerts) >= 1
        invalidate_cache("")

    def test_caching(self):
        cfg = {
            "discovery": {
                "repos": [FAKE_REPO],
            },
            "telemetry": {},
        }
        invalidate_cache("")
        inv1 = build_all_inventories(cfg)
        inv2 = build_all_inventories(cfg)
        assert inv1 is inv2
        invalidate_cache("")


class TestPersesYamlDashboard:
    def test_scan_perses_yaml(self):
        fpath = os.path.join(
            FAKE_REPO,
            "controllers/perses/resources/dashboards/perses-dashboard-test.yaml",
        )
        dashboards, panels = _scan_perses_yaml(fpath)
        assert len(dashboards) >= 1


if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-v"]))

"""Scan repos for observability artifacts.

Supports both local checkouts and upstream GitHub URLs (via shallow clone).
Finds Prometheus metrics, alerting/recording rules, and dashboards
by parsing Go source files and YAML PrometheusRule manifests.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


@dataclass
class MetricInfo:
    name: str
    help: str
    metric_type: str  # counter, gauge, histogram, summary
    file: str
    line: int
    repo: str = ""


@dataclass
class AlertRuleInfo:
    name: str
    expr: str
    severity: str
    file: str
    line: int
    source_format: str  # "yaml" or "go"
    repo: str = ""


@dataclass
class RecordingRuleInfo:
    name: str
    expr: str
    file: str
    line: int
    source_format: str
    repo: str = ""


@dataclass
class DashboardInfo:
    name: str
    file: str
    dashboard_type: str  # "perses-go", "grafana-json", "perses-yaml"
    repo: str = ""


@dataclass
class PanelInfo:
    name: str
    dashboard: str
    queries: list[str]
    file: str
    repo: str = ""


@dataclass
class TelemetryAllowlistEntry:
    """A single entry from the CMO telemetry allowlist."""
    metric_name: str
    match_expr: str  # full selector, e.g. '{__name__="cnv_abnormal", reason=~"..."}'
    file: str
    repo: str = ""


@dataclass
class ObservabilityInventory:
    """Everything found in a repo checkout."""
    repo_path: str
    metrics: list[MetricInfo] = field(default_factory=list)
    alerts: list[AlertRuleInfo] = field(default_factory=list)
    recording_rules: list[RecordingRuleInfo] = field(default_factory=list)
    dashboards: list[DashboardInfo] = field(default_factory=list)
    panels: list[PanelInfo] = field(default_factory=list)
    telemetry_allowlist: list[TelemetryAllowlistEntry] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo_path": self.repo_path,
            "metrics": [
                {"name": m.name, "help": m.help, "type": m.metric_type,
                 "file": m.file, "line": m.line, "repo": m.repo}
                for m in self.metrics
            ],
            "alerts": [
                {"name": a.name, "expr": a.expr, "severity": a.severity,
                 "file": a.file, "line": a.line, "source": a.source_format,
                 "repo": a.repo}
                for a in self.alerts
            ],
            "recording_rules": [
                {"name": r.name, "expr": r.expr,
                 "file": r.file, "line": r.line, "source": r.source_format,
                 "repo": r.repo}
                for r in self.recording_rules
            ],
            "dashboards": [
                {"name": d.name, "file": d.file, "type": d.dashboard_type,
                 "repo": d.repo}
                for d in self.dashboards
            ],
            "panels": [
                {"name": p.name, "dashboard": p.dashboard,
                 "queries": p.queries, "file": p.file, "repo": p.repo}
                for p in self.panels
            ],
            "telemetry_allowlist": [
                {"metric_name": t.metric_name, "match_expr": t.match_expr,
                 "file": t.file, "repo": t.repo}
                for t in self.telemetry_allowlist
            ],
        }

    def summary(self) -> str:
        return (
            f"{len(self.metrics)} metrics, "
            f"{len(self.alerts)} alerts, "
            f"{len(self.recording_rules)} recording rules, "
            f"{len(self.dashboards)} dashboards, "
            f"{len(self.panels)} panels, "
            f"{len(self.telemetry_allowlist)} telemetry allowlist entries"
        )


# ---------------------------------------------------------------------------
# Go source scanning
# ---------------------------------------------------------------------------

# operatormetrics.MetricOpts{Name: "kubevirt_vmi_*", Help: "..."}
_GO_METRIC_OPTS_RE = re.compile(
    r'MetricOpts\s*\{[^}]*?'
    r'Name:\s*["`]([^"`]+)["`]\s*,'
    r'[^}]*?Help:\s*["`]([^"`]+)["`]',
    re.DOTALL,
)

# operatormetrics.NewGauge / NewCounter / NewCounterVec / NewHistogram etc.
_GO_METRIC_TYPE_RE = re.compile(
    r'(?:operatormetrics|prometheus|promauto)\.'
    r'(?:New|Must)?(Counter|Gauge|Histogram|Summary)(?:Vec)?\s*\(',
    re.IGNORECASE,
)

# promv1.Rule{ Alert: "...", Expr: intstr.FromString("...")
_GO_ALERT_RE = re.compile(
    r'Alert:\s*["`]([^"`]+)["`]',
)
_GO_EXPR_RE = re.compile(
    r'Expr:\s*(?:intstr\.FromString\()?["`]([^"`]+)["`]',
)
_GO_SEVERITY_RE = re.compile(
    r'(?:'
    r'["\']severity["\']\s*:\s*["\'](\w+)["\']'  # literal key
    r'|'
    r'severity\w*(?:Key|Label)\w*\s*:\s*["\'](\w+)["\']'  # constant key like severityAlertLabelKey
    r')',
    re.IGNORECASE,
)

# operatorrules.RecordingRule with MetricsOpts{Name: "..."}
_GO_RECORDING_NAME_RE = re.compile(
    r'Name:\s*["`]([^"`]+)["`]',
)

# Perses dashboard builder function — look for dashboard.New("name")
_GO_PERSES_DASHBOARD_RE = re.compile(
    r'dashboard\.New\s*\(\s*["`]([^"`]+)["`]',
)

# dashboard.Name("Virtualization / Clusters Overview")
_GO_DASHBOARD_NAME_RE = re.compile(
    r'dashboard\.Name\s*\(\s*["`]([^"`]+)["`]',
)

# panelgroup.AddPanel("panel title", ...)
_GO_PANEL_TITLE_RE = re.compile(
    r'panelgroup\.AddPanel\s*\(\s*["`]([^"`]+)["`]',
)

# query.PromQL(constName, ...) — capture the first identifier argument
_GO_PROMQL_REF_RE = re.compile(
    r'query\.PromQL\s*\(\s*([a-zA-Z_]\w*)',
)

# Go const definitions: backtick-delimited PromQL strings
# Handles both `const name = \`...\`` and grouped `const ( name = \`...\` )`
_GO_CONST_BACKTICK_RE = re.compile(
    r'(\w+)\s*=\s*`([^`]*)`',
    re.DOTALL,
)

SKIP_DIRS = {"vendor", "node_modules", ".git", "_output", "bin", "hack/tools"}


def _should_skip(dirpath: str) -> bool:
    parts = Path(dirpath).parts
    return any(p in SKIP_DIRS for p in parts)


def _scan_go_metrics(content: str, filepath: str) -> list[MetricInfo]:
    """Extract Prometheus metric registrations from Go source."""
    results: list[MetricInfo] = []
    seen_names: set[str] = set()

    for match in _GO_METRIC_OPTS_RE.finditer(content):
        name = match.group(1)
        help_text = match.group(2)
        if name in seen_names:
            continue
        seen_names.add(name)

        line = content[:match.start()].count("\n") + 1

        # Try to find the metric type from nearby context
        ctx_start = max(0, match.start() - 200)
        ctx = content[ctx_start:match.end()]
        type_match = _GO_METRIC_TYPE_RE.search(ctx)
        metric_type = type_match.group(1).lower() if type_match else "unknown"

        results.append(MetricInfo(
            name=name, help=help_text, metric_type=metric_type,
            file=filepath, line=line,
        ))

    return results


def _scan_go_alerts(content: str, filepath: str) -> list[AlertRuleInfo]:
    """Extract alert rules defined as Go structs (promv1.Rule)."""
    results: list[AlertRuleInfo] = []

    # Split into blocks by looking for Alert: patterns
    for alert_match in _GO_ALERT_RE.finditer(content):
        alert_name = alert_match.group(1)
        line = content[:alert_match.start()].count("\n") + 1

        # Find expr and severity in surrounding context
        block_end = min(len(content), alert_match.end() + 800)
        block = content[alert_match.start():block_end]

        expr_match = _GO_EXPR_RE.search(block)
        expr = expr_match.group(1) if expr_match else ""

        sev_match = _GO_SEVERITY_RE.search(block)
        severity = "unknown"
        if sev_match:
            severity = sev_match.group(1) or sev_match.group(2) or "unknown"

        results.append(AlertRuleInfo(
            name=alert_name, expr=expr, severity=severity,
            file=filepath, line=line, source_format="go",
        ))

    return results


def _scan_go_recording_rules(content: str, filepath: str) -> list[RecordingRuleInfo]:
    """Extract recording rules from Go structs (operatorrules.RecordingRule)."""
    if "RecordingRule" not in content:
        return []

    results: list[RecordingRuleInfo] = []
    seen_names: set[str] = set()

    # Find all MetricsOpts blocks within the file. Each recording rule has
    # a MetricsOpts with Name + an Expr nearby. We rely on the file
    # already being identified as containing RecordingRule.
    for opts_match in _GO_METRIC_OPTS_RE.finditer(content):
        name = opts_match.group(1)
        if name in seen_names:
            continue

        # Look for Expr in the surrounding context (after MetricsOpts)
        ctx_end = min(len(content), opts_match.end() + 400)
        ctx = content[opts_match.start():ctx_end]
        expr_match = _GO_EXPR_RE.search(ctx)
        if not expr_match:
            continue

        seen_names.add(name)
        line = content[:opts_match.start()].count("\n") + 1

        results.append(RecordingRuleInfo(
            name=name, expr=expr_match.group(1), file=filepath,
            line=line, source_format="go",
        ))

    return results


def _scan_go_dashboards(content: str, filepath: str) -> list[DashboardInfo]:
    """Find Perses dashboard definitions in Go code."""
    results: list[DashboardInfo] = []
    for match in _GO_PERSES_DASHBOARD_RE.finditer(content):
        results.append(DashboardInfo(
            name=match.group(1), file=filepath,
            dashboard_type="perses-go",
        ))
    return results


def _build_const_map(directory: str) -> dict[str, str]:
    """Build a map of Go const names to their backtick-delimited values.

    Scans all .go files in the same directory to resolve cross-file const
    references (e.g. panel files referencing query consts in *-queries.go).
    """
    const_map: dict[str, str] = {}
    dirpath = Path(directory)
    if not dirpath.is_dir():
        return const_map
    for gofile in dirpath.glob("*.go"):
        try:
            text = gofile.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in _GO_CONST_BACKTICK_RE.finditer(text):
            const_map[m.group(1)] = m.group(2).strip()
    return const_map


def _resolve_promql(const_name: str, const_map: dict[str, str]) -> str:
    """Resolve a Go const name to its PromQL value, following simple concatenation."""
    return const_map.get(const_name, "")


def _scan_go_panels(
    content: str,
    filepath: str,
    const_map: dict[str, str],
    dashboard_name: str = "",
) -> list[PanelInfo]:
    """Extract panel definitions and their PromQL queries from Go panel files."""
    results: list[PanelInfo] = []

    for panel_match in _GO_PANEL_TITLE_RE.finditer(content):
        panel_title = panel_match.group(1)
        # Find the function body scope: from this AddPanel call to the next
        # top-level function or end of file
        func_end = len(content)
        next_panel = _GO_PANEL_TITLE_RE.search(content, panel_match.end())
        if next_panel:
            func_end = next_panel.start()

        block = content[panel_match.start():func_end]
        query_refs = _GO_PROMQL_REF_RE.findall(block)
        queries = []
        for ref in query_refs:
            promql = _resolve_promql(ref, const_map)
            if promql:
                queries.append(promql)

        results.append(PanelInfo(
            name=panel_title,
            dashboard=dashboard_name,
            queries=queries,
            file=filepath,
        ))

    return results


def _scan_perses_yaml(filepath: str) -> tuple[
    list[DashboardInfo], list[PanelInfo],
]:
    """Parse a Perses PersesDashboard YAML for dashboard and panel info."""
    dashboards: list[DashboardInfo] = []
    panels: list[PanelInfo] = []

    try:
        with open(filepath, encoding="utf-8", errors="replace") as f:
            docs = list(yaml.safe_load_all(f))
    except Exception:
        logger.debug("Failed to parse Perses YAML %s", filepath, exc_info=True)
        return [], []

    for doc in docs:
        if not isinstance(doc, dict):
            continue
        if doc.get("kind") != "PersesDashboard":
            continue

        spec = doc.get("spec", {})
        display = spec.get("display", {})
        dashboard_name = display.get("name", Path(filepath).stem)
        dashboards.append(DashboardInfo(
            name=dashboard_name,
            file=filepath,
            dashboard_type="perses-yaml",
        ))

        panels_map = spec.get("panels", {})
        if not isinstance(panels_map, dict):
            continue
        for _panel_key, panel_def in panels_map.items():
            if not isinstance(panel_def, dict):
                continue
            panel_spec = panel_def.get("spec", {})
            panel_display = panel_spec.get("display", {})
            panel_name = panel_display.get("name", _panel_key)

            queries_list = panel_spec.get("queries", [])
            promql_exprs: list[str] = []
            for q in queries_list:
                if not isinstance(q, dict):
                    continue
                q_spec = q.get("spec", {})
                q_plugin = q_spec.get("plugin", {})
                q_plugin_spec = q_plugin.get("spec", {})
                query_str = q_plugin_spec.get("query", "")
                if query_str:
                    promql_exprs.append(str(query_str).strip())

            panels.append(PanelInfo(
                name=panel_name,
                dashboard=dashboard_name,
                queries=promql_exprs,
                file=filepath,
            ))

    return dashboards, panels


def scan_go_file(filepath: str) -> tuple[
    list[MetricInfo], list[AlertRuleInfo],
    list[RecordingRuleInfo], list[DashboardInfo],
]:
    try:
        with open(filepath, encoding="utf-8", errors="replace") as f:
            content = f.read()
    except (OSError, UnicodeDecodeError):
        return [], [], [], []

    metrics = _scan_go_metrics(content, filepath)
    alerts = _scan_go_alerts(content, filepath)
    recording = _scan_go_recording_rules(content, filepath)
    dashboards = _scan_go_dashboards(content, filepath)
    return metrics, alerts, recording, dashboards


# ---------------------------------------------------------------------------
# YAML PrometheusRule scanning
# ---------------------------------------------------------------------------

def _scan_yaml_prometheus_rule(filepath: str) -> tuple[
    list[AlertRuleInfo], list[RecordingRuleInfo],
]:
    """Parse a YAML file for PrometheusRule alert and recording rules."""
    alerts: list[AlertRuleInfo] = []
    recordings: list[RecordingRuleInfo] = []

    try:
        with open(filepath, encoding="utf-8", errors="replace") as f:
            docs = list(yaml.safe_load_all(f))
    except Exception:
        logger.debug("Failed to parse PrometheusRule YAML %s", filepath, exc_info=True)
        return [], []

    for doc in docs:
        if not isinstance(doc, dict):
            continue
        if doc.get("kind") != "PrometheusRule":
            continue

        spec = doc.get("spec", {})
        for group in spec.get("groups", []):
            for rule in group.get("rules", []):
                if "alert" in rule:
                    labels = rule.get("labels", {})
                    alerts.append(AlertRuleInfo(
                        name=rule["alert"],
                        expr=str(rule.get("expr", "")),
                        severity=labels.get("severity", "unknown"),
                        file=filepath, line=0,
                        source_format="yaml",
                    ))
                elif "record" in rule:
                    recordings.append(RecordingRuleInfo(
                        name=rule["record"],
                        expr=str(rule.get("expr", "")),
                        file=filepath, line=0,
                        source_format="yaml",
                    ))

    return alerts, recordings


# ---------------------------------------------------------------------------
# CMO telemetry allowlist scanning
# ---------------------------------------------------------------------------

_MATCH_NAME_RE = re.compile(r'__name__\s*=\s*[~]?\s*"([^"]+)"')


def _parse_cmo_telemetry_yaml(
    yaml_content: str,
    source: str = "",
) -> list[TelemetryAllowlistEntry]:
    """Extract telemetry match entries from CMO telemetry ConfigMap YAML."""
    try:
        docs = list(yaml.safe_load_all(yaml_content))
    except Exception:
        logger.debug("Failed to parse CMO telemetry YAML", exc_info=True)
        return []

    entries: list[TelemetryAllowlistEntry] = []
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        kind = doc.get("kind", "")
        if kind and kind != "ConfigMap":
            continue

        data = doc.get("data", {})
        if not isinstance(data, dict):
            continue

        metrics_yaml_str = data.get("metrics.yaml", "")
        if not metrics_yaml_str:
            continue

        try:
            inner = yaml.safe_load(metrics_yaml_str)
        except Exception:
            logger.debug("Failed to parse inner metrics YAML in CMO ConfigMap", exc_info=True)
            continue

        if not isinstance(inner, dict):
            continue

        for match_entry in inner.get("matches", []):
            if not isinstance(match_entry, str):
                continue
            m = _MATCH_NAME_RE.search(match_entry)
            if m:
                entries.append(TelemetryAllowlistEntry(
                    metric_name=m.group(1),
                    match_expr=match_entry.strip(),
                    file=source,
                    repo="cluster-monitoring-operator",
                ))

    return entries


def _scan_cmo_telemetry_configmap(
    filepath: str,
) -> list[TelemetryAllowlistEntry]:
    """Extract telemetry match entries from a local CMO telemetry ConfigMap."""
    try:
        with open(filepath, encoding="utf-8", errors="replace") as f:
            content = f.read()
    except (OSError, UnicodeDecodeError):
        return []
    return _parse_cmo_telemetry_yaml(content, source=filepath)


def fetch_cmo_allowlist(url: str) -> list[TelemetryAllowlistEntry]:
    """Fetch the CMO telemetry allowlist from a raw GitHub URL."""
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            content = resp.read().decode("utf-8", errors="replace")
    except Exception as exc:
        logger.warning("Failed to fetch CMO allowlist from %s: %s", url, exc)
        return []

    return _parse_cmo_telemetry_yaml(content, source=url)


# ---------------------------------------------------------------------------
# Grafana JSON dashboard scanning
# ---------------------------------------------------------------------------

def _scan_grafana_json(filepath: str) -> list[DashboardInfo]:
    """Check if a JSON file looks like a Grafana dashboard."""
    import json
    try:
        with open(filepath, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        logger.debug("Failed to parse Grafana JSON %s", filepath, exc_info=True)
        return []

    if isinstance(data, dict) and "panels" in data and ("title" in data or "uid" in data):
        title = data.get("title", Path(filepath).stem)
        return [DashboardInfo(name=title, file=filepath, dashboard_type="grafana-json")]
    return []


# ---------------------------------------------------------------------------
# Top-level scanner
# ---------------------------------------------------------------------------

def _repo_name(path_or_url: str) -> str:
    """Extract a short repo name from a URL or path."""
    return path_or_url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")


def _is_url(path_or_url: str) -> bool:
    return path_or_url.startswith(("https://", "http://", "git@"))


def fetch_repo(
    url: str,
    branch: str = "",
    cache_dir: str = "",
) -> str:
    """Shallow-clone a git repo and return the local path.

    Uses a persistent cache directory so repeated scans of the same repo
    just do a fetch instead of a full clone.
    """
    if not cache_dir:
        cache_dir = os.path.join(tempfile.gettempdir(), "cnv-epic-agent-repos")
    os.makedirs(cache_dir, exist_ok=True)

    # Derive a directory name from the URL
    repo_name = url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
    local_path = os.path.join(cache_dir, repo_name)

    if os.path.isdir(os.path.join(local_path, ".git")):
        cmd = ["git", "-C", local_path, "fetch", "--depth=1", "origin"]
        if branch:
            cmd.append(branch)
        result = subprocess.run(cmd, capture_output=True, timeout=120)
        if result.returncode != 0:
            logger.warning(
                "git fetch failed for %s: %s",
                url, result.stderr.decode(errors="replace"),
            )
        checkout_ref = f"origin/{branch}" if branch else "FETCH_HEAD"
        result = subprocess.run(
            ["git", "-C", local_path, "checkout", "-f", checkout_ref],
            capture_output=True, timeout=30,
        )
        if result.returncode != 0:
            logger.warning(
                "git checkout failed for %s (%s): %s",
                url, checkout_ref,
                result.stderr.decode(errors="replace"),
            )
    else:
        cmd = ["git", "clone", "--depth=1", "--single-branch"]
        if branch:
            cmd.extend(["--branch", branch])
        cmd.extend([url, local_path])
        result = subprocess.run(cmd, capture_output=True, timeout=120)
        if result.returncode != 0:
            raise RuntimeError(
                f"Failed to clone {url}: {result.stderr.decode(errors='replace')}"
            )

    return local_path


def discover_observability(
    repo_path_or_url: str,
    branch: str = "",
    scan_go: bool = True,
    scan_yaml: bool = True,
    scan_json_dashboards: bool = True,
) -> ObservabilityInventory:
    """Walk a repo and collect all observability artifacts.

    Accepts either a local path or a GitHub URL. URLs are shallow-cloned
    (with caching) so the latest upstream code is always scanned.
    """
    if _is_url(repo_path_or_url):
        repo_path = fetch_repo(repo_path_or_url, branch=branch)
        label = repo_path_or_url
    else:
        repo_path = repo_path_or_url
        label = repo_path_or_url

    repo_short = _repo_name(repo_path_or_url)
    inventory = ObservabilityInventory(repo_path=label)
    repo = Path(repo_path)

    if not repo.is_dir():
        return inventory

    # First pass: collect Go panel files that need const maps from sibling dirs.
    # We also track dashboard names discovered per directory for panel context.
    dir_dashboard_names: dict[str, str] = {}
    panel_scan_queue: list[tuple[str, str, str]] = []  # (fpath, rel, dirpath)

    for dirpath, dirnames, filenames in os.walk(repo):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]

        if _should_skip(dirpath):
            continue

        for fname in filenames:
            fpath = os.path.join(dirpath, fname)
            rel = os.path.relpath(fpath, repo_path)

            if scan_go and fname.endswith(".go"):
                metrics, alerts, recording, dashboards = scan_go_file(fpath)
                for m in metrics:
                    m.file = rel
                    m.repo = repo_short
                for a in alerts:
                    a.file = rel
                    a.repo = repo_short
                for r in recording:
                    r.file = rel
                    r.repo = repo_short
                for d in dashboards:
                    d.file = rel
                    d.repo = repo_short
                inventory.metrics.extend(metrics)
                inventory.alerts.extend(alerts)
                inventory.recording_rules.extend(recording)
                inventory.dashboards.extend(dashboards)

                # Check for dashboard.Name(...) to associate panels
                try:
                    with open(fpath, encoding="utf-8", errors="replace") as f:
                        go_content = f.read()
                except OSError:
                    go_content = ""

                if go_content:
                    name_match = _GO_DASHBOARD_NAME_RE.search(go_content)
                    if name_match:
                        dir_dashboard_names[dirpath] = name_match.group(1)
                    if _GO_PANEL_TITLE_RE.search(go_content):
                        panel_scan_queue.append((fpath, rel, dirpath))

            elif scan_yaml and fname.endswith((".yaml", ".yml")):
                alerts, recordings = _scan_yaml_prometheus_rule(fpath)
                for a in alerts:
                    a.file = rel
                    a.repo = repo_short
                for r in recordings:
                    r.file = rel
                    r.repo = repo_short
                inventory.alerts.extend(alerts)
                inventory.recording_rules.extend(recordings)

                # Also check for PersesDashboard YAML
                perses_dashboards, perses_panels = _scan_perses_yaml(fpath)
                for d in perses_dashboards:
                    d.file = rel
                    d.repo = repo_short
                for p in perses_panels:
                    p.file = rel
                    p.repo = repo_short
                inventory.dashboards.extend(perses_dashboards)
                inventory.panels.extend(perses_panels)

            elif scan_json_dashboards and fname.endswith(".json"):
                json_dashboards = _scan_grafana_json(fpath)
                for d in json_dashboards:
                    d.file = rel
                    d.repo = repo_short
                inventory.dashboards.extend(json_dashboards)

    # Second pass: scan Go panel files with const maps from sibling dirs
    const_map_cache: dict[str, dict[str, str]] = {}
    for fpath, rel, dirpath in panel_scan_queue:
        if dirpath not in const_map_cache:
            const_map_cache[dirpath] = _build_const_map(dirpath)
        const_map = const_map_cache[dirpath]

        # Also check parent directories for const maps (panels and queries
        # may be in different subdirectories under the same package)
        parent = str(Path(dirpath).parent)
        if parent not in const_map_cache:
            const_map_cache[parent] = _build_const_map(parent)
        merged_map = {**const_map_cache[parent], **const_map}

        dashboard_name = dir_dashboard_names.get(dirpath, "")
        # Check parent dirs for dashboard name if not found in this dir
        if not dashboard_name:
            for d, name in dir_dashboard_names.items():
                if dirpath.startswith(d) or d.startswith(str(Path(dirpath).parent)):
                    dashboard_name = name
                    break

        try:
            with open(fpath, encoding="utf-8", errors="replace") as f:
                content = f.read()
        except OSError:
            continue

        go_panels = _scan_go_panels(content, rel, merged_map, dashboard_name)
        for p in go_panels:
            p.repo = repo_short
        inventory.panels.extend(go_panels)

    return inventory


# ---------------------------------------------------------------------------
# Multi-repo aggregation with session cache
# ---------------------------------------------------------------------------

_inventory_cache: dict[str, ObservabilityInventory] = {}


def build_all_inventories(
    cfg: dict[str, Any],
    branch: str = "",
) -> ObservabilityInventory:
    """Scan all configured repos and merge into a single inventory.

    Results are cached by branch for the lifetime of the process so that
    repeated tool calls within a session don't re-clone 8 repos.
    """
    cache_key = branch or "__default__"
    if cache_key in _inventory_cache:
        return _inventory_cache[cache_key]

    repos = cfg.get("discovery", {}).get("repos", [])
    merged = ObservabilityInventory(repo_path="all")

    for repo_url in repos:
        inv = discover_observability(repo_url, branch=branch)
        merged.metrics.extend(inv.metrics)
        merged.alerts.extend(inv.alerts)
        merged.recording_rules.extend(inv.recording_rules)
        merged.dashboards.extend(inv.dashboards)
        merged.panels.extend(inv.panels)

    cmo_url = cfg.get("telemetry", {}).get("cmo_allowlist_url", "")
    if cmo_url:
        merged.telemetry_allowlist.extend(fetch_cmo_allowlist(cmo_url))

    _inventory_cache[cache_key] = merged
    return merged


_PROMQL_METRIC_RE = re.compile(r'([a-zA-Z_:][a-zA-Z0-9_:]*)\s*[{(]')
_PROMQL_FUNCTIONS = frozenset({
    "sum", "avg", "min", "max", "count", "stddev", "stdvar",
    "rate", "irate", "increase", "delta", "idelta", "deriv",
    "histogram_quantile", "label_replace", "label_join",
    "absent", "absent_over_time", "ceil", "floor", "round",
    "sort", "sort_desc", "topk", "bottomk", "clamp", "clamp_max",
    "clamp_min", "vector", "scalar", "time", "timestamp",
    "count_values", "quantile", "group", "sgn", "changes",
    "resets", "predict_linear", "holt_winters", "exp", "ln",
    "log2", "log10", "sqrt", "last_over_time", "on", "ignoring",
})


def extract_metric_names_from_promql(expr: str) -> set[str]:
    """Extract probable Prometheus metric names from a PromQL expression."""
    candidates = _PROMQL_METRIC_RE.findall(expr)
    return {
        c for c in candidates
        if c.lower() not in _PROMQL_FUNCTIONS
        and not c.startswith("$")
    }


def is_cluster_level_rule(name: str) -> bool:
    """Check if a recording rule name indicates cluster-level aggregation."""
    return (
        name.startswith(("cluster:", "cnv:", "cnv_"))
        or ":sum" in name
        or ":count" in name
        or ":avg" in name
    )


def find_cluster_level_rules(
    inv: ObservabilityInventory,
) -> list[RecordingRuleInfo]:
    """Return recording rules that aggregate to the cluster level."""
    return [r for r in inv.recording_rules if is_cluster_level_rule(r.name)]


def find_unvisualized_metrics(inv: ObservabilityInventory) -> list[MetricInfo]:
    """Find metrics that exist in code but are not referenced in any dashboard panel."""
    visualized: set[str] = set()
    for panel in inv.panels:
        for q in panel.queries:
            visualized.update(extract_metric_names_from_promql(q))

    return [m for m in inv.metrics if m.name not in visualized]


def invalidate_cache(branch: str = "") -> None:
    """Clear the inventory cache (useful for tests or forced refresh)."""
    key = branch or "__default__"
    _inventory_cache.pop(key, None)


def format_inventory(inv: ObservabilityInventory) -> str:
    """Render inventory as a human-readable markdown report."""
    lines: list[str] = []
    lines.append(f"# Observability Inventory: {inv.repo_path}")
    lines.append("")
    lines.append(f"**Summary:** {inv.summary()}")
    lines.append("")

    if inv.metrics:
        lines.append("## Prometheus Metrics")
        lines.append("")
        lines.append("| Name | Type | Help | File |")
        lines.append("|------|------|------|------|")
        for m in sorted(inv.metrics, key=lambda x: x.name):
            help_short = m.help[:80] + "..." if len(m.help) > 80 else m.help
            lines.append(f"| `{m.name}` | {m.metric_type} | {help_short} | {m.file}:{m.line} |")
        lines.append("")

    if inv.alerts:
        lines.append("## Alerting Rules")
        lines.append("")
        lines.append("| Alert | Severity | Source | File |")
        lines.append("|-------|----------|--------|------|")
        for a in sorted(inv.alerts, key=lambda x: x.name):
            lines.append(f"| `{a.name}` | {a.severity} | {a.source_format} | {a.file}:{a.line} |")
        lines.append("")

    if inv.recording_rules:
        lines.append("## Recording Rules")
        lines.append("")
        lines.append("| Name | Source | File |")
        lines.append("|------|--------|------|")
        for r in sorted(inv.recording_rules, key=lambda x: x.name):
            lines.append(f"| `{r.name}` | {r.source_format} | {r.file}:{r.line} |")
        lines.append("")

    if inv.dashboards:
        lines.append("## Dashboards")
        lines.append("")
        lines.append("| Name | Type | File |")
        lines.append("|------|------|------|")
        for d in sorted(inv.dashboards, key=lambda x: x.name):
            lines.append(f"| {d.name} | {d.dashboard_type} | {d.file} |")
        lines.append("")

    if inv.panels:
        lines.append("## Dashboard Panels")
        lines.append("")
        lines.append("| Panel | Dashboard | Queries | File |")
        lines.append("|-------|-----------|---------|------|")
        for p in sorted(inv.panels, key=lambda x: (x.dashboard, x.name)):
            q_count = len(p.queries)
            lines.append(f"| {p.name} | {p.dashboard} | {q_count} | {p.file} |")
        lines.append("")

    if inv.telemetry_allowlist:
        lines.append("## Telemetry Allowlist")
        lines.append("")
        lines.append("| Metric | Match Expression | Repo | File |")
        lines.append("|--------|-----------------|------|------|")
        for t in sorted(inv.telemetry_allowlist, key=lambda x: x.metric_name):
            lines.append(
                f"| `{t.metric_name}` | `{t.match_expr}` | {t.repo} | {t.file} |"
            )
        lines.append("")

    if not any([inv.metrics, inv.alerts, inv.recording_rules, inv.dashboards,
                inv.panels, inv.telemetry_allowlist]):
        lines.append("No observability artifacts found.")

    return "\n".join(lines)

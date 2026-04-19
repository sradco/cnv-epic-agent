"""GitHub/code-discovery MCP tools: discover_repo_observability,
list_metrics, list_alerts, list_dashboards, search_observability,
suggest_telemetry, list_telemetry."""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


def register_github_tools(server: Any) -> None:
    """Register code-discovery tools on the FastMCP server."""

    @server.tool()
    async def discover_repo_observability(
        repo: str,
        branch: str = "",
        scan_go: bool = True,
        scan_yaml: bool = True,
        scan_json_dashboards: bool = True,
    ) -> str:
        """Scan a repo for existing observability artifacts.

        Accepts a GitHub URL or a local path. URLs are shallow-cloned
        with caching so the latest upstream code is always scanned.

        Finds Prometheus metrics, alerting/recording rules, and dashboards
        by parsing Go source files and YAML PrometheusRule manifests.

        Parameters:
        - repo: GitHub URL or local path to the repo
        - branch: git branch to scan (default: repo default branch)
        - scan_go: scan Go source files (default: True)
        - scan_yaml: scan YAML files (default: True)
        - scan_json_dashboards: scan JSON files (default: True)
        """
        from mcp.github.discover import (
            discover_observability,
            format_inventory,
        )

        inv = discover_observability(
            repo,
            branch=branch,
            scan_go=scan_go,
            scan_yaml=scan_yaml,
            scan_json_dashboards=scan_json_dashboards,
        )
        return format_inventory(inv)

    @server.tool()
    async def list_metrics(
        repo: str = "",
        metric_type: str = "",
        branch: str = "",
    ) -> str:
        """List all Prometheus metrics across all CNV repos.

        Parameters:
        - repo: filter by repo name (substring match)
        - metric_type: filter by type (counter, gauge, histogram, summary)
        - branch: git branch to scan (default: repo default branch)
        """
        from mcp.server import load_config
        from mcp.github.discover import build_all_inventories

        cfg = load_config()
        inv = build_all_inventories(cfg, branch=branch)

        metrics = inv.metrics
        if repo:
            repo_lower = repo.lower()
            metrics = [
                m for m in metrics if repo_lower in m.repo.lower()
            ]
        if metric_type:
            type_lower = metric_type.lower()
            metrics = [
                m for m in metrics if m.metric_type == type_lower
            ]

        lines: list[str] = []
        lines.append(f"# CNV Metrics ({len(metrics)} found)")
        lines.append("")
        lines.append(f"**Total across all repos:** {inv.summary()}")
        if repo:
            lines.append(f"**Filtered by repo:** {repo}")
        if metric_type:
            lines.append(f"**Filtered by type:** {metric_type}")
        lines.append("")

        if not metrics:
            lines.append("No metrics found matching the filters.")
            return "\n".join(lines)

        lines.append("| Name | Type | Help | Repo | File |")
        lines.append("|------|------|------|------|------|")
        for m in sorted(metrics, key=lambda x: (x.repo, x.name)):
            help_short = (
                m.help[:60] + "..." if len(m.help) > 60 else m.help
            )
            lines.append(
                f"| `{m.name}` | {m.metric_type} | {help_short} "
                f"| {m.repo} | {m.file}:{m.line} |"
            )

        return "\n".join(lines)

    @server.tool()
    async def list_alerts(
        repo: str = "",
        severity: str = "",
        branch: str = "",
    ) -> str:
        """List all alerting rules across all CNV repos.

        Parameters:
        - repo: filter by repo name (substring match)
        - severity: filter by severity (critical, warning, info)
        - branch: git branch to scan (default: repo default branch)
        """
        from mcp.server import load_config
        from mcp.github.discover import build_all_inventories

        cfg = load_config()
        inv = build_all_inventories(cfg, branch=branch)

        alerts = inv.alerts
        if repo:
            repo_lower = repo.lower()
            alerts = [
                a for a in alerts if repo_lower in a.repo.lower()
            ]
        if severity:
            sev_lower = severity.lower()
            alerts = [
                a for a in alerts if a.severity == sev_lower
            ]

        lines: list[str] = []
        lines.append(f"# CNV Alerts ({len(alerts)} found)")
        lines.append("")
        lines.append(f"**Total across all repos:** {inv.summary()}")
        if repo:
            lines.append(f"**Filtered by repo:** {repo}")
        if severity:
            lines.append(f"**Filtered by severity:** {severity}")
        lines.append("")

        if not alerts:
            lines.append("No alerts found matching the filters.")
            return "\n".join(lines)

        lines.append(
            "| Alert | Severity | Expr | Source | Repo | File |"
        )
        lines.append(
            "|-------|----------|------|--------|------|------|"
        )
        for a in sorted(alerts, key=lambda x: (x.repo, x.name)):
            expr_short = (
                a.expr[:50] + "..." if len(a.expr) > 50 else a.expr
            )
            lines.append(
                f"| `{a.name}` | {a.severity} | `{expr_short}` "
                f"| {a.source_format} | {a.repo} "
                f"| {a.file}:{a.line} |"
            )

        return "\n".join(lines)

    @server.tool()
    async def search_observability(
        pattern: str,
        kind: str = "",
        branch: str = "",
    ) -> str:
        """Search metrics, alerts, recording rules, dashboards by name.

        Parameters:
        - pattern: search string or glob with * wildcards
        - kind: filter to a specific kind (metrics, alerts,
                recording_rules, dashboards, panels)
        - branch: git branch to scan (default: repo default branch)
        """
        import fnmatch

        from mcp.server import load_config
        from mcp.github.discover import build_all_inventories

        cfg = load_config()
        inv = build_all_inventories(cfg, branch=branch)

        pat_lower = pattern.lower()
        use_glob = "*" in pattern or "?" in pattern

        def _matches(name: str) -> bool:
            if use_glob:
                return fnmatch.fnmatch(name.lower(), pat_lower)
            return pat_lower in name.lower()

        lines: list[str] = []
        lines.append(f'# Search results for "{pattern}"')
        lines.append("")
        lines.append(
            f"**Total across all repos:** {inv.summary()}"
        )
        lines.append("")

        valid_kinds = (
            "metrics", "alerts", "recording_rules",
            "dashboards", "panels",
        )
        if kind and kind not in valid_kinds:
            return (
                f"Unknown kind '{kind}'. "
                f"Valid kinds: {', '.join(valid_kinds)}"
            )

        kinds_to_search = [kind] if kind else list(valid_kinds)
        total_matches = 0

        if "metrics" in kinds_to_search:
            matched = [m for m in inv.metrics if _matches(m.name)]
            total_matches += len(matched)
            if matched:
                lines.append(f"## Metrics ({len(matched)})")
                lines.append("")
                lines.append("| Name | Type | Help | Repo | File |")
                lines.append("|------|------|------|------|------|")
                for m in sorted(matched, key=lambda x: x.name):
                    help_short = (
                        m.help[:60] + "..."
                        if len(m.help) > 60 else m.help
                    )
                    lines.append(
                        f"| `{m.name}` | {m.metric_type} "
                        f"| {help_short} | {m.repo} "
                        f"| {m.file}:{m.line} |"
                    )
                lines.append("")

        if "alerts" in kinds_to_search:
            matched = [a for a in inv.alerts if _matches(a.name)]
            total_matches += len(matched)
            if matched:
                lines.append(f"## Alerts ({len(matched)})")
                lines.append("")
                lines.append(
                    "| Alert | Severity | Expr | Repo | File |"
                )
                lines.append(
                    "|-------|----------|------|------|------|"
                )
                for a in sorted(matched, key=lambda x: x.name):
                    expr_short = (
                        a.expr[:50] + "..."
                        if len(a.expr) > 50 else a.expr
                    )
                    lines.append(
                        f"| `{a.name}` | {a.severity} "
                        f"| `{expr_short}` | {a.repo} "
                        f"| {a.file}:{a.line} |"
                    )
                lines.append("")

        if "recording_rules" in kinds_to_search:
            matched = [
                r for r in inv.recording_rules if _matches(r.name)
            ]
            total_matches += len(matched)
            if matched:
                lines.append(
                    f"## Recording Rules ({len(matched)})"
                )
                lines.append("")
                lines.append("| Name | Expr | Repo | File |")
                lines.append("|------|------|------|------|")
                for r in sorted(matched, key=lambda x: x.name):
                    expr_short = (
                        r.expr[:50] + "..."
                        if len(r.expr) > 50 else r.expr
                    )
                    lines.append(
                        f"| `{r.name}` | `{expr_short}` "
                        f"| {r.repo} | {r.file}:{r.line} |"
                    )
                lines.append("")

        if "dashboards" in kinds_to_search:
            matched = [
                d for d in inv.dashboards if _matches(d.name)
            ]
            total_matches += len(matched)
            if matched:
                lines.append(f"## Dashboards ({len(matched)})")
                lines.append("")
                lines.append("| Name | Type | Repo | File |")
                lines.append("|------|------|------|------|")
                for d in sorted(matched, key=lambda x: x.name):
                    lines.append(
                        f"| {d.name} | {d.dashboard_type} "
                        f"| {d.repo} | {d.file} |"
                    )
                lines.append("")

        if "panels" in kinds_to_search:
            matched_panels = [
                p for p in inv.panels if _matches(p.name)
            ]
            total_matches += len(matched_panels)
            if matched_panels:
                lines.append(f"## Panels ({len(matched_panels)})")
                lines.append("")
                lines.append(
                    "| Panel | Dashboard | Queries | Repo | File |"
                )
                lines.append(
                    "|-------|-----------|---------|------|------|"
                )
                for p in sorted(matched_panels, key=lambda x: x.name):
                    lines.append(
                        f"| {p.name} | {p.dashboard} "
                        f"| {len(p.queries)} | {p.repo} "
                        f"| {p.file} |"
                    )
                lines.append("")

        if total_matches == 0:
            lines.append(f'No matches found for "{pattern}".')

        return "\n".join(lines)

    @server.tool()
    async def list_dashboards(
        repo: str = "",
        scope: str = "",
        branch: str = "",
    ) -> str:
        """List all dashboards and their panels across all CNV repos.

        Parameters:
        - repo: filter by repo name (substring match)
        - scope: filter by type (perses-go, perses-yaml, grafana-json)
        - branch: git branch to scan (default: repo default branch)
        """
        from mcp.server import load_config
        from mcp.github.discover import (
            build_all_inventories,
            extract_metric_names_from_promql,
            find_unvisualized_metrics,
        )

        cfg = load_config()
        inv = build_all_inventories(cfg, branch=branch)

        dashboards = inv.dashboards
        panels = inv.panels

        if repo:
            repo_lower = repo.lower()
            dashboards = [
                d for d in dashboards
                if repo_lower in d.repo.lower()
            ]
            panels = [
                p for p in panels if repo_lower in p.repo.lower()
            ]
        if scope:
            scope_lower = scope.lower()
            dashboards = [
                d for d in dashboards
                if d.dashboard_type == scope_lower
            ]

        lines: list[str] = []
        lines.append(
            f"# CNV Dashboards ({len(dashboards)} dashboards, "
            f"{len(panels)} panels)"
        )
        lines.append("")

        if not dashboards and not panels:
            lines.append(
                "No dashboards found matching the filters."
            )
            return "\n".join(lines)

        if dashboards:
            lines.append("## Dashboards")
            lines.append("")
            lines.append("| Name | Type | Repo | File |")
            lines.append("|------|------|------|------|")
            for d in sorted(
                dashboards, key=lambda x: (x.repo, x.name),
            ):
                lines.append(
                    f"| {d.name} | {d.dashboard_type} "
                    f"| {d.repo} | {d.file} |"
                )
            lines.append("")

        if panels:
            lines.append("## Panels")
            lines.append("")
            for p in sorted(
                panels, key=lambda x: (x.dashboard, x.name),
            ):
                lines.append(f"### {p.name}")
                lines.append(f"- **Dashboard:** {p.dashboard}")
                lines.append(f"- **Repo:** {p.repo}")
                lines.append(f"- **File:** {p.file}")
                if p.queries:
                    lines.append(
                        f"- **Queries ({len(p.queries)}):**"
                    )
                    for q in p.queries:
                        q_short = (
                            q[:120] + "..."
                            if len(q) > 120 else q
                        )
                        lines.append(f"  - `{q_short}`")
                    metrics_in_panel: set[str] = set()
                    for q in p.queries:
                        metrics_in_panel.update(
                            extract_metric_names_from_promql(q)
                        )
                    if metrics_in_panel:
                        lines.append(
                            "- **Metrics referenced:** "
                            f"{', '.join(sorted(metrics_in_panel))}"
                        )
                else:
                    lines.append(
                        "- **Queries:** *(PromQL not resolved)*"
                    )
                lines.append("")

        unviz = find_unvisualized_metrics(inv)
        if unviz:
            lines.append(
                f"## Unvisualized Metrics "
                f"({len(unviz)} metrics without panel coverage)"
            )
            lines.append("")
            lines.append("| Metric | Type | Repo |")
            lines.append("|--------|------|------|")
            for m in sorted(
                unviz, key=lambda x: (x.repo, x.name),
            ):
                lines.append(
                    f"| `{m.name}` | {m.metric_type} | {m.repo} |"
                )
            lines.append("")

        return "\n".join(lines)

    @server.tool()
    async def suggest_telemetry(branch: str = "") -> str:
        """Suggest cluster-level recording rules not yet on the CMO
        telemetry allowlist.

        Parameters:
        - branch: git branch to scan (default: repo default branch)
        """
        from agent.analyzer.analysis import (
            suggest_telemetry as _suggest,
        )
        from mcp.server import load_config
        from mcp.github.discover import build_all_inventories

        cfg = load_config()
        inv = build_all_inventories(cfg, branch=branch)
        candidates = _suggest(inventory=inv)

        allowlist_entries = inv.telemetry_allowlist

        lines: list[str] = []
        lines.append("# Telemetry Suggestions")
        lines.append("")

        lines.append(
            f"## Current CMO allowlist "
            f"({len(allowlist_entries)} entries)"
        )
        lines.append("")
        for t in sorted(
            allowlist_entries, key=lambda x: x.metric_name,
        ):
            lines.append(
                f"- `{t.metric_name}` — `{t.match_expr}`"
            )
        if not allowlist_entries:
            lines.append(
                "_No CMO telemetry ConfigMap found. "
                "Ensure telemetry.cmo_allowlist_url is set._"
            )
        lines.append("")

        if not candidates:
            lines.append(
                "All cluster-level recording rules are already on "
                "the allowlist (or none were discovered)."
            )
            return "\n".join(lines)

        lines.append(
            f"## Candidates for allowlist "
            f"({len(candidates)} found)"
        )
        lines.append("")
        lines.append(
            "| Name | PromQL | Why collect | Repo | File |"
        )
        lines.append(
            "|------|--------|------------|------|------|"
        )
        for s in candidates:
            expr_short = s["expr"][:60] + (
                "..." if len(s["expr"]) > 60 else ""
            )
            rationale = s.get("rationale", "")
            lines.append(
                f"| `{s['name']}` | `{expr_short}` "
                f"| {rationale} "
                f"| {s.get('repo', '')} | {s.get('file', '')} |"
            )
        lines.append("")

        lines.append("## CMO metrics.yaml entries")
        lines.append("")
        lines.append(
            "Add these to the CMO `metrics.yaml` allowlist:"
        )
        lines.append("")
        lines.append("```yaml")
        lines.append("matches:")
        for s in candidates:
            lines.append(
                f'  - \'{{__name__="{s["name"]}"}}\''
            )
        lines.append("```")

        return "\n".join(lines)

    @server.tool()
    async def list_telemetry(branch: str = "") -> str:
        """Show current telemetry allowlist and candidate recording
        rules.

        Parameters:
        - branch: git branch to scan (default: repo default branch)
        """
        from mcp.server import load_config
        from mcp.github.discover import (
            build_all_inventories,
            find_cluster_level_rules,
        )

        cfg = load_config()
        inv = build_all_inventories(cfg, branch=branch)
        allowlist_names = {
            t.metric_name for t in inv.telemetry_allowlist
        }
        cluster_level = find_cluster_level_rules(inv)

        lines: list[str] = []
        lines.append("# Telemetry Status")
        lines.append("")
        lines.append(
            f"## Current allowlist "
            f"({len(inv.telemetry_allowlist)} entries from CMO)"
        )
        lines.append("")
        for t in sorted(
            inv.telemetry_allowlist,
            key=lambda x: x.metric_name,
        ):
            lines.append(
                f"- `{t.metric_name}` — `{t.match_expr}`"
            )
        if not inv.telemetry_allowlist:
            lines.append(
                "_No CMO telemetry ConfigMap found. "
                "Ensure telemetry.cmo_allowlist_url is set._"
            )
        lines.append("")

        lines.append(
            f"## Cluster-level recording rules "
            f"({len(cluster_level)} found)"
        )
        lines.append("")
        if cluster_level:
            lines.append(
                "| Name | On Allowlist | Repo | File |"
            )
            lines.append(
                "|------|-------------|------|------|"
            )
            for r in sorted(
                cluster_level, key=lambda x: x.name,
            ):
                on_list = (
                    "YES" if r.name in allowlist_names else "no"
                )
                lines.append(
                    f"| `{r.name}` | {on_list} | {r.repo} "
                    f"| {r.file}:{r.line} |"
                )
        else:
            lines.append(
                "No cluster-level recording rules found."
            )
        lines.append("")

        candidates = [
            r for r in cluster_level
            if r.name not in allowlist_names
        ]
        if candidates:
            lines.append(
                f"## Candidates for allowlist "
                f"({len(candidates)})"
            )
            lines.append("")
            for r in sorted(candidates, key=lambda x: x.name):
                lines.append(
                    f"- `{r.name}` — {r.repo} ({r.file})"
                )
            lines.append("")
            lines.append("```yaml")
            lines.append("matches:")
            for r in sorted(candidates, key=lambda x: x.name):
                lines.append(
                    f'  - \'{{__name__="{r.name}"}}\''
                )
            lines.append("```")

        return "\n".join(lines)

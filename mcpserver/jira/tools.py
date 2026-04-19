"""Jira MCP tools: scan_epics, analyze_epic, get_analysis_data,
create_stories, create_story."""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


def register_jira_tools(server: Any) -> None:
    """Register Jira-related tools on the FastMCP server."""

    @server.tool()
    async def scan_epics(
        project: Optional[str] = None,
        since_days: Optional[int] = None,
        jql: Optional[str] = None,
    ) -> str:
        """Scan CNV epics for observability gaps.

        Finds recent epics, reads each epic + stories to decide if monitoring
        is needed, then checks for metrics/alerts/dashboards coverage.

        Returns a markdown summary table showing gaps.

        Parameters:
        - project: Jira project key (default: CNV)
        - since_days: how far back to look (default: 30)
        - jql: optional raw JQL override
        """
        from mcpserver.server import load_config, get_inventory

        from agent.analyzer.analysis import build_analysis_result
        from agent.analyzer.formatter import format_scan_table
        from mcpserver.jira.client import (
            get_jira_client,
            search_epics as _search_epics,
            fetch_epic_with_children,
        )

        cfg = load_config()
        client = get_jira_client(cfg)
        epics = _search_epics(
            client, cfg, project=project, since_days=since_days, jql=jql,
        )

        if not epics:
            return "No epics found matching the query."

        inv = get_inventory(cfg)

        results: list[dict[str, Any]] = []
        for epic_issue in epics:
            epic, children = fetch_epic_with_children(
                client, cfg, epic_issue.key,
            )
            result = build_analysis_result(
                epic, children, cfg, inventory=inv,
            )
            results.append(result)

        table = format_scan_table(results)
        needed_count = sum(
            1 for r in results if r["need_state"] == "needed"
        )
        gap_count = sum(1 for r in results if r.get("gaps"))

        summary = (
            f"Scanned **{len(results)}** epics. "
            f"**{needed_count}** need monitoring. "
            f"**{gap_count}** have observability gaps.\n\n"
        )
        return summary + table

    @server.tool()
    async def analyze_epic(epic_key: str) -> str:
        """Deep-dive analysis of a single epic for observability needs.

        Reads the epic and its child stories/tasks, then:
        1. Decides if monitoring is needed (with evidence)
        2. Checks existing coverage for metrics, alerts, dashboards
        3. Proposes concrete additions for any gaps

        Returns a detailed human-readable report.

        Parameters:
        - epic_key: the Jira epic key (e.g. CNV-12345)
        """
        from mcpserver.server import load_config, get_inventory

        from agent.analyzer.analysis import build_analysis_result
        from agent.analyzer.formatter import format_analysis_result
        from mcpserver.jira.client import get_jira_client, fetch_epic_with_children

        cfg = load_config()
        client = get_jira_client(cfg)
        epic, children = fetch_epic_with_children(client, cfg, epic_key)
        inv = get_inventory(cfg)
        result = build_analysis_result(epic, children, cfg, inventory=inv)
        return format_analysis_result(result)

    @server.tool()
    async def get_analysis_data(epic_key: str) -> str:
        """Get structured analysis data for an epic as JSON.

        Returns the same analysis as analyze_epic but as raw JSON instead
        of markdown.  Designed for AI-assisted workflows where the client
        (LLM) reasons over the evidence to produce epic-specific rationale
        before creating stories via create_story.

        The JSON includes:
        - epic_key, epic_summary, epic_description (full text)
        - child_issues with summaries and descriptions
        - domain_keywords extracted from the epic content
        - need assessment with evidence
        - coverage gaps
        - existing related artifacts (name, type, repo, file, PromQL)
        - pattern-based proposed items (scaffolding for LLM refinement)
        - telemetry candidates
        - dashboard targets

        Parameters:
        - epic_key: the Jira epic key (e.g. CNV-12345)
        """
        from mcpserver.server import load_config, get_inventory

        from agent.analyzer.analysis import build_analysis_result
        from mcpserver.jira.client import get_jira_client, fetch_epic_with_children

        cfg = load_config()
        client = get_jira_client(cfg)
        epic, children = fetch_epic_with_children(client, cfg, epic_key)
        inv = get_inventory(cfg)
        result = build_analysis_result(epic, children, cfg, inventory=inv)
        return json.dumps(result, indent=2, default=str)

    @server.tool()
    async def create_stories(
        epic_key: str,
        version: str = "",
        categories: Optional[list[str]] = None,
        dry_run: bool = True,
        detail: str = "summary",
    ) -> str:
        """Create observability stories on Jira for an epic.

        Stories are placed under a version-scoped observability epic
        (one per CNV release) and linked back to the source feature epic.

        Default mode is dry-run (shows what would be created without
        creating). Set dry_run=False to actually create the stories.

        Idempotent: skips stories that already exist for this source epic.

        Parameters:
        - epic_key: the source Jira epic key (e.g. CNV-12345)
        - version: CNV version (e.g. "4.18"). Required.
        - categories: list of categories to create (default: all missing)
        - dry_run: if True (default), preview only; if False, create
        - detail: "summary" (default) or "full" for complete descriptions
        """
        if not version:
            return (
                "Parameter 'version' is required (e.g. version=\"4.18\")."
            )

        if detail not in ("summary", "full"):
            return "Parameter 'detail' must be \"summary\" or \"full\"."

        from mcpserver.server import load_config, get_inventory

        from agent.analyzer.analysis import build_analysis_result
        from agent.analyzer.formatter import build_subtask_payloads
        from mcpserver.jira.client import (
            get_jira_client,
            fetch_epic_with_children,
            find_existing_obs_stories,
            find_or_create_obs_epic,
            create_obs_story,
            is_duplicate_story,
        )

        cfg = load_config()
        client = get_jira_client(cfg)
        epic, children = fetch_epic_with_children(client, cfg, epic_key)
        inv = get_inventory(cfg)
        result = build_analysis_result(
            epic, children, cfg, inventory=inv,
        )

        if not result.get("apply_allowed"):
            if dry_run:
                if result["need_state"] == "not_needed":
                    return (
                        f"Monitoring is not needed for {epic_key}. "
                        "No stories to create."
                    )
                if result["need_state"] == "uncertain":
                    return (
                        f"Monitoring need for {epic_key} is uncertain "
                        f"(confidence: {result['need_confidence']}). "
                        "Manual review recommended before creating stories."
                    )
                return f"No observability gaps found for {epic_key}."
            return (
                f"Cannot create stories for {epic_key}: "
                f"need_state={result['need_state']}, "
                f"gaps={result.get('gaps', [])}"
            )

        if categories:
            result["gaps"] = [
                g for g in result["gaps"] if g in categories
            ]
            if not result["gaps"]:
                return (
                    "No gaps found for the specified categories: "
                    f"{categories}"
                )

        obs_epic = find_or_create_obs_epic(
            client, cfg, version, dry_run=dry_run,
        )
        payloads = build_subtask_payloads(result, cfg)

        existing = find_existing_obs_stories(
            client, cfg, obs_epic["key"], epic_key,
        )
        for child in children:
            existing.append({
                "key": child.key,
                "summary": child.summary,
                "labels": [],
                "description": child.description,
            })

        created: list[str] = []
        skipped: list[str] = []
        would_create: list[dict[str, str]] = []

        for payload in payloads:
            if is_duplicate_story(
                payload["summary"], epic_key, existing,
            ):
                skipped.append(payload["summary"])
                continue

            if dry_run:
                would_create.append(payload)
            else:
                issue, warnings = create_obs_story(
                    client, cfg, obs_epic["key"], epic_key,
                    payload["summary"], payload["description"],
                    story_points=payload.get("story_points"),
                    category=payload.get("category", ""),
                )
                warn_tag = (
                    f" ⚠ {warnings.warning_text()}"
                    if warnings.has_warnings else ""
                )
                created.append(
                    f"{issue.key}: {payload['summary']}{warn_tag}"
                )

        lines: list[str] = []
        if dry_run:
            creation_cfg = cfg.get("creation", {})
            story_label = creation_cfg.get(
                "story_label", "epic-agent-generated",
            )
            epic_label = creation_cfg.get(
                "epic_label", "cnv-observability",
            )

            lines.append(f"## Dry-Run Results for {epic_key}")
            lines.append("")
            lines.append(
                f"**Source epic:** {epic_key} — "
                f"{result['epic_summary']}"
            )
            lines.append(
                f"**Target observability epic:** "
                f"{obs_epic['key']} — {obs_epic['summary']}"
            )
            lines.append(
                f"**Labels:** `{epic_label}`, `{story_label}`"
            )
            lines.append(
                "**Component:** "
                f"{creation_cfg.get('component', 'CNV Install, Upgrade and Operators')}"
            )
            lines.append(f"**Each story linked to:** {epic_key}")
            lines.append("")
            lines.append(
                f"- **Would create:** {len(would_create)} stories"
            )
            lines.append(
                f"- **Would skip (existing):** {len(skipped)}"
            )
            lines.append("")

            if existing:
                lines.append(
                    "### Existing observability stories "
                    "(will be skipped)"
                )
                lines.append("")
                for e in existing:
                    lines.append(f"- {e['key']}: {e['summary']}")
                lines.append("")

            if detail == "full":
                for i, payload in enumerate(would_create, 1):
                    lines.append(
                        f"### Story {i}: {payload['category']}"
                    )
                    lines.append("")
                    lines.append(f"**Summary:** {payload['summary']}")
                    lines.append("")
                    lines.append("**Description:**")
                    lines.append("")
                    lines.append(payload["description"])
                    lines.append("")
                    lines.append("---")
                    lines.append("")
            else:
                if would_create:
                    lines.append("### Would create:")
                    for p in would_create:
                        lines.append(f"- {p['summary']}")
                    lines.append("")

            if skipped:
                lines.append("### Would skip:")
                for s in skipped:
                    lines.append(f"- {s}")
                lines.append("")

            lines.append(
                f'To apply: `create_stories(epic_key="{epic_key}", '
                f'version="{version}", dry_run=False)`'
            )
            if detail == "summary":
                lines.append(
                    f'For full descriptions: '
                    f'`create_stories(epic_key="{epic_key}", '
                    f'version="{version}", detail="full")`'
                )
        else:
            lines.append(
                f"## Created Observability Stories for {epic_key}"
            )
            lines.append(
                f"**Observability epic:** {obs_epic['key']} — "
                f"{obs_epic['summary']}"
            )
            if obs_epic.get("created"):
                lines.append(
                    "*(Observability epic was just created)*"
                )
            lines.append("")
            lines.append(f"- **Created:** {len(created)}")
            lines.append(
                f"- **Skipped (existing):** {len(skipped)}"
            )
            lines.append("")
            if created:
                lines.append("### Created:")
                for c in created:
                    lines.append(f"- {c}")
            if skipped:
                lines.append("")
                lines.append("### Skipped:")
                for s in skipped:
                    lines.append(f"- {s}")

        return "\n".join(lines)

    @server.tool()
    async def create_story(
        epic_key: str,
        version: str,
        summary: str,
        description: str,
        category: str = "",
        dry_run: bool = True,
    ) -> str:
        """Create a single observability story with client-provided content.

        Use this in the AI-assisted workflow: after calling
        get_analysis_data to get structured evidence, the AI client writes
        the story summary and description (with LLM reasoning), then calls
        this tool to file it.

        The story is placed under the version-scoped observability epic and
        linked to the source feature epic, just like create_stories.

        Idempotent: skips if a story with the same summary already exists.

        Parameters:
        - epic_key: the source feature epic key (e.g. CNV-12345)
        - version: CNV version (e.g. "4.18")
        - summary: story title (composed by the AI client)
        - description: full story body in markdown
        - category: story category (e.g. "metrics", "alerts")
        - dry_run: if True (default), preview; if False, create on Jira
        """
        if not version:
            return (
                "Parameter 'version' is required "
                "(e.g. version=\"4.18\")."
            )
        if not summary:
            return "Parameter 'summary' is required."
        if not description:
            return "Parameter 'description' is required."

        from mcpserver.server import load_config

        from mcpserver.jira.client import (
            get_jira_client,
            fetch_child_issues,
            find_existing_obs_stories,
            find_or_create_obs_epic,
            create_obs_story,
            is_duplicate_story,
        )

        cfg = load_config()
        client = get_jira_client(cfg)

        obs_epic = find_or_create_obs_epic(
            client, cfg, version, dry_run=dry_run,
        )

        existing = find_existing_obs_stories(
            client, cfg, obs_epic["key"], epic_key,
        )
        for child in fetch_child_issues(client, cfg, epic_key):
            existing.append({
                "key": child.key,
                "summary": str(
                    getattr(child.fields, "summary", "") or ""
                ),
                "labels": [],
                "description": str(
                    getattr(child.fields, "description", "") or ""
                ),
            })

        if is_duplicate_story(summary, epic_key, existing):
            return (
                "Story already exists: a story with summary matching "
                f'"{summary}" was found under {obs_epic["key"]}. '
                "Skipped."
            )

        if dry_run:
            lines: list[str] = [
                f"## Dry-Run: create_story for {epic_key}",
                "",
                f"**Target observability epic:** "
                f"{obs_epic['key']} — {obs_epic['summary']}",
                f"**Linked to:** {epic_key}",
                "",
                f"**Summary:** {summary}",
                "",
                "**Description:**",
                "",
                description,
            ]
            return "\n".join(lines)

        issue, warnings = create_obs_story(
            client, cfg, obs_epic["key"], epic_key,
            summary, description,
            category=category,
        )
        warn_msg = ""
        if warnings.has_warnings:
            warn_msg = f"\n\n⚠ {warnings.warning_text()}"
        return (
            f"Created **{issue.key}**: {summary}\n\n"
            f"Under observability epic {obs_epic['key']}. "
            f"Linked to {epic_key}.{warn_msg}"
        )

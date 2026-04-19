"""Orchestrator: load config -> discover -> analyze -> plan -> apply.

Connects the analyzer, planner, and Jira client into a single pipeline
that can be driven by the CLI or a CI/cron job.  Supports pluggable
story categories (observability, docs, QE) with LLM-estimated story points.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

import yaml

from agent.analyzer.analysis import IssueDoc, build_analysis_result
from agent.analyzer.formatter import (
    build_subtask_payloads,
    format_analysis_result,
)
from agent.planner.planner import compose_stories, estimate_story_points
from mcp.github.discover import build_all_inventories
from mcp.jira.client import (
    create_obs_story,
    fetch_child_issues,
    fetch_epic_with_children,
    fetch_unsized_stories,
    find_existing_obs_stories,
    find_or_create_obs_epic,
    get_jira_client,
    search_epics,
    update_story_points,
)
from schemas.stories import StoryPayload

logger = logging.getLogger(__name__)

CONFIG_PATH = os.path.join(_project_root, "config.yaml")


def _load_config() -> dict[str, Any]:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def run(
    epic_keys: list[str] | None = None,
    version: str = "",
    since_days: int | None = None,
    apply: bool = False,
    model: str = "",
    use_llm: bool = True,
    categories: list[str] | None = None,
) -> str:
    """Run the full epic agent pipeline.

    Parameters:
    - epic_keys: specific epic keys to process (None = scan via JQL)
    - version: CNV version for the observability epic
    - since_days: how far back to scan (overrides config)
    - apply: if True, create stories on Jira; if False, dry-run
    - model: LLM model string (overrides config)
    - use_llm: if True, use LLM for story composition; if False,
               use template-based stories
    - categories: category list override (None = use config)
    """
    cfg = _load_config()
    agent_cfg = cfg.get("agent", {})

    if not model:
        model = os.environ.get(
            "LLM_MODEL",
            agent_cfg.get("default_model", "gpt-4o"),
        )

    max_stories = int(agent_cfg.get("max_stories_per_run", 50))

    enabled_categories: list[str] = categories or agent_cfg.get(
        "enabled_categories",
        ["metrics", "alerts", "dashboards", "telemetry"],
    )
    category_guidance: dict[str, Any] = agent_cfg.get(
        "category_guidance", {},
    )
    sp_cfg = agent_cfg.get("story_points", {})
    sp_enabled = sp_cfg.get("enabled", False)
    story_points_guidance: str = (
        sp_cfg.get("guidance", "") if sp_enabled else ""
    )
    estimate_existing_sp: bool = (
        sp_enabled and sp_cfg.get("estimate_existing", False)
    )

    client = get_jira_client(cfg)

    logger.info("Building observability inventory...")
    inv = build_all_inventories(cfg)

    if epic_keys:
        epics_to_process: list[Any] = []
        for key in epic_keys:
            try:
                epic_issue = client.issue(key)
                epics_to_process.append(epic_issue)
            except Exception:
                logger.error("Failed to fetch epic %s", key, exc_info=True)
    else:
        kwargs: dict[str, Any] = {}
        if since_days:
            kwargs["since_days"] = since_days
        epics_to_process = search_epics(client, cfg, **kwargs)

    if not epics_to_process:
        return "No epics found to process."

    report_lines: list[str] = []
    report_lines.append(
        f"# Epic Agent Run "
        f"({'APPLY' if apply else 'DRY-RUN'})"
    )
    report_lines.append("")
    report_lines.append(
        f"- **Epics:** {len(epics_to_process)}"
    )
    report_lines.append(f"- **Version:** {version or '(not set)'}")
    report_lines.append(f"- **Model:** {model}")
    report_lines.append(
        f"- **Mode:** {'LLM-assisted' if use_llm else 'template-based'}"
    )
    report_lines.append(
        f"- **Categories:** {', '.join(enabled_categories)}"
    )
    report_lines.append("")

    total_created = 0
    total_skipped = 0
    total_sp_updated = 0
    total_sp_skipped = 0

    for epic_issue in epics_to_process:
        epic_key = epic_issue.key
        logger.info("Processing epic %s...", epic_key)

        try:
            epic, children = fetch_epic_with_children(
                client, cfg, epic_key,
            )
        except Exception:
            logger.error(
                "Failed to fetch %s", epic_key, exc_info=True,
            )
            report_lines.append(f"## {epic_key} — ERROR (fetch failed)")
            report_lines.append("")
            continue

        result = build_analysis_result(
            epic, children, cfg, inventory=inv,
        )

        if not result.get("apply_allowed"):
            report_lines.append(
                f"## {epic_key} — {result['need_state']} "
                f"({result['need_confidence']})"
            )
            report_lines.append(
                f"*{result['recommended_action']}*"
            )
            report_lines.append("")
            continue

        report_lines.append(
            f"## {epic_key} — {epic.summary}"
        )
        report_lines.append(
            f"Gaps: {', '.join(result['gaps'])}"
        )
        report_lines.append("")

        if use_llm:
            stories = compose_stories(
                result,
                model=model,
                categories=enabled_categories,
                category_guidance=category_guidance,
                story_points_guidance=story_points_guidance,
            )
        else:
            payloads = build_subtask_payloads(result, cfg)
            stories = [
                StoryPayload(
                    category=p["category"],
                    summary=p["summary"],
                    description=p["description"],
                )
                for p in payloads
            ]

        if not stories:
            report_lines.append("*No stories generated.*")
            report_lines.append("")
            continue

        if version and (apply or not apply):
            obs_epic = find_or_create_obs_epic(
                client, cfg, version, dry_run=not apply,
            )
            existing = find_existing_obs_stories(
                client, cfg, obs_epic["key"], epic_key,
            )
            existing_summaries = {
                e["summary"].lower() for e in existing
            }
        else:
            obs_epic = {"key": "(no version)", "summary": ""}
            existing_summaries = set()

        for story in stories:
            if total_created >= max_stories:
                report_lines.append(
                    f"*Reached max stories per run ({max_stories}). "
                    "Stopping.*"
                )
                break

            if story.summary.lower() in existing_summaries:
                total_skipped += 1
                report_lines.append(
                    f"- SKIP: {story.summary}"
                )
                continue

            if apply and version:
                try:
                    issue = create_obs_story(
                        client, cfg, obs_epic["key"], epic_key,
                        story.summary, story.description,
                        story_points=story.story_points,
                    )
                    total_created += 1
                    sp_tag = (
                        f" ({story.story_points}sp)"
                        if story.story_points else ""
                    )
                    report_lines.append(
                        f"- CREATED {issue.key}: "
                        f"{story.summary}{sp_tag}"
                    )
                except Exception:
                    logger.error(
                        "Failed to create story for %s",
                        epic_key, exc_info=True,
                    )
                    report_lines.append(
                        f"- ERROR: {story.summary}"
                    )
            else:
                total_created += 1
                sp_tag = (
                    f" ({story.story_points}sp)"
                    if story.story_points else ""
                )
                report_lines.append(
                    f"- WOULD CREATE: {story.summary}{sp_tag}"
                )

        if estimate_existing_sp and use_llm:
            unsized = fetch_unsized_stories(client, cfg, epic_key)
            if unsized:
                unsized_data = [
                    {
                        "key": iss.key,
                        "summary": str(
                            getattr(iss.fields, "summary", "") or ""
                        ),
                        "description": str(
                            getattr(iss.fields, "description", "") or ""
                        ),
                    }
                    for iss in unsized
                ]

                sp_estimates = estimate_story_points(
                    epic_summary=epic.summary,
                    epic_description=epic.description or "",
                    stories=unsized_data,
                    model=model,
                    story_points_guidance=story_points_guidance,
                )

                if sp_estimates:
                    report_lines.append(
                        f"### Story point estimates "
                        f"({len(sp_estimates)} unsized stories)"
                    )
                    for iss_key, sp_val in sp_estimates.items():
                        if apply:
                            try:
                                updated = update_story_points(
                                    client, cfg, iss_key, sp_val,
                                    model=model,
                                )
                                if updated:
                                    total_sp_updated += 1
                                    report_lines.append(
                                        f"- SP SET {iss_key}: "
                                        f"{sp_val}sp"
                                    )
                                else:
                                    total_sp_skipped += 1
                                    report_lines.append(
                                        f"- SP SKIP {iss_key}: "
                                        "already set"
                                    )
                            except Exception:
                                logger.error(
                                    "Failed to update SP for %s",
                                    iss_key, exc_info=True,
                                )
                                report_lines.append(
                                    f"- SP ERROR {iss_key}"
                                )
                        else:
                            total_sp_updated += 1
                            report_lines.append(
                                f"- WOULD SET SP {iss_key}: "
                                f"{sp_val}sp"
                            )

        report_lines.append("")

    report_lines.append("---")

    sp_summary = ""
    if total_sp_updated or total_sp_skipped:
        sp_summary = (
            f", {total_sp_updated} SP "
            f"{'set' if apply else 'would set'}"
        )
        if total_sp_skipped:
            sp_summary += f", {total_sp_skipped} SP skipped"

    report_lines.append(
        f"**Total: {total_created} "
        f"{'created' if apply else 'would create'}, "
        f"{total_skipped} skipped{sp_summary}**"
    )

    return "\n".join(report_lines)

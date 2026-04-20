"""Orchestrator: load config -> discover -> analyze -> plan -> apply.

Connects the analyzer, planner, and Jira client into a single pipeline
that can be driven by the CLI or a CI/cron job.  Supports pluggable
story categories (observability, docs, QE) with LLM-estimated story points.
"""

from __future__ import annotations

import logging
import os
import sys
import uuid
from typing import Any

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

import yaml

from agent.analyzer.analysis import build_analysis_result
from agent.analyzer.formatter import build_subtask_payloads
from agent.planner.llm import LLMError
from agent.planner.planner import compose_stories, estimate_story_points
from mcpserver.github.discover import build_all_inventories
from mcpserver.jira.client import (
    create_obs_story,
    fetch_epic_with_children,
    fetch_unsized_stories,
    find_existing_obs_stories,
    find_or_create_obs_epic,
    get_jira_client,
    is_duplicate_story,
    search_epics,
    update_story_points,
)
from schemas.stories import VALID_CATEGORIES, StoryPayload

logger = logging.getLogger(__name__)

CONFIG_PATH = os.path.join(_project_root, "config.yaml")


class ConfigError(ValueError):
    """Raised when config.yaml has invalid or missing values."""


def _load_config() -> dict[str, Any]:
    with open(CONFIG_PATH) as f:
        raw = yaml.safe_load(f)
    if not isinstance(raw, dict):
        raise ConfigError(
            f"config.yaml must be a YAML mapping, got {type(raw).__name__}"
        )
    _validate_config(raw)
    return raw


def _validate_config(cfg: dict[str, Any]) -> None:
    """Validate critical config fields at startup."""
    agent_cfg = cfg.get("agent", {})
    if not isinstance(agent_cfg, dict):
        raise ConfigError("'agent' section must be a mapping")

    cats = agent_cfg.get("enabled_categories", [])
    if not isinstance(cats, list):
        raise ConfigError("agent.enabled_categories must be a list")
    for cat in cats:
        if cat not in VALID_CATEGORIES:
            raise ConfigError(
                f"Unknown category {cat!r} in enabled_categories. "
                f"Valid: {sorted(VALID_CATEGORIES)}"
            )

    temp = agent_cfg.get("temperature", 0.2)
    try:
        float(temp)
    except (ValueError, TypeError):
        raise ConfigError(
            f"agent.temperature must be a number, got {temp!r}"
        )

    sp_cfg = agent_cfg.get("story_points", {})
    if sp_cfg and not isinstance(sp_cfg, dict):
        raise ConfigError("agent.story_points must be a mapping")

    creation = cfg.get("creation", {})
    if creation and not isinstance(creation, dict):
        raise ConfigError("'creation' section must be a mapping")


class _RunCounters:
    """Mutable counters for a single run."""

    def __init__(self) -> None:
        self.created = 0
        self.skipped = 0
        self.failed = 0
        self.llm_errors = 0
        self.sp_updated = 0
        self.sp_skipped = 0


class _RunContext:
    """Bundles shared state for a single run to avoid long param lists."""

    def __init__(
        self,
        client: Any,
        cfg: dict[str, Any],
        apply: bool,
        version: str,
        max_stories: int,
        model: str,
        temperature: float,
        story_points_guidance: str,
        run_id: str,
    ) -> None:
        self.client = client
        self.cfg = cfg
        self.apply = apply
        self.version = version
        self.max_stories = max_stories
        self.model = model
        self.temperature = temperature
        self.story_points_guidance = story_points_guidance
        self.run_id = run_id
        self.counters = _RunCounters()


def _children_as_dedup_entries(
    children: list[Any],
) -> list[dict[str, Any]]:
    """Convert source-epic children into dedup-compatible dicts.

    This lets the dedup logic cross-check LLM-generated stories
    against the source epic's existing child issues, preventing
    the agent from proposing stories that duplicate work already
    tracked under the feature epic.
    """
    entries: list[dict[str, Any]] = []
    for child in children:
        entries.append({
            "key": child.key,
            "summary": child.summary,
            "labels": [],
            "description": child.description,
        })
    return entries


def _dedup_and_create(
    stories: list[StoryPayload],
    epic_key: str,
    obs_epic: dict[str, Any],
    existing: list[dict[str, Any]],
    ctx: _RunContext,
) -> list[str]:
    """Dedup stories against existing, create or report each one."""
    lines: list[str] = []
    for story in stories:
        if ctx.counters.created >= ctx.max_stories:
            lines.append(
                f"*Reached max stories per run ({ctx.max_stories}). "
                "Stopping.*"
            )
            break

        if is_duplicate_story(
            story.summary, epic_key, existing,
        ):
            ctx.counters.skipped += 1
            lines.append(f"- SKIP (dup): {story.summary}")
            continue

        if ctx.apply and ctx.version:
            try:
                issue, warnings = create_obs_story(
                    ctx.client, ctx.cfg,
                    obs_epic["key"], epic_key,
                    story.summary, story.description,
                    story_points=story.story_points,
                    category=story.category,
                )
                ctx.counters.created += 1
                sp_tag = (
                    f" ({story.story_points}sp)"
                    if story.story_points else ""
                )
                warn_tag = (
                    f" ⚠ {warnings.warning_text()}"
                    if warnings.has_warnings else ""
                )
                lines.append(
                    f"- CREATED {issue.key}: "
                    f"{story.summary}{sp_tag}{warn_tag}"
                )
            except Exception:
                ctx.counters.failed += 1
                logger.error(
                    "[%s] Failed to create story for %s",
                    ctx.run_id, epic_key, exc_info=True,
                )
                lines.append(f"- ERROR: {story.summary}")
        else:
            ctx.counters.created += 1
            sp_tag = (
                f" ({story.story_points}sp)"
                if story.story_points else ""
            )
            lines.append(
                f"- WOULD CREATE: {story.summary}{sp_tag}"
            )
    return lines


def _estimate_existing_sp(
    epic_key: str,
    epic_summary: str,
    epic_description: str,
    ctx: _RunContext,
) -> list[str]:
    """Estimate SP for unsized children of an epic."""
    lines: list[str] = []
    unsized = fetch_unsized_stories(ctx.client, ctx.cfg, epic_key)
    if not unsized:
        return lines

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

    try:
        sp_estimates = estimate_story_points(
            epic_summary=epic_summary,
            epic_description=epic_description,
            stories=unsized_data,
            model=ctx.model,
            temperature=ctx.temperature,
            story_points_guidance=ctx.story_points_guidance,
        )
    except LLMError as exc:
        ctx.counters.llm_errors += 1
        logger.error(
            "[%s] SP estimation LLM failed for %s: %s",
            ctx.run_id, epic_key, exc,
        )
        lines.append(f"- **SP ESTIMATION ERROR**: {exc}")
        return lines
    except Exception as exc:
        ctx.counters.llm_errors += 1
        logger.error(
            "[%s] SP estimation failed for %s: %s",
            ctx.run_id, epic_key, exc, exc_info=True,
        )
        lines.append(
            f"- **SP ESTIMATION ERROR**: "
            f"{type(exc).__name__}: {exc}"
        )
        return lines

    if not sp_estimates:
        return lines

    lines.append(
        f"### Story point estimates "
        f"({len(sp_estimates)} unsized stories)"
    )
    for iss_key, sp_val in sp_estimates.items():
        if ctx.apply:
            try:
                updated = update_story_points(
                    ctx.client, ctx.cfg, iss_key, sp_val,
                    model=ctx.model,
                )
                if updated:
                    ctx.counters.sp_updated += 1
                    lines.append(f"- SP SET {iss_key}: {sp_val}sp")
                else:
                    ctx.counters.sp_skipped += 1
                    lines.append(
                        f"- SP SKIP {iss_key}: already set"
                    )
            except Exception:
                logger.error(
                    "[%s] Failed to update SP for %s",
                    ctx.run_id, iss_key, exc_info=True,
                )
                lines.append(f"- SP ERROR {iss_key}")
        else:
            ctx.counters.sp_updated += 1
            lines.append(
                f"- WOULD SET SP {iss_key}: {sp_val}sp"
            )
    return lines


def run(
    epic_keys: list[str] | None = None,
    version: str = "",
    since_days: int | None = None,
    component: str | None = None,
    fix_version: str | None = None,
    target_version: str | None = None,
    labels: list[str] | None = None,
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
    - component: filter epics by Jira component name
    - fix_version: filter epics by fixVersion
    - target_version: filter epics by Target Version
    - labels: filter epics by label(s)
    - apply: if True, create stories on Jira; if False, dry-run
    - model: LLM model string (overrides config)
    - use_llm: if True, use LLM for story composition; if False,
               use template-based stories
    - categories: category list override (None = use config)
    """
    run_id = uuid.uuid4().hex[:8]
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
    estimate_existing = sp_enabled and sp_cfg.get("estimate_existing", False)
    temperature: float = float(agent_cfg.get("temperature", 0.2))

    client = get_jira_client(cfg)

    logger.info("[%s] Building observability inventory...", run_id)
    inv = build_all_inventories(cfg)

    if epic_keys:
        epics_to_process: list[Any] = []
        for key in epic_keys:
            try:
                epic_issue = client.issue(key)
                epics_to_process.append(epic_issue)
            except Exception:
                logger.error(
                    "[%s] Failed to fetch epic %s",
                    run_id, key, exc_info=True,
                )
    else:
        kwargs: dict[str, Any] = {}
        if since_days:
            kwargs["since_days"] = since_days
        if component:
            kwargs["component"] = component
        if fix_version:
            kwargs["fix_version"] = fix_version
        if target_version:
            kwargs["target_version"] = target_version
        if labels:
            kwargs["labels"] = labels
        epics_to_process = search_epics(client, cfg, **kwargs)

    if not epics_to_process:
        return "No epics found to process."

    ctx = _RunContext(
        client=client,
        cfg=cfg,
        apply=apply,
        version=version,
        max_stories=max_stories,
        model=model,
        temperature=temperature,
        story_points_guidance=story_points_guidance,
        run_id=run_id,
    )

    filters_active: list[str] = []
    if component:
        filters_active.append(f"component={component}")
    if fix_version:
        filters_active.append(f"fixVersion={fix_version}")
    if target_version:
        filters_active.append(f"targetVersion={target_version}")
    if labels:
        filters_active.append(f"labels={','.join(labels)}")

    report_lines: list[str] = [
        f"# Epic Agent Run ({'APPLY' if apply else 'DRY-RUN'})",
        "",
        f"- **Epics:** {len(epics_to_process)}",
        f"- **Version:** {version or '(not set)'}",
        f"- **Model:** {model}",
        f"- **Mode:** {'LLM-assisted' if use_llm else 'template-based'}",
        f"- **Categories:** {', '.join(enabled_categories)}",
        f"- **Filters:** {', '.join(filters_active) if filters_active else '(none)'}",
        f"- **Run ID:** {run_id}",
        "",
    ]

    for epic_issue in epics_to_process:
        epic_key = epic_issue.key
        logger.info("[%s] Processing epic %s...", run_id, epic_key)

        try:
            epic, children = fetch_epic_with_children(
                client, cfg, epic_key,
            )
        except Exception:
            logger.error(
                "[%s] Failed to fetch %s",
                run_id, epic_key, exc_info=True,
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

        report_lines.append(f"## {epic_key} — {epic.summary}")
        report_lines.append(
            f"Gaps: {', '.join(result.get('gaps', []))}"
        )
        report_lines.append("")

        if use_llm:
            try:
                stories = compose_stories(
                    result,
                    model=model,
                    temperature=temperature,
                    categories=enabled_categories,
                    category_guidance=category_guidance,
                    story_points_guidance=story_points_guidance,
                )
            except LLMError as exc:
                ctx.counters.llm_errors += 1
                logger.error(
                    "[%s] LLM failed for %s: %s",
                    run_id, epic_key, exc,
                )
                report_lines.append(f"- **LLM ERROR**: {exc}")
                report_lines.append("")
                continue
            except Exception as exc:
                ctx.counters.llm_errors += 1
                logger.error(
                    "[%s] Story composition failed for %s: %s",
                    run_id, epic_key, exc, exc_info=True,
                )
                report_lines.append(
                    f"- **COMPOSITION ERROR**: "
                    f"{type(exc).__name__}: {exc}"
                )
                report_lines.append("")
                continue
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

        if version:
            obs_epic = find_or_create_obs_epic(
                client, cfg, version, dry_run=not apply,
            )
            existing = find_existing_obs_stories(
                client, cfg, obs_epic["key"], epic_key,
            )
        else:
            obs_epic = {"key": "(no version)", "summary": ""}
            existing = []

        existing.extend(
            _children_as_dedup_entries(children)
        )

        report_lines.extend(_dedup_and_create(
            stories, epic_key, obs_epic, existing, ctx,
        ))

        if estimate_existing and use_llm:
            report_lines.extend(_estimate_existing_sp(
                epic_key, epic.summary,
                epic.description or "", ctx,
            ))

        report_lines.append("")

    report_lines.append("---")

    counters = ctx.counters
    sp_summary = ""
    if counters.sp_updated or counters.sp_skipped:
        sp_summary = (
            f", {counters.sp_updated} SP "
            f"{'set' if apply else 'would set'}"
        )
        if counters.sp_skipped:
            sp_summary += f", {counters.sp_skipped} SP skipped"

    error_summary = ""
    if counters.llm_errors:
        error_summary += f", {counters.llm_errors} LLM errors"
    if counters.failed:
        error_summary += f", {counters.failed} failed"

    report_lines.append(
        f"**Total: {counters.created} "
        f"{'created' if apply else 'would create'}, "
        f"{counters.skipped} skipped{sp_summary}{error_summary}**"
    )

    return "\n".join(report_lines)

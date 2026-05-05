"""Orchestrator: load config -> discover -> analyze -> plan -> apply.

Connects the analyzer, planner, and Jira client into a single pipeline
that can be driven by the CLI or a CI/cron job.  Supports pluggable
story categories (observability, docs, QE) with LLM-estimated story points.
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.request
import uuid
from datetime import datetime, timezone
from typing import Any

from agent.analyzer.analysis import build_analysis_result
from agent.analyzer.formatter import build_subtask_payloads
from agent.planner.llm import LLMError
from agent.planner.planner import (
    check_epic_clarity,
    compose_stories,
    estimate_story_points,
)
from agent.discovery.discover import build_all_inventories
from agent.jira.client import (
    add_grooming_comment,
    add_grooming_label,
    create_obs_story,
    days_since_last_agent_comment,
    fetch_epic_with_children,
    fetch_unsized_stories,
    find_broad_matching_stories,
    find_existing_obs_stories,
    find_or_create_obs_epic,
    format_jira_version,
    get_jira_client,
    is_duplicate_story,
    needs_grooming,
    search_epics,
    update_story_points,
)
from schemas.stories import StoryPayload

logger = logging.getLogger(__name__)

STATUS_GROOMED = "groomed"
STATUS_NEEDS_GROOMING = "needs grooming"
STATUS_NOTHING_TO_DO = "nothing to do"
STATUS_ERROR = "error"
STATUS_LLM_ERROR = "llm error"

_STATUS_ORDER = {
    STATUS_ERROR: 0,
    STATUS_LLM_ERROR: 1,
    STATUS_NEEDS_GROOMING: 2,
    STATUS_NOTHING_TO_DO: 3,
    STATUS_GROOMED: 4,
    "": 5,
}

_DEFAULT_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config.yaml",
)


from schemas.config import AppConfig, ConfigError


def _load_config(
    path: str | None = None,
) -> AppConfig:
    config_path = path or _DEFAULT_CONFIG_PATH
    return AppConfig.from_yaml(config_path)


def _validate_config(cfg: dict[str, Any]) -> None:
    """Validate critical config fields at startup.

    Kept for backward compatibility with tests that call it directly.
    Delegates to AppConfig validation.
    """
    AppConfig.from_dict(cfg)


class _EpicTally:
    """Per-epic story counts by category and status."""

    __slots__ = (
        "key", "summary", "by_category", "status",
        "fix_version", "target_version",
        "dev_sp_existing", "dev_sp_proposed",
        "qe_sp_existing", "qe_sp_proposed",
        "docs_sp_existing", "docs_sp_proposed",
        "has_no_qe", "has_no_doc",
    )

    def __init__(
        self, key: str, *, status: str = "",
    ) -> None:
        self.key = key
        self.summary: str = ""
        self.by_category: dict[str, int] = {}
        self.status = status
        self.fix_version: str = ""
        self.target_version: str = ""
        self.dev_sp_existing: int = 0
        self.dev_sp_proposed: int = 0
        self.qe_sp_existing: int = 0
        self.qe_sp_proposed: int = 0
        self.docs_sp_existing: int = 0
        self.docs_sp_proposed: int = 0
        self.has_no_qe: bool = False
        self.has_no_doc: bool = False

    def record(self, category: str) -> None:
        self.by_category[category] = (
            self.by_category.get(category, 0) + 1
        )

    @property
    def total(self) -> int:
        return sum(self.by_category.values())


class _RunCounters:
    """Mutable counters for a single run."""

    def __init__(self) -> None:
        self.created = 0
        self.skipped = 0
        self.skipped_epics = 0
        self.failed = 0
        self.llm_errors = 0
        self.sp_updated = 0
        self.sp_skipped = 0
        self.sp_failed = 0
        self.needs_grooming = 0
        self.by_category: dict[str, int] = {}
        self.epic_tallies: list[_EpicTally] = []

    def _get_or_create_tally(
        self, epic_key: str,
    ) -> _EpicTally:
        for tally in self.epic_tallies:
            if tally.key == epic_key:
                return tally
        t = _EpicTally(epic_key)
        self.epic_tallies.append(t)
        return t

    def record_category(
        self, category: str, epic_key: str,
    ) -> None:
        self.by_category[category] = (
            self.by_category.get(category, 0) + 1
        )
        tally = self._get_or_create_tally(epic_key)
        tally.record(category)
        if not tally.status:
            tally.status = STATUS_GROOMED

    def record_epic_status(
        self, epic_key: str, status: str,
    ) -> None:
        """Register an epic with a status for the summary table.

        Status values: "needs grooming", "nothing to do", or
        left empty for epics that produced stories.
        """
        tally = self._get_or_create_tally(epic_key)
        if not tally.status:
            tally.status = status


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

    Entries are tagged ``"_from_children": True`` so that
    ``is_duplicate_story`` uses only exact-summary matching.
    Key-reference and containment strategies are disabled for
    children because the LLM intentionally embeds child keys in
    proposed stories and child summaries are naturally substrings
    of the proposed observability/QE/docs summaries.
    """
    entries: list[dict[str, Any]] = []
    for child in children:
        entries.append({
            "key": child.key,
            "summary": child.summary,
            "labels": [],
            "description": child.description,
            "_from_children": True,
        })
    return entries


def _classify_child_category(child: Any) -> str:
    """Classify a child issue as 'qe', 'docs', or 'dev' (best-effort).

    Checks labels and summary prefix to assign a bucket. Falls back to
    'dev' so non-QE/docs work is not silently lost from SP totals.
    """
    labels = set(getattr(child, "labels", []) or [])
    summary = (getattr(child, "summary", "") or "").lower()
    if "qe" in labels or summary.startswith("[qe]"):
        return "qe"
    if labels & {"doc", "docs", "documentation"} or summary.startswith(
        ("[docs]", "[doc]")
    ):
        return "docs"
    return "dev"


def _dedup_and_create(
    stories: list[StoryPayload],
    epic_key: str,
    obs_epic: dict[str, Any],
    existing: list[dict[str, Any]],
    ctx: _RunContext,
    tally: _EpicTally | None = None,
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

        dup_key = is_duplicate_story(
            story.summary, epic_key, existing,
        )
        if dup_key:
            ctx.counters.skipped += 1
            lines.append(
                f"- SKIP (dup of {dup_key}): {story.summary}"
            )
            continue

        # Accumulate proposed SP into tally (only non-dup stories)
        if tally is not None and story.story_points:
            cat = story.category
            sp = story.story_points
            if cat == "qe":
                tally.qe_sp_proposed += sp
            elif cat in ("docs", "documentation"):
                tally.docs_sp_proposed += sp
            else:
                tally.dev_sp_proposed += sp

        if ctx.apply and ctx.version:
            try:
                issue, warnings = create_obs_story(
                    ctx.client, ctx.cfg,
                    obs_epic["key"], epic_key,
                    story.summary, story.description,
                    story_points=story.story_points,
                    category=story.category,
                    run_id=ctx.run_id,
                )
                ctx.counters.created += 1
                ctx.counters.record_category(
                    story.category, epic_key,
                )
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
            ctx.counters.record_category(
                story.category, epic_key,
            )
            sp_tag = (
                f" ({story.story_points}sp)"
                if story.story_points else ""
            )
            lines.append(
                f"- WOULD CREATE: {story.summary}{sp_tag}"
            )
            lines.append("")
            lines.append(
                f"  **Category:** {story.category}"
            )
            if story.reasoning:
                lines.append(
                    f"  **Reasoning:** {story.reasoning}"
                )
            if story.description:
                lines.append("")
                for desc_line in story.description.splitlines():
                    lines.append(f"  > {desc_line}")
            lines.append("")
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

    valid_keys = {d["key"] for d in unsized_data}

    lines.append(
        f"### Story point estimates "
        f"({len(sp_estimates)} unsized stories)"
    )
    for iss_key, sp_val in sp_estimates.items():
        if iss_key not in valid_keys:
            logger.warning(
                "[%s] SP estimation returned unknown key %s "
                "(not in unsized set), skipping",
                ctx.run_id, iss_key,
            )
            lines.append(
                f"- SP SKIP {iss_key}: "
                f"not in unsized set (LLM hallucination?)"
            )
            continue
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
                ctx.counters.sp_failed += 1
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


def _resolve_epics(
    client: Any,
    cfg: dict[str, Any],
    run_id: str,
    *,
    epic_keys: list[str] | None = None,
    jql: str | None = None,
    since_days: int | None = None,
    component: str | None = None,
    fix_version: str | None = None,
    target_version: str | None = None,
    labels: list[str] | None = None,
) -> list[Any]:
    """Resolve which epics to process from CLI args."""
    if epic_keys:
        epics: list[Any] = []
        failed_keys: list[str] = []
        for key in epic_keys:
            try:
                epics.append(client.issue(key))
            except Exception:
                logger.error(
                    "[%s] Failed to fetch epic %s",
                    run_id, key, exc_info=True,
                )
                failed_keys.append(key)
        if failed_keys:
            logger.warning(
                "[%s] Could not fetch %d epic(s): %s",
                run_id, len(failed_keys),
                ", ".join(failed_keys),
            )
        return epics

    if jql:
        logger.info("[%s] Using raw JQL: %s", run_id, jql)
        return search_epics(client, cfg, jql=jql)

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
    return search_epics(client, cfg, **kwargs)


def _check_grooming(
    epic: Any,
    children: list[Any],
    cfg: dict[str, Any],
    app_cfg: Any,
    ctx: _RunContext,
    *,
    use_llm: bool,
    model: str,
    temperature: float,
) -> tuple[bool, str]:
    """Check if an epic needs grooming.

    Returns (flagged, reason).
    """
    if needs_grooming(epic, children, cfg):
        return True, (
            "Epic lacks sufficient detail (short description "
            "and no child stories)."
        )

    if use_llm and app_cfg.grooming.llm_clarity_check:
        try:
            children_data = [
                {
                    "key": c.key,
                    "summary": c.summary,
                    "description": c.description,
                }
                for c in children
            ]
            clarity = check_epic_clarity(
                epic_key=epic.key,
                epic_summary=epic.summary,
                epic_description=epic.description or "",
                children=children_data,
                model=model,
                temperature=temperature,
            )
            if clarity["verdict"] == "needs_grooming":
                return True, clarity["reason"]
        except Exception:
            logger.warning(
                "[%s] LLM clarity check failed for %s, "
                "proceeding with analysis",
                ctx.run_id, epic.key, exc_info=True,
            )

    return False, ""


def _handle_grooming(
    epic_key: str,
    epic_summary: str,
    grooming_reason: str,
    app_cfg: Any,
    ctx: _RunContext,
) -> list[str]:
    """Handle a grooming-flagged epic: add labels/comments."""
    lines: list[str] = []
    epic_link = _jira_link(epic_key, ctx.cfg)
    grooming_label = app_cfg.grooming.label
    ctx.counters.needs_grooming += 1
    ctx.counters.record_epic_status(
        epic_key, STATUS_NEEDS_GROOMING,
    )
    lines.append(f'<a id="{_epic_anchor(epic_key)}"></a>')
    lines.append(
        f"## {epic_link} — {epic_summary} — NEEDS GROOMING"
    )
    lines.append(grooming_reason)

    comment_text = (
        f"[Epic Agent] {grooming_reason}\n\n"
        f"Please add more detail and remove the "
        f"*{grooming_label}* label to re-enable processing."
    )
    cooldown = app_cfg.grooming.comment_cooldown_days
    last_days = days_since_last_agent_comment(
        ctx.client, epic_key,
    )
    comment_due = last_days is None or last_days >= cooldown

    if ctx.apply:
        try:
            add_grooming_label(ctx.client, ctx.cfg, epic_key)
            if comment_due:
                add_grooming_comment(
                    ctx.client, ctx.cfg, epic_key,
                    comment_override=comment_text,
                )
                lines.append(
                    f"Added *{grooming_label}* label and "
                    f"comment to {epic_key}."
                )
            else:
                lines.append(
                    f"Label *{grooming_label}* ensured on "
                    f"{epic_key}; comment skipped "
                    f"(last reminder {last_days:.0f}d ago)."
                )
        except Exception:
            logger.error(
                "[%s] Failed to label/comment %s for grooming",
                ctx.run_id, epic_key, exc_info=True,
            )
            lines.append(
                "Failed to add grooming label/comment."
            )
    else:
        if comment_due:
            lines.append(
                f"*Would add '{grooming_label}' label and "
                f"grooming comment.*"
            )
        else:
            lines.append(
                f"*Would add '{grooming_label}' label; "
                f"comment skipped "
                f"(last reminder {last_days:.0f}d ago).*"
            )
    lines.append("")
    return lines


def _process_epic(
    epic_issue: Any,
    ctx: _RunContext,
    app_cfg: Any,
    inv: Any,
    *,
    enabled_categories: list[str],
    category_guidance: dict[str, Any],
    use_llm: bool,
    estimate_existing: bool,
) -> list[str]:
    """Process a single epic: grooming check, analysis, story creation."""
    epic_key = epic_issue.key
    run_id = ctx.run_id
    client = ctx.client
    cfg = ctx.cfg
    model = ctx.model
    temperature = ctx.temperature

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
        ctx.counters.record_epic_status(
            epic_key, STATUS_ERROR,
        )
        epic_link = _jira_link(epic_key, cfg)
        return [
            f'<a id="{_epic_anchor(epic_key)}"></a>',
            f"## {epic_link} — ERROR (fetch failed)",
            "",
        ]

    # Populate tally with summary and version metadata immediately
    # after fetch so data is available even on early exits (grooming).
    tally = ctx.counters._get_or_create_tally(epic_key)
    tally.summary = epic.summary
    fields = getattr(epic_issue, "fields", None)
    fix_versions = getattr(fields, "fixVersions", None) or []
    if fix_versions:
        tally.fix_version = (
            fix_versions[0].get("name", "") if isinstance(fix_versions[0], dict)
            else getattr(fix_versions[0], "name", "")
        )
    tv_field = app_cfg.jira.target_version_field
    if tv_field:
        tv_raw = getattr(fields, tv_field, None)
        if tv_raw is not None:
            # Target Version is a list of JIRA Version objects (same
            # structure as fixVersions).  Join all names into one string.
            if isinstance(tv_raw, list):
                names = [
                    v.get("name", "") if isinstance(v, dict)
                    else getattr(v, "name", str(v))
                    for v in tv_raw
                ]
                tally.target_version = ", ".join(n for n in names if n)
            elif isinstance(tv_raw, dict):
                tally.target_version = tv_raw.get("name", "")
            else:
                tally.target_version = str(tv_raw)

    flagged, grooming_reason = _check_grooming(
        epic, children, cfg, app_cfg, ctx,
        use_llm=use_llm, model=model, temperature=temperature,
    )
    if flagged:
        return _handle_grooming(
            epic_key, epic.summary, grooming_reason,
            app_cfg, ctx,
        )

    result = build_analysis_result(
        epic, children, cfg, inventory=inv,
    )

    epic_header: list[str] = []
    epic_link = _jira_link(epic_key, cfg)
    epic_header.append(f'<a id="{_epic_anchor(epic_key)}"></a>')
    epic_header.append(f"## {epic_link} — {epic.summary}")
    epic_components = result.get("epic_components", [])
    if epic_components:
        epic_header.append(
            f"Components: {', '.join(epic_components)}"
        )
    epic_header.append(
        f"Gaps: {', '.join(result.get('gaps', []))}"
    )
    epic_header.append("")

    # Sum existing child SP by category.
    for child in children:
        child_sp = child.story_points
        if child_sp:
            bucket = _classify_child_category(child)
            if bucket == "qe":
                tally.qe_sp_existing += child_sp
            elif bucket == "docs":
                tally.docs_sp_existing += child_sp
            else:
                tally.dev_sp_existing += child_sp

    no_qe_label = app_cfg.grooming.no_qe_label
    no_doc_label = app_cfg.grooming.no_doc_label

    epic_labels = set(result.get("epic_labels", []))
    epic_categories = list(enabled_categories)
    if no_doc_label in epic_labels or "no-docs" in epic_labels:
        tally.has_no_doc = True
        epic_categories = [
            c for c in epic_categories if c != "docs"
        ]
    if no_qe_label in epic_labels:
        tally.has_no_qe = True
        epic_categories = [
            c for c in epic_categories if c != "qe"
        ]

    if use_llm:
        try:
            stories = compose_stories(
                result,
                model=model,
                temperature=temperature,
                categories=epic_categories,
                category_guidance=category_guidance,
                story_points_guidance=ctx.story_points_guidance,
            )
        except LLMError as exc:
            ctx.counters.llm_errors += 1
            ctx.counters.record_epic_status(
                epic_key, STATUS_LLM_ERROR,
            )
            logger.error(
                "[%s] LLM failed for %s: %s",
                run_id, epic_key, exc,
            )
            return epic_header + [f"- **LLM ERROR**: {exc}", ""]
        except Exception as exc:
            ctx.counters.llm_errors += 1
            ctx.counters.record_epic_status(
                epic_key, STATUS_ERROR,
            )
            logger.error(
                "[%s] Story composition failed for %s: %s",
                run_id, epic_key, exc, exc_info=True,
            )
            return epic_header + [
                f"- **COMPOSITION ERROR**: "
                f"{type(exc).__name__}: {exc}",
                "",
            ]
    else:
        payloads = build_subtask_payloads(result, cfg)
        stories = [
            StoryPayload(
                category=p["category"],
                summary=p["summary"],
                description=p["description"],
            )
            for p in payloads
            if p["category"] in epic_categories
        ]

    if not stories:
        ctx.counters.skipped_epics += 1
        ctx.counters.record_epic_status(
            epic_key, STATUS_NOTHING_TO_DO,
        )
        return epic_header + [
            "No stories to create for this epic.", "",
        ]

    if ctx.version:
        try:
            obs_epic = find_or_create_obs_epic(
                client, cfg, ctx.version, dry_run=not ctx.apply,
            )
            existing = find_existing_obs_stories(
                client, cfg, obs_epic["key"], epic_key,
            )
        except Exception:
            logger.error(
                "[%s] Failed obs epic / dedup lookup for %s",
                run_id, epic_key, exc_info=True,
            )
            ctx.counters.record_epic_status(
                epic_key, STATUS_ERROR,
            )
            return epic_header + [
                "- **ERROR**: Failed to resolve "
                "observability epic or existing stories.",
                "",
            ]
    else:
        obs_epic = {"key": "(no version)", "summary": ""}
        existing = []

    existing.extend(_children_as_dedup_entries(children))

    domain_keywords = result.get("domain_keywords", [])
    if domain_keywords:
        try:
            existing.extend(
                find_broad_matching_stories(
                    client, cfg, epic_key, domain_keywords,
                )
            )
        except Exception:
            logger.warning(
                "[%s] Broad keyword search failed for %s, "
                "continuing without it",
                run_id, epic_key, exc_info=True,
            )

    story_lines = _dedup_and_create(
        stories, epic_key, obs_epic, existing, ctx,
        tally=tally,
    )

    # If all proposed stories were skipped as dups (nothing created,
    # no error), the tally still has no status.  Mark as nothing to do
    # so the epic appears correctly in both summary tables.
    if not tally.status:
        ctx.counters.skipped_epics += 1
        ctx.counters.record_epic_status(
            epic_key, STATUS_NOTHING_TO_DO,
        )

    lines = epic_header + story_lines

    if estimate_existing and use_llm:
        lines.extend(_estimate_existing_sp(
            epic_key, epic.summary,
            epic.description or "", ctx,
        ))

    lines.append("")
    return lines


def _epic_anchor(epic_key: str) -> str:
    """Return a stable anchor id for an epic's detail section."""
    return epic_key.lower()


def _jira_link(epic_key: str, cfg: dict[str, Any]) -> str:
    """Return a markdown link to the epic in Jira."""
    base = cfg.get("jira", {}).get("url", "").rstrip("/")
    if base:
        return f"[{epic_key}]({base}/browse/{epic_key})"
    return epic_key


def _fetch_open_feedback_count(
    feedback_repo: str,
) -> int | None:
    """Return the number of open agent-feedback issues on GitHub.

    Makes a single API call with per_page=100. If the response
    contains exactly 100 items, checks the Link header for the
    last page to compute a full count. Returns None on any error
    so callers can gracefully skip the line.
    """
    if not feedback_repo:
        return None
    repo = feedback_repo.rstrip("/")
    if repo.startswith("https://github.com/"):
        repo = repo[len("https://github.com/"):]
    url = (
        f"https://api.github.com/repos/{repo}/issues"
        f"?labels=agent-feedback&state=open&per_page=100"
    )
    try:
        req = urllib.request.Request(
            url,
            headers={"Accept": "application/vnd.github+json",
                     "User-Agent": "cnv-epic-agent"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read())
            count = len(body)
            if count < 100:
                return count
            link = resp.headers.get("Link", "")
            if 'rel="last"' in link:
                m = re.search(r'page=(\d+)>;\s*rel="last"', link)
                if m:
                    last_page = int(m.group(1))
                    return (last_page - 1) * 100 + count
            return count
    except Exception:
        logger.warning(
            "Could not fetch feedback issue count from %s",
            feedback_repo, exc_info=False,
        )
        return None


def _sp_cell(existing: int, proposed: int) -> str:
    """Format a story-points cell as '21 (+8)' or '0' or '0 (+3)'."""
    if proposed:
        return f"{existing} (+{proposed})"
    return str(existing)


def _build_report_summary(
    counters: _RunCounters,
    processed: int,
    apply: bool,
) -> list[str]:
    """Build two summary tables followed by run-level counters."""
    lines: list[str] = []
    lines.append("---")
    lines.append("")
    lines.append("## Summary")
    lines.append("")

    action = "created" if apply else "would create"

    if counters.epic_tallies:
        # ── Partition tallies for the three planning sub-tables ───────
        # 1. Has fixVersion  → "Fix Version Epics"
        # 2. No fixVersion, has targetVersion → "Target Version Epics"
        # 3. Neither → "Unversioned Epics"
        def _version_sort_key(t: _EpicTally) -> tuple:
            return (_STATUS_ORDER.get(t.status, 5), t.key)

        fix_ver_tallies = sorted(
            [t for t in counters.epic_tallies if t.fix_version],
            key=_version_sort_key,
        )
        target_ver_tallies = sorted(
            [t for t in counters.epic_tallies
             if not t.fix_version and t.target_version],
            key=_version_sort_key,
        )
        unversioned_tallies = sorted(
            [t for t in counters.epic_tallies
             if not t.fix_version and not t.target_version],
            key=_version_sort_key,
        )

        # Status-first order for Table 2 (all epics)
        sorted_tallies = sorted(
            counters.epic_tallies,
            key=lambda t: (_STATUS_ORDER.get(t.status, 5), t.key),
        )

        def _planning_row(tally: _EpicTally) -> str:
            anchor = _epic_anchor(tally.key)
            link = f"[{tally.key}](#{anchor})"
            status = tally.status or ""
            fix_ver = tally.fix_version or "-"
            target_ver = tally.target_version or "-"
            dev_sp = _sp_cell(
                tally.dev_sp_existing, tally.dev_sp_proposed,
            )
            qe_sp = (
                "no-qe" if tally.has_no_qe
                else _sp_cell(
                    tally.qe_sp_existing, tally.qe_sp_proposed,
                )
            )
            docs_sp = (
                "no-doc" if tally.has_no_doc
                else _sp_cell(
                    tally.docs_sp_existing, tally.docs_sp_proposed,
                )
            )
            summary = tally.summary or ""
            return (
                f"| {link} | {summary} | {status} | {fix_ver}"
                f" | {target_ver} | {dev_sp} | {qe_sp} | {docs_sp} |"
            )

        def _planning_totals(tallies: list[_EpicTally]) -> str:
            dev_ex = sum(t.dev_sp_existing for t in tallies)
            dev_pr = sum(t.dev_sp_proposed for t in tallies)
            qe_ex = sum(
                t.qe_sp_existing for t in tallies if not t.has_no_qe
            )
            qe_pr = sum(
                t.qe_sp_proposed for t in tallies if not t.has_no_qe
            )
            docs_ex = sum(
                t.docs_sp_existing for t in tallies if not t.has_no_doc
            )
            docs_pr = sum(
                t.docs_sp_proposed for t in tallies if not t.has_no_doc
            )
            return (
                f"| **Total** | | | | | {_sp_cell(dev_ex, dev_pr)}"
                f" | {_sp_cell(qe_ex, qe_pr)}"
                f" | {_sp_cell(docs_ex, docs_pr)} |"
            )

        _PLANNING_HEADER = (
            "| Epic | Summary | Status | Fix Ver | Target Ver"
            " | Dev SP | QE SP | Docs SP |"
        )
        _PLANNING_SEP = "| --- | --- | --- | --- | --- | --- | --- | --- |"

        # ── Table 1a: Fix Version Epics ───────────────────────────────
        if fix_ver_tallies:
            lines.append("### Fix Version Epics")
            lines.append("")
            lines.append(_PLANNING_HEADER)
            lines.append(_PLANNING_SEP)
            for tally in fix_ver_tallies:
                lines.append(_planning_row(tally))
            lines.append(_planning_totals(fix_ver_tallies))
            lines.append("")

        # ── Table 1b: Target Version Epics ───────────────────────────
        if target_ver_tallies:
            lines.append("### Target Version Epics")
            lines.append("")
            lines.append(_PLANNING_HEADER)
            lines.append(_PLANNING_SEP)
            for tally in target_ver_tallies:
                lines.append(_planning_row(tally))
            lines.append(_planning_totals(target_ver_tallies))
            lines.append("")

        # ── Table 1c: Unversioned Epics ──────────────────────────────
        if unversioned_tallies:
            lines.append("### Unversioned Epics")
            lines.append("")
            lines.append(_PLANNING_HEADER)
            lines.append(_PLANNING_SEP)
            for tally in unversioned_tallies:
                lines.append(_planning_row(tally))
            lines.append(_planning_totals(unversioned_tallies))
            lines.append("")

        # ── Table 2: Agent Proposed Stories ──────────────────────────
        all_cats = sorted(counters.by_category)
        lines.append("### Agent Proposed Stories")
        lines.append("")
        if all_cats:
            cat_headers = " | ".join(all_cats)
            cat_sep = " | ".join("---" for _ in all_cats)
            lines.append(
                f"| Epic | Summary | Status | {cat_headers} | Total |"
            )
            lines.append(f"| --- | --- | --- | {cat_sep} | --- |")
        else:
            lines.append("| Epic | Summary | Status | Total |")
            lines.append("| --- | --- | --- | --- |")
        for tally in sorted_tallies:
            anchor = _epic_anchor(tally.key)
            link = f"[{tally.key}](#{anchor})"
            summary = tally.summary or ""
            status = tally.status or ""
            if all_cats:
                cols = " | ".join(
                    str(tally.by_category.get(c, 0))
                    for c in all_cats
                )
                lines.append(
                    f"| {link} | {summary} | {status}"
                    f" | {cols} | {tally.total} |"
                )
            else:
                lines.append(
                    f"| {link} | {summary} | {status} | {tally.total} |"
                )
        if all_cats:
            total_cols = " | ".join(
                str(counters.by_category.get(c, 0))
                for c in all_cats
            )
            lines.append(
                f"| **Total** | | | {total_cols} | {counters.created} |"
            )
        else:
            lines.append(
                f"| **Total** | | | {counters.created} |"
            )
        lines.append("")

    lines.append("| Metric | Count |")
    lines.append("|---|---|")
    lines.append(f"| Epics processed | {processed} |")
    if counters.needs_grooming:
        lines.append(
            f"| Epics needing grooming | {counters.needs_grooming} |"
        )
    if counters.skipped_epics:
        lines.append(
            f"| Epics with nothing to report | "
            f"{counters.skipped_epics} |"
        )
    lines.append(f"| Stories {action} | {counters.created} |")
    if counters.skipped:
        lines.append(
            f"| Stories skipped (dup) | {counters.skipped} |"
        )
    if counters.failed:
        lines.append(f"| Stories failed | {counters.failed} |")
    if counters.llm_errors:
        lines.append(f"| LLM errors | {counters.llm_errors} |")
    if counters.sp_updated:
        lines.append(
            f"| Story points "
            f"{'set' if apply else 'would set'} | "
            f"{counters.sp_updated} |"
        )
    if counters.sp_skipped:
        lines.append(
            f"| Story points skipped | {counters.sp_skipped} |"
        )
    if counters.sp_failed:
        lines.append(
            f"| Story points failed | {counters.sp_failed} |"
        )
    lines.append("")

    return lines


def run(
    epic_keys: list[str] | None = None,
    jql: str | None = None,
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
    config_path: str | None = None,
    no_cache: bool = False,
) -> str:
    """Run the full epic agent pipeline.

    Parameters:
    - epic_keys: specific epic keys to process (None = scan via JQL)
    - jql: raw JQL query (bypasses all other filters)
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
    - config_path: path to config.yaml (None = project default)
    """
    run_id = uuid.uuid4().hex[:8]
    app_cfg = _load_config(config_path)
    cfg = app_cfg.raw

    if apply and not version:
        raise ConfigError(
            "Cannot apply changes without a version. "
            "Pass --version to specify the target CNV version."
        )

    if not model:
        model = os.environ.get(
            "LLM_MODEL", app_cfg.agent.default_model,
        )

    max_stories = app_cfg.agent.max_stories_per_run

    if categories:
        from schemas.stories import VALID_CATEGORIES
        for cat in categories:
            if cat not in VALID_CATEGORIES:
                raise ConfigError(
                    f"Unknown category {cat!r}. "
                    f"Valid: {sorted(VALID_CATEGORIES)}"
                )
    enabled_categories: list[str] = (
        categories or app_cfg.agent.enabled_categories
    )
    category_guidance: dict[str, Any] = (
        app_cfg.agent.category_guidance
    )
    sp_enabled = app_cfg.agent.story_points.enabled
    story_points_guidance: str = (
        app_cfg.agent.story_points.guidance
        if sp_enabled else ""
    )
    estimate_existing = (
        sp_enabled
        and app_cfg.agent.story_points.estimate_existing
    )
    temperature: float = app_cfg.agent.temperature

    client = get_jira_client(cfg)

    logger.info("[%s] Building observability inventory...", run_id)
    inv = build_all_inventories(cfg, no_cache=no_cache)

    if version and not fix_version and not target_version:
        jira_version = format_jira_version(cfg, version)
        fix_version = jira_version
        target_version = jira_version
        logger.info(
            "[%s] Auto-derived version filter: %s",
            run_id, jira_version,
        )

    epics_to_process = _resolve_epics(
        client, cfg, run_id,
        epic_keys=epic_keys, jql=jql,
        since_days=since_days, component=component,
        fix_version=fix_version,
        target_version=target_version,
        labels=labels,
    )

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
    if jql:
        filters_active.append(f"jql={jql}")
    else:
        if component:
            filters_active.append(f"component={component}")
        if fix_version:
            filters_active.append(f"fixVersion={fix_version}")
        if target_version:
            filters_active.append(f"targetVersion={target_version}")
        if labels:
            filters_active.append(f"labels={','.join(labels)}")

    run_timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    feedback_repo = app_cfg.agent.feedback_repo
    feedback_count = _fetch_open_feedback_count(feedback_repo)

    header_lines: list[str] = [
        f"# Epic Agent Run ({'APPLY' if apply else 'DRY-RUN'})",
        "",
        f"- **Date:** {run_timestamp}",
        f"- **Epics:** {len(epics_to_process)}",
        f"- **Version:** {version or '(not set)'}",
        f"- **Model:** {model}",
        f"- **Mode:** {'LLM-assisted' if use_llm else 'template-based'}",
        f"- **Categories:** {', '.join(enabled_categories)}",
        f"- **Filters:** {', '.join(filters_active) if filters_active else '(none)'}",
        f"- **Run ID:** {run_id}",
    ]
    if feedback_count is not None:
        issues_url = f"{feedback_repo.rstrip('/')}/issues?labels=agent-feedback&state=open"
        header_lines.append(
            f"- **Open feedback issues:** {feedback_count}"
            f" — [review]({issues_url})"
        )
    header_lines.append("")

    epic_detail_lines: list[str] = []
    for epic_issue in epics_to_process:
        epic_lines = _process_epic(
            epic_issue, ctx, app_cfg, inv,
            enabled_categories=enabled_categories,
            category_guidance=category_guidance,
            use_llm=use_llm,
            estimate_existing=estimate_existing,
        )
        epic_detail_lines.extend(epic_lines)

    summary_lines = _build_report_summary(
        ctx.counters, len(epics_to_process), apply,
    )

    return "\n".join(header_lines + summary_lines + epic_detail_lines)

"""Prompt templates for LLM-assisted story composition.

Used by both the MCP ``@server.prompt()`` endpoint and the standalone
agent planner so that both workflows produce identically-structured
stories.  Supports pluggable categories (observability, docs, QE) and
LLM-estimated story points via injected guidance from config.
"""

from __future__ import annotations

import json
from typing import Any

from schemas.stories import SP_ESTIMATION_JSON_SCHEMA, STORY_JSON_SCHEMA

_PROPOSALS_MAX_CHARS = 4000
_TELEMETRY_MAX_CHARS = 2000


def _capped_json(
    data: Any,
    max_chars: int,
    *,
    label: str = "items",
) -> str:
    """Serialize *data* to JSON, truncating if it exceeds *max_chars*.

    When the full dump is too large, items are dropped from the end
    until the output fits.  A note is appended so the LLM knows
    the data was trimmed.
    """
    full = json.dumps(data, indent=2, default=str)
    if len(full) <= max_chars:
        return full

    if isinstance(data, list):
        total = len(data)
        subset = list(data)
        while subset:
            attempt = json.dumps(subset, indent=2, default=str)
            if len(attempt) <= max_chars:
                return (
                    attempt.rstrip()
                    + f"\n// (truncated — {total} total {label},"
                    + f" showing top {len(subset)})"
                )
            subset.pop()
        return f"[]  // (truncated — {total} total {label})"

    if isinstance(data, dict):
        total = len(data)
        keys = list(data.keys())
        while keys:
            subset_dict = {k: data[k] for k in keys}
            attempt = json.dumps(subset_dict, indent=2, default=str)
            if len(attempt) <= max_chars:
                return (
                    attempt.rstrip()
                    + f"\n// (truncated — {total} total {label},"
                    + f" showing top {len(keys)})"
                )
            keys.pop()
        return f"{{}}  // (truncated — {total} total {label})"

    return full[:max_chars] + f"\n// (truncated — full output was {len(full)} chars)"

SYSTEM_PROMPT = """\
You are an expert Site Reliability Engineer and engineering lead
specializing in KubeVirt / CNV.  Your job is to compose Jira stories
for a feature epic across multiple categories.

## Observability stories (metrics, alerts, dashboards, telemetry)

- Each story must explain *why* the proposed instrumentation matters for
  the specific feature, not just restate what it is.
- Reference existing artifacts found in the codebase (metrics, alerts,
  panels) and explain whether they already cover the new behaviour or
  need extending.
- Proposed new items must include concrete metric names following the
  ``kubevirt_<component>_<noun>_<unit>`` convention.
- Alert names must use CamelCase with a component prefix.
- Dashboard panel suggestions must reference the target dashboard and
  explain what operators will learn from the panel.
- Telemetry suggestions must justify why the recording rule is suitable
  for cluster-level aggregation and adoption tracking.

## Documentation stories (docs)

- Only propose a docs story when the epic genuinely changes user-facing
  behavior, APIs, CLI flags, or UI — not for internal refactors.
- The story must specify *which* docs need updating (API reference,
  user guide, release notes, etc.) and why.

## QE stories (qe)

- Review ALL child stories under the epic and identify which ones
  introduce testable changes (runtime behavior, APIs, UI, CLI,
  configuration).
- The QE story must reference the specific child stories it covers
  and explain what test scenarios are needed for each.
- Ensure full coverage: every child story that changes user-facing
  behavior or introduces a new code path must be addressed in the
  QE test plan.
- Group related test areas into a single QE story when they share
  the same component, but split into multiple QE stories if the
  epic spans distinct subsystems.
- Each QE story must outline: happy path, edge cases, failure modes,
  upgrade/rollback scenarios (if applicable), and performance
  considerations.
- Only propose QE stories when the epic has child stories with
  genuinely testable changes — not for docs-only or config-only work.

## Story points

For every **story** you produce (all categories), estimate story points
on the Fibonacci scale (1, 2, 3, 5, 8, 13) based on implementation
complexity.  Do NOT assign story points to bugs — only stories.
If a story already has story points set (any value other than 0 or
0.42), do not override them.

## General rules

- Story descriptions must include acceptance criteria as a checklist.
- Only produce stories for categories that are listed in the
  "Enabled categories" section of the prompt.
- Do NOT produce docs or QE stories unless genuinely warranted by the
  epic's content.

Return your answer as JSON matching the provided schema.
"""


def build_story_composition_prompt(
    analysis: dict[str, Any],
    *,
    categories: list[str] | None = None,
    category_guidance: dict[str, Any] | None = None,
    story_points_guidance: str = "",
) -> str:
    """Build the user-message prompt from a ``build_analysis_result`` dict.

    The prompt embeds the full analysis evidence so the LLM can write
    epic-specific rationale for each story.

    Parameters:
    - analysis: dict from build_analysis_result
    - categories: which categories the LLM should produce stories for
    - category_guidance: per-category trigger/criteria from config
    - story_points_guidance: free-text sizing guidance from config
    """
    parts: list[str] = []

    parts.append(
        f"# Epic: {analysis['epic_key']} — {analysis['epic_summary']}"
    )
    parts.append("")

    if analysis.get("epic_description"):
        parts.append("## Epic description")
        parts.append(analysis["epic_description"])
        parts.append("")

    children = analysis.get("child_issues", [])
    if children:
        parts.append(f"## Child issues ({len(children)})")
        for c in children:
            parts.append(f"- **{c['key']}**: {c['summary']}")
            if c.get("description"):
                desc_short = c["description"][:300]
                parts.append(f"  {desc_short}")
        parts.append("")

    keywords = analysis.get("domain_keywords", [])
    if keywords:
        parts.append(f"## Domain keywords: {', '.join(keywords)}")
        parts.append("")

    if categories:
        parts.append(f"## Enabled categories: {', '.join(categories)}")
        parts.append("")

    gaps = analysis.get("gaps", [])
    if gaps:
        parts.append(f"## Observability gaps: {', '.join(gaps)}")
        parts.append("")

    proposals = analysis.get("proposals", {})
    if proposals:
        parts.append("## Analysis findings (existing + proposed)")
        parts.append("")
        parts.append("```json")
        parts.append(_capped_json(proposals, _PROPOSALS_MAX_CHARS, label="proposals"))
        parts.append("```")
        parts.append("")

    targets = analysis.get("dashboard_targets", [])
    if targets:
        parts.append("## Target dashboards for new panels")
        for t in targets:
            parts.append(f"- {t}")
        parts.append("")

    telemetry = analysis.get("telemetry_suggestions", [])
    if telemetry:
        parts.append("## Telemetry candidates (not on CMO allowlist)")
        parts.append("```json")
        parts.append(_capped_json(telemetry, _TELEMETRY_MAX_CHARS, label="telemetry items"))
        parts.append("```")
        parts.append("")

    if category_guidance:
        parts.append("## Category guidance")
        parts.append("")
        for cat_name, guidance in category_guidance.items():
            parts.append(f"### {cat_name}")
            if guidance.get("trigger"):
                parts.append(
                    f"- **When to create:** {guidance['trigger']}"
                )
            if guidance.get("story_prefix"):
                parts.append(
                    f"- **Title prefix:** {guidance['story_prefix']}"
                )
            criteria = guidance.get("acceptance_criteria", [])
            if criteria:
                parts.append("- **Acceptance criteria:**")
                for ac in criteria:
                    parts.append(f"  - {ac}")
            parts.append("")

    if story_points_guidance:
        parts.append("## Story point estimation")
        parts.append("")
        parts.append(story_points_guidance)
        parts.append("")

    parts.append("## Instructions")
    parts.append("")
    parts.append(
        "For each observability gap listed above, compose a Jira story "
        "with a clear summary and a detailed description. The description "
        "must explain *why* the proposed change matters for this specific "
        "feature, reference existing artifacts where relevant, and include "
        "acceptance criteria as a markdown checklist."
    )
    parts.append("")
    parts.append(
        "If 'docs' is an enabled category and this epic changes "
        "user-facing behavior, compose a docs story. If 'qe' is enabled, "
        "review every child story listed above and compose QE stories "
        "that cover all testable changes. Reference the specific child "
        "story keys (e.g. CNV-xxxxx) that each QE story covers. Only "
        "create docs/QE stories when genuinely warranted."
    )
    parts.append("")
    parts.append(
        "Estimate story points for every story using the Fibonacci scale "
        "(1, 2, 3, 5, 8, 13)."
    )
    parts.append("")
    parts.append("Return JSON matching this schema:")
    parts.append("```json")
    parts.append(json.dumps(STORY_JSON_SCHEMA, indent=2))
    parts.append("```")

    return "\n".join(parts)


SP_ESTIMATION_SYSTEM_PROMPT = """\
You are an experienced engineering lead specializing in KubeVirt / CNV.
Your job is to estimate story points for Jira stories that have not
been sized yet.

Estimate on the Fibonacci scale (1, 2, 3, 5, 8, 13) based on
implementation complexity:
- 1 = trivial (typo fix, config change)
- 2 = small (straightforward, single-file change)
- 3 = medium (moderate complexity, a few files)
- 5 = large (significant work, cross-component)
- 8 = very large (major feature, multi-sprint)
- 13 = epic-sized (should probably be split)

Do NOT estimate bugs — only stories.
Base your estimate on the story summary and description, considering
the epic context provided.

Return your answer as JSON matching the provided schema.
"""


def build_sp_estimation_prompt(
    epic_summary: str,
    epic_description: str,
    stories: list[dict[str, str]],
    story_points_guidance: str = "",
) -> str:
    """Build a prompt asking the LLM to estimate SP for unsized stories.

    Parameters:
    - epic_summary: the parent epic's summary
    - epic_description: the parent epic's description
    - stories: list of dicts with keys: key, summary, description
    - story_points_guidance: free-text sizing guidance from config
    """
    parts: list[str] = []

    parts.append(f"# Epic context: {epic_summary}")
    parts.append("")
    if epic_description:
        parts.append(epic_description[:1000])
        parts.append("")

    parts.append(f"## Stories to estimate ({len(stories)})")
    parts.append("")
    for s in stories:
        parts.append(f"### {s['key']}: {s['summary']}")
        if s.get("description"):
            desc = s["description"][:500]
            parts.append(desc)
        parts.append("")

    if story_points_guidance:
        parts.append("## Sizing guidance")
        parts.append("")
        parts.append(story_points_guidance)
        parts.append("")

    parts.append("Return JSON matching this schema:")
    parts.append("```json")
    parts.append(json.dumps(SP_ESTIMATION_JSON_SCHEMA, indent=2))
    parts.append("```")

    return "\n".join(parts)

"""Prompt templates for LLM-assisted story composition.

Used by both the MCP ``@server.prompt()`` endpoint and the standalone
agent planner so that both workflows produce identically-structured
stories.  Supports pluggable categories (observability, docs, QE) and
LLM-estimated story points via injected guidance from config.
"""

from __future__ import annotations

import json
import re
from typing import Any

from schemas.stories import SP_ESTIMATION_JSON_SCHEMA, STORY_JSON_SCHEMA

_PROPOSALS_MAX_CHARS = 4000
_TELEMETRY_MAX_CHARS = 2000
_MAX_EXISTING_ITEMS_PER_CATEGORY = 5


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
        lo, hi = 0, total
        while lo < hi:
            mid = (lo + hi + 1) // 2
            attempt = json.dumps(data[:mid], indent=2, default=str)
            if len(attempt) <= max_chars:
                lo = mid
            else:
                hi = mid - 1
        if lo == 0:
            return f"[]  // (truncated — {total} total {label})"
        attempt = json.dumps(data[:lo], indent=2, default=str)
        return (
            attempt.rstrip()
            + f"\n// (truncated — {total} total {label},"
            + f" showing top {lo})"
        )

    if isinstance(data, dict):
        total = len(data)
        keys = list(data.keys())
        lo, hi = 0, total
        while lo < hi:
            mid = (lo + hi + 1) // 2
            subset_dict = {k: data[k] for k in keys[:mid]}
            attempt = json.dumps(subset_dict, indent=2, default=str)
            if len(attempt) <= max_chars:
                lo = mid
            else:
                hi = mid - 1
        if lo == 0:
            return f"{{}}  // (truncated — {total} total {label})"
        subset_dict = {k: data[k] for k in keys[:lo]}
        attempt = json.dumps(subset_dict, indent=2, default=str)
        return (
            attempt.rstrip()
            + f"\n// (truncated — {total} total {label},"
            + f" showing top {lo})"
        )

    return full[:max_chars] + f"\n// (truncated — full output was {len(full)} chars)"


def _trim_existing_items(
    proposals: dict[str, Any],
    max_per_category: int = _MAX_EXISTING_ITEMS_PER_CATEGORY,
) -> dict[str, Any]:
    """Return a copy of *proposals* with existing items capped.

    Keeps all proposed (new) items intact but limits existing
    (reference) items to *max_per_category* to save tokens.
    """
    trimmed: dict[str, Any] = {}
    for category, data in proposals.items():
        if not isinstance(data, dict):
            trimmed[category] = data
            continue
        existing = data.get("existing", [])
        total = len(existing)
        capped = existing[:max_per_category]
        if total > max_per_category:
            capped.append({
                "_note": f"{total - max_per_category} more existing "
                         f"items omitted for brevity",
            })
        trimmed[category] = {
            "existing": capped,
            "proposed": data.get("proposed", []),
        }
    return trimmed


_JIRA_HEADING_RE = re.compile(r'^h[1-6]\.\s*', re.MULTILINE)
_JIRA_LINK_RE = re.compile(
    r'\[([^|]*?)\|([^]]*?)(?:\|[^]]*?)?\]',
)
_JIRA_BOLD_RE = re.compile(r'\*(\S[^*]*\S|\S)\*')
_JIRA_PANEL_RE = re.compile(
    r'\{(?:panel|code|noformat|quote)[^}]*\}', re.IGNORECASE,
)


def strip_jira_markup(text: str) -> str:
    """Convert Jira wiki markup to plain text.

    Handles headings (h1. … h6.), links, bold, and panel/code macros.
    """
    text = _JIRA_HEADING_RE.sub('', text)
    text = _JIRA_LINK_RE.sub(r'\1', text)
    text = _JIRA_BOLD_RE.sub(r'\1', text)
    text = _JIRA_PANEL_RE.sub('', text)
    return text.strip()


SYSTEM_PROMPT = """\
You are an SRE lead for KubeVirt/CNV. Compose Jira stories for a \
feature epic.

Rules:
- Observability stories: explain *why* instrumentation matters, \
reference existing codebase artifacts, use \
kubevirt_<component>_<noun>_<unit> naming, CamelCase alert names.
- Docs stories: only when epic changes user-facing behavior/APIs.
- QE stories: reference child story keys, cover happy path + edge \
cases + failure modes. Only when genuinely testable changes exist.
- Story points: Fibonacci (1,2,3,5,8,13) by complexity. No SP on bugs.
- Include acceptance criteria as a checklist in every story description.
- Only produce stories for enabled categories. Skip docs/QE unless \
warranted.

Return JSON matching the provided schema.
"""


def build_story_composition_prompt(
    analysis: dict[str, Any],
    *,
    categories: list[str] | None = None,
    category_guidance: dict[str, Any] | None = None,
    story_points_guidance: str = "",
    include_schema: bool = False,
) -> str:
    """Build the user-message prompt from a ``build_analysis_result`` dict.

    Parameters:
    - analysis: dict from build_analysis_result
    - categories: which categories the LLM should produce stories for
    - category_guidance: per-category trigger/criteria from config
    - story_points_guidance: free-text sizing guidance from config
    - include_schema: embed the JSON schema in the prompt body.
      Set False (default) when the caller passes ``response_format``
      to the LLM, avoiding ~300 tokens of duplication.
    """
    parts: list[str] = []

    parts.append(
        f"# Epic: {analysis['epic_key']} — {analysis['epic_summary']}"
    )
    parts.append("")

    if analysis.get("epic_description"):
        parts.append("## Epic description")
        parts.append("---BEGIN EPIC DESCRIPTION---")
        parts.append(strip_jira_markup(analysis["epic_description"]))
        parts.append("---END EPIC DESCRIPTION---")
        parts.append("")

    children = analysis.get("child_issues", [])
    if children:
        parts.append(f"## Child issues ({len(children)})")
        parts.append("---BEGIN CHILD ISSUES---")
        for c in children:
            parts.append(f"- **{c['key']}**: {c['summary']}")
            if c.get("description"):
                desc_short = strip_jira_markup(c["description"])[:300]
                parts.append(f"  {desc_short}")
        parts.append("---END CHILD ISSUES---")
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
        trimmed = _trim_existing_items(proposals)
        parts.append("## Analysis findings (existing + proposed)")
        parts.append("")
        parts.append("```json")
        parts.append(_capped_json(
            trimmed, _PROPOSALS_MAX_CHARS, label="proposals",
        ))
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
        parts.append(_capped_json(
            telemetry, _TELEMETRY_MAX_CHARS, label="telemetry items",
        ))
        parts.append("```")
        parts.append("")

    if category_guidance:
        parts.append("## Category guidance")
        parts.append("")
        for cat_name, guidance in category_guidance.items():
            trigger = guidance.get("trigger", "")
            prefix = guidance.get("story_prefix", "")
            line = f"- **{cat_name}**: "
            if trigger:
                line += trigger
            if prefix:
                line += f" (prefix: {prefix})"
            parts.append(line)
        parts.append("")

    if story_points_guidance:
        parts.append(f"## Story points: {story_points_guidance}")
        parts.append("")

    parts.append(
        "Compose one story per gap. For docs/QE, reference child "
        "story keys. Return JSON."
    )

    if include_schema:
        parts.append("")
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
    include_schema: bool = False,
) -> str:
    """Build a prompt asking the LLM to estimate SP for unsized stories.

    Parameters:
    - epic_summary: the parent epic's summary
    - epic_description: the parent epic's description
    - stories: list of dicts with keys: key, summary, description
    - story_points_guidance: free-text sizing guidance from config
    - include_schema: embed the JSON schema in the prompt body.
      Set False (default) when the caller passes ``response_format``.
    """
    parts: list[str] = []

    parts.append(f"# Epic context: {epic_summary}")
    parts.append("")
    if epic_description:
        parts.append("---BEGIN EPIC DESCRIPTION---")
        parts.append(strip_jira_markup(epic_description)[:1000])
        parts.append("---END EPIC DESCRIPTION---")
        parts.append("")

    parts.append(f"## Stories to estimate ({len(stories)})")
    parts.append("")
    for s in stories:
        parts.append(f"### {s['key']}: {s['summary']}")
        if s.get("description"):
            desc = strip_jira_markup(s["description"])[:500]
            parts.append(desc)
        parts.append("")

    if story_points_guidance:
        parts.append(f"## Sizing: {story_points_guidance}")
        parts.append("")

    parts.append("Return JSON.")

    if include_schema:
        parts.append("")
        parts.append("```json")
        parts.append(json.dumps(SP_ESTIMATION_JSON_SCHEMA, indent=2))
        parts.append("```")

    return "\n".join(parts)

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
You are an SRE lead for KubeVirt/CNV advising cluster operators \
who run production OpenShift Virtualization environments.

It is perfectly fine to return an empty stories list if the \
epic does not warrant new work. Do NOT invent stories just \
to fill a gap. Internal refactoring epics typically need \
zero new observability stories.

Backlog / umbrella epics (e.g. "Metrics backlog 4.23") are \
organizational containers — do NOT create dashboards, alerts, \
or metrics for the backlog concept itself. Instead, examine \
the child stories and propose QE or docs stories for specific \
items that need them.

Do NOT duplicate work already tracked as child issues. \
If a child story's summary already describes the metrics, \
alerts, dashboards, recording rules, or observability work \
you would propose, that work is already planned — do NOT \
propose a new story for it. Only propose QE or docs stories \
that complement existing children when genuinely needed.

If the epic has a **no-doc** label, skip docs stories. \
If the epic has a **no-qe** label, skip QE stories.

Rules:
- Story points: Fibonacci (1,2,3,5,8,13) by complexity. \
No SP on bugs or closed stories.
- Include acceptance criteria as a checklist in every story.
- Only produce stories for enabled categories.

Return JSON matching the provided schema.
"""


_OBSERVABILITY_RULES = """\
Observability story rules:
- Think from the perspective of real customers. Every metric, \
alert, and dashboard must serve: troubleshooting, capacity \
planning, health assessment, or operator decision-making.
- Story descriptions MUST include these sections: \
**Why this matters** (the real-world problem), \
**Who benefits** (operator / SRE / virt-operator), \
**How it is used** (alert threshold, dashboard panel, \
operator decision). This detail is required only for \
observability stories — not for QE or docs.
- Use kubevirt_<component>_<noun>_<unit> naming, CamelCase \
alert names.
- Alerts MUST be backed by a concrete metric. Name the metric. \
Every alert must have a clear actionable response — "low \
utilization" is a dashboard insight, NOT an alert.
- Dashboards must serve real operator workflows. Prefer adding \
panels to existing dashboards. Every panel MUST reference \
specific metrics.
- Do NOT propose presence-check alerts, dashboards for internal \
component health, or items only useful to test pipelines.
- Before proposing any new item, check the **existing** items \
in the "Analysis findings" section:
  * **Alerts**: if an existing alert already covers the same \
condition or a closely related one (e.g. unhealthy VM status), \
do NOT propose a duplicate or overlapping alert.
  * **Metrics**: if an existing metric already tracks the same \
measurement (even via a different label combination), do NOT \
propose a new metric. If the need is for aggregation or a \
different view of existing data, propose a **recording rule** \
instead of a new metric.
  * **Dashboards / panels**: if an existing dashboard panel \
already visualizes the data you would propose, do NOT propose \
a new panel for it. Check panel names and PromQL queries.
- When assessing resource health (e.g. VM status), consider \
the full picture: existing metrics AND existing alerts. Do \
not propose new metrics for data that can be derived from \
existing metrics via PromQL or recording rules.
- Only propose metrics and alerts for resources the component \
**owns or directly controls**. If the component merely reads \
an external CR or config (e.g. an OpenShift platform CR like \
APIServer, Infrastructure, or Network), do NOT propose alerts \
or metrics for unexpected values in that external resource — \
that is the responsibility of the platform operator, not \
KubeVirt/CNV.
"""

_DOCS_RULES = """\
Docs story rules:
- Only when the epic introduces a new user-facing feature, \
changes behavior, renames concepts, or modifies APIs/CLI/UI.
- Internal refactoring or backend-only changes do NOT need docs.
- Description format: a short plain-text paragraph describing \
what needs to be documented, followed by an acceptance \
criteria checklist. Nothing else. Do NOT add sections like \
"Why this is needed", "Proposed changes", "Who benefits", \
"How it is used", or any other headings.
"""

_QE_RULES = """\
QE story rules:
- Take a QE engineer role. Split by test type / scope. \
Group same-type tests into one story. Reference child \
story keys.
- Description format: a checklist of specific test cases \
to verify. Nothing else. Do NOT add sections like \
"Why this is needed", "Proposed changes", "Who benefits", \
"How it is used", or any other headings.
- Distinguish new vs. migrated items: moved/refactored items \
(same names) already have tests — only propose end-to-end \
and upgrade/rollback verification. Renamed metrics need a \
story to update existing tests. Only propose unit tests for \
genuinely new metrics/alerts.
- Test categories: metric unit tests (new only), alert rule \
tests (new only), dashboard verification, end-to-end pipeline, \
upgrade/rollback verification.
- Do NOT create a single monolithic QE story.
"""


_OBSERVABILITY_CATEGORIES = frozenset({
    "metrics", "alerts", "dashboards", "telemetry",
})


def get_system_prompt(categories: list[str] | None = None) -> str:
    """Assemble the system prompt with category-specific rules."""
    cats = set(categories or [])
    parts = [SYSTEM_PROMPT]
    if cats & _OBSERVABILITY_CATEGORIES:
        parts.append(_OBSERVABILITY_RULES)
    if "docs" in cats:
        parts.append(_DOCS_RULES)
    if "qe" in cats:
        parts.append(_QE_RULES)
    return "\n".join(parts)


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

    epic_components = analysis.get("epic_components", [])
    if epic_components:
        parts.append(f"## Epic components: {', '.join(epic_components)}")
        parts.append("")

    epic_labels = analysis.get("epic_labels", [])
    if epic_labels:
        parts.append(f"## Epic labels: {', '.join(epic_labels)}")
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
        "Based on the analysis above, propose stories only where "
        "genuinely needed. It is fine to return an empty list if "
        "no new work is warranted. For docs/QE stories, reference "
        "the relevant child story keys. Return JSON."
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

Do NOT estimate bugs or closed stories — only open stories.
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


CLARITY_CHECK_SYSTEM_PROMPT = """\
You are an engineering lead reviewing Jira epics before sprint \
planning. Your job is to decide whether an epic has enough detail \
for a team to start breaking it into implementation stories.

An epic is **clear** when:
- The goal / desired outcome is stated
- The scope is bounded (you can tell what is and isn't included)
- There is enough context (description or child stories) to \
understand what needs to be built or changed

An epic **needs grooming** when:
- The summary is vague and the description is missing or generic \
(e.g. "Improve observability" with no specifics)
- You cannot determine what components, repos, or subsystems \
are involved
- There are no child stories to clarify scope, and the \
description does not compensate

Recurring backlog / umbrella epics (e.g. "Metrics backlog 4.23", \
"Observability backlog") are common and valid. Their description \
may be intentionally generic because the **child stories define \
the scope**. If an epic has a clear topic in its summary and \
child stories that are related to that topic, mark it as clear. \
Mixed or unrelated children do not make it unclear — the LLM \
story composition step will focus on the relevant ones.

Be pragmatic: a short but specific description is fine. A long \
but hand-wavy description is not.

Return JSON matching the provided schema.
"""


CLARITY_CHECK_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdict": {
            "type": "string",
            "enum": ["clear", "needs_grooming"],
        },
        "reason": {
            "type": "string",
            "description": (
                "One or two sentences explaining why the epic is "
                "clear or what detail is missing."
            ),
        },
    },
    "required": ["verdict", "reason"],
    "additionalProperties": False,
}


def build_clarity_check_prompt(
    epic_key: str,
    epic_summary: str,
    epic_description: str,
    children: list[dict[str, str]],
) -> str:
    """Build a prompt asking the LLM whether an epic is clear."""
    parts: list[str] = []

    parts.append(f"# Epic: {epic_key} — {epic_summary}")
    parts.append("")

    if epic_description:
        parts.append("## Description")
        parts.append("---BEGIN EPIC DESCRIPTION---")
        parts.append(strip_jira_markup(epic_description)[:2000])
        parts.append("---END EPIC DESCRIPTION---")
        parts.append("")
    else:
        parts.append("## Description")
        parts.append("*(no description)*")
        parts.append("")

    if children:
        parts.append(f"## Child issues ({len(children)})")
        for c in children[:20]:
            parts.append(f"- **{c['key']}**: {c['summary']}")
            if c.get("description"):
                desc = strip_jira_markup(c["description"])[:200]
                parts.append(f"  {desc}")
        if len(children) > 20:
            parts.append(f"  *(... {len(children) - 20} more)*")
        parts.append("")
    else:
        parts.append("## Child issues")
        parts.append("*(none)*")
        parts.append("")

    parts.append(
        "Is this epic clear enough for a team to start creating "
        "implementation stories? Return your verdict as JSON."
    )

    return "\n".join(parts)

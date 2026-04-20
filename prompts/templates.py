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

Domain context — Jira component to source repository mapping:
- "CNV Virtualization" → kubevirt/kubevirt
- "CNV Install, Upgrade and Operators" → \
kubevirt/hyperconverged-cluster-operator, kubevirt/monitoring, \
and the observability layer of the other repositories
- "CNV Infrastructure" → kubevirt/hostpath-provisioner, \
kubevirt/hostpath-provisioner-operator
- "CNV Storage" → kubevirt/containerized-data-importer

Alert and recording-rule placement — where observability \
artifacts live depends on the metric prefix, NOT the epic's \
Jira component:
- kubevirt_vmi_* metrics, alerts, and recording rules → \
kubevirt/kubevirt (they ship with virt-launcher / virt-controller)
- kubevirt_hco_* metrics and alerts → \
kubevirt/hyperconverged-cluster-operator
- kubevirt_hpp_* metrics and alerts → \
kubevirt/hostpath-provisioner
- kubevirt_cdi_* / kubevirt_import_* metrics and alerts → \
kubevirt/containerized-data-importer
- kubevirt/monitoring contains the monitoring operator itself \
and dashboards — NOT alert rules for other components. \
Do NOT place alert rules in kubevirt/monitoring unless the \
alert is about the monitoring operator's own health.

Use the epic's component and associated repositories to \
understand the feature scope, but always use the metric-prefix \
rules above to determine where alerts, recording rules, and \
dashboards should be defined.

Think from the perspective of real customers operating clusters \
at scale. Every metric, alert, and dashboard you propose must \
serve at least one of these real-world use cases:
- **Troubleshooting:** helps operators diagnose a live incident \
(e.g. "VM GPU is underperforming — is it a driver issue or \
resource contention?")
- **Capacity planning:** helps operators forecast resource needs \
(e.g. "GPU utilization trending toward saturation across nodes")
- **Health assessment:** gives operators a quick signal on \
overall cluster/feature health (e.g. "GPU passthrough success \
rate over the last 24h")
- **Autopilot / operator decision-making:** enables the \
OpenShift Virtualization operator itself to make automated \
scheduling or remediation decisions

It is perfectly fine to return an empty stories list if the \
epic does not warrant new observability work. Not every epic \
needs new metrics, alerts, or dashboards. Internal refactoring \
epics (e.g. moving code between repos, restructuring how \
metrics are generated without changing the metrics themselves) \
typically need zero new observability stories — the existing \
metrics, alerts, and dashboards should continue working \
unchanged. Do NOT invent stories just to fill a gap.

Do NOT propose:
- "Presence check" alerts that merely verify a metric exists \
or a component is running — those belong in QE/CI, not \
production alerting.
- Alerts or dashboards for features that not every cluster \
uses (e.g. GPU) unless the alert fires only when the feature \
is actively enabled and in use.
- Observability items whose only consumer would be a test \
pipeline rather than a human operator or the virt operator.
- Stories for epics that only restructure internal code without \
changing user-facing metrics, APIs, or behavior — unless the \
restructuring genuinely introduces new failure modes that \
operators need visibility into.

Rules:
- Observability stories: explain *why* the instrumentation \
matters and *who* benefits (operator, virt-operator, SRE). \
Reference existing codebase artifacts, use \
kubevirt_<component>_<noun>_<unit> naming, CamelCase alert names.
- Alerts MUST be backed by a concrete metric (existing or \
proposed in this epic). Name the metric in the description. \
Do NOT propose alerts for metrics that don't exist yet unless \
the same run also proposes those metrics.
- Every alert must have a clear actionable response — the \
operator must be able to do something concrete when it fires \
(investigate, scale, restart, reallocate). "Low utilization" \
or "resource is idle" is a capacity-planning insight best \
surfaced on a dashboard, NOT an alert. Only propose an alert \
when the condition requires timely human or automated action.
- Dashboards must serve a real operator workflow — ask "would a \
customer open this dashboard during an incident, capacity \
planning session, or health review?" Do NOT propose dashboards \
for internal component internals (e.g. "controller health") \
that no customer would look at. Prefer adding panels to \
existing dashboards (e.g. VM Overview, Storage Overview) over \
creating new standalone dashboards. A new dashboard is only \
justified when the feature introduces a genuinely new domain \
(e.g. GPU workloads) that doesn't fit any existing dashboard. \
Every dashboard story MUST reference specific metrics or \
recording rules — do NOT propose generic panels without naming \
the metrics they will visualize.
- Do NOT duplicate work already tracked as child issues of the \
source epic. If a child issue already covers a topic, skip it.
- For each observability story description, include these sections: \
**Why this matters** (the real-world problem it solves), \
**Who benefits** (cluster operator / SRE / virt-operator), \
**How it is used** (concrete scenario: alert threshold, dashboard \
panel, operator decision).
- Docs stories: only when the epic introduces a new user-facing \
feature, changes user-facing behavior, renames concepts, or \
modifies APIs/CLI/UI. Internal refactoring, code moves between \
repos, or backend-only changes do NOT need docs stories.
- If the epic has a **no-doc** label, do NOT produce any docs \
stories. If the epic has a **no-qe** label, do NOT produce \
any QE stories. These labels override all other guidance.
- QE stories: take a QE engineer role. Split QE work into \
multiple stories by test type / scope. Group related tests of \
the same type into one story (e.g. one story for all new metric \
unit tests, one for alert rule validation, one for dashboard \
panel verification, one for end-to-end manual verifications). \
Each QE story description must list the specific test cases \
as checklist items.
Important: distinguish between **new** and **migrated** \
observability items:
  * Metrics/alerts/dashboards that are being **moved or \
refactored** (same names, same behavior, different component) \
are assumed to already have automated tests. Do NOT propose \
new unit tests for them — only propose end-to-end and \
upgrade/rollback verification to ensure nothing broke.
  * If a metric is **renamed** during migration, propose a \
story to update existing automated tests and any alert \
expressions that reference the old name.
  * Only propose metric unit tests and alert rule tests for \
**genuinely new** metrics or alerts that did not exist before.
Consider these test categories:
  * **Metric unit tests** (automated): ONLY for genuinely new \
metrics. Verify registration, exposure on /metrics, correct \
type/labels, and expected values under known conditions.
  * **Alert rule tests** (automated): ONLY for genuinely new \
alerts. Verify PrometheusRule fires correctly against sample \
data, severity and runbook_url are set, alert resolves when \
condition clears.
  * **Dashboard verification** (manual + automated): verify \
panels render with sample data, queries return expected results, \
no broken panels after upgrade.
  * **End-to-end pipeline tests** (manual): verify the full \
observability pipeline works in a real cluster — metrics \
scraped, alerts fire, dashboards populated. This is the key \
QE story for architectural changes and migrations.
  * **Upgrade/rollback verification** (manual): verify \
metrics/alerts/dashboards survive an upgrade and rollback \
without data loss or broken panels.
Do NOT create a single monolithic QE story. Reference the \
specific observability child story keys each QE story validates.
- Story points: Fibonacci (1,2,3,5,8,13) by complexity. No SP \
on bugs or closed stories.
- Include acceptance criteria as a checklist in every story \
description.
- Only produce stories for enabled categories. Skip docs/QE \
unless warranted.

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

    epic_components = analysis.get("epic_components", [])
    if epic_components:
        parts.append(f"## Epic components: {', '.join(epic_components)}")
        parts.append("")

    associated_repos = analysis.get("associated_repos", [])
    if associated_repos:
        parts.append(
            "## Associated source repositories: "
            + ", ".join(associated_repos)
        )
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

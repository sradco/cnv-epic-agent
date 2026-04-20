"""Human-readable formatting for analysis results and Jira story templates."""

from __future__ import annotations

from typing import Any


def format_analysis_result(result: dict[str, Any]) -> str:
    """Render the full analysis result as human-readable markdown."""
    lines: list[str] = []

    lines.append(f"# Observability Analysis: {result['epic_key']}")
    lines.append("")
    lines.append(f"**Epic:** {result['epic_key']} — {result['epic_summary']}")
    lines.append("")

    # Decision
    state = result["need_state"]
    confidence = result["need_confidence"]
    state_icon = {"needed": "YES", "not_needed": "NO", "uncertain": "UNCERTAIN"}.get(
        state, state.upper()
    )
    lines.append(f"## Monitoring needed: {state_icon} (confidence: {confidence})")
    lines.append("")

    # Why
    needed_ev = result.get("need_evidence", {}).get("needed", [])
    not_needed_ev = result.get("need_evidence", {}).get("not_needed", [])
    if needed_ev:
        lines.append("### Why monitoring is needed")
        lines.append("")
        for ev in needed_ev:
            matches_str = ", ".join(ev["matches"])
            lines.append(f"- **{ev['issue_key']}** matched: {matches_str}")
        lines.append("")
    if not_needed_ev:
        lines.append("### Signals suggesting monitoring may not be needed")
        lines.append("")
        for ev in not_needed_ev:
            matches_str = ", ".join(ev["matches"])
            lines.append(f"- **{ev['issue_key']}** matched: {matches_str}")
        lines.append("")

    # Existing coverage
    coverage = result.get("coverage", {})
    lines.append("## Existing observability coverage")
    lines.append("")
    for category, data in coverage.items():
        status = "FOUND" if data.get("present") else "MISSING"
        lines.append(f"- **{category}**: {status}")
        for m in data.get("matches", []):
            lines.append(f"  - {m['issue_key']}: {', '.join(m['matches'])}")
    lines.append("")

    # Gaps
    gaps = result.get("gaps", [])
    if gaps:
        lines.append("## Gaps (missing observability)")
        lines.append("")
        for gap in gaps:
            lines.append(f"- {gap}")
        lines.append("")

    # Feature types detected
    ftypes = result.get("feature_types", [])
    if ftypes:
        lines.append(f"## Feature types detected: {', '.join(ftypes)}")
        lines.append("")

    # Proposals (two-section: existing + proposed)
    proposals = result.get("proposals", {})
    if proposals:
        lines.append("## What we should add")
        lines.append("")
        for category, data in proposals.items():
            lines.append(f"### {category}")
            lines.append("")
            if isinstance(data, dict):
                existing = data.get("existing", [])
                proposed = data.get("proposed", [])
                if existing:
                    lines.append("**Existing related items (reference):**")
                    lines.append("")
                    for item in existing:
                        name = item.get("name", "")
                        rationale = item.get("rationale", "")
                        lines.append(f"- `{name}` — {rationale}")
                    lines.append("")
                if proposed:
                    lines.append("**Proposed new items:**")
                    lines.append("")
                    for item in proposed:
                        hint = item.get("name_hint") or item.get("panel_hint", "")
                        rationale = item.get("rationale", "")
                        user_action = item.get("user_action", "")
                        lines.append(f"- `{hint}` — {rationale}")
                        if user_action:
                            lines.append(f"  - *How to use:* {user_action}")
                    lines.append("")
                if not existing and not proposed:
                    lines.append(
                        "- (no existing or proposed items discovered)")
                    lines.append("")
            else:
                for item in data:
                    lines.append(f"- {item}")
                lines.append("")

    # Dashboard targets
    dashboard_targets = result.get("dashboard_targets", [])
    if dashboard_targets:
        lines.append("## Target dashboards for new panels")
        lines.append("")
        for target in dashboard_targets:
            lines.append(f"- {target}")
        lines.append("")

    # Telemetry suggestions
    telemetry = result.get("telemetry_suggestions", [])
    if telemetry:
        lines.append("## Telemetry candidates (not on CMO allowlist)")
        lines.append("")
        lines.append(
            "Cluster-level recording rules discovered in code that are not "
            "yet on the CMO telemetry allowlist:"
        )
        lines.append("")
        lines.append("| Name | PromQL | Why collect | Repo | File |")
        lines.append("|------|--------|------------|------|------|")
        for s in telemetry:
            expr_short = s["expr"][:60] + ("..." if len(s["expr"]) > 60 else "")
            rationale = s.get("rationale", "")
            lines.append(
                f"| `{s['name']}` | `{expr_short}` "
                f"| {rationale} "
                f"| {s.get('repo', '')} | {s.get('file', '')} |"
            )
        lines.append("")

    # Recommended action
    action = result.get("recommended_action", "skip")
    lines.append(f"## Recommended action: {action}")
    lines.append("")
    count = result.get("would_create_count", 0)
    if count:
        lines.append(f"Would create **{count}** Jira story(ies).")
        lines.append("")
        lines.append(
            f'Run dry-run: `create_stories(epic_key="{result["epic_key"]}", version="<VERSION>", dry_run=True)`'
        )
        lines.append(
            f'Apply: `create_stories(epic_key="{result["epic_key"]}", version="<VERSION>", dry_run=False)`'
        )
    else:
        lines.append("No new stories needed.")

    return "\n".join(lines)


def format_scan_table(results: list[dict[str, Any]]) -> str:
    """Render a markdown summary table for multiple epics."""
    lines: list[str] = []
    lines.append("| Epic | Summary | Need | Metrics | Alerts | Dashboards | Action |")
    lines.append("|------|---------|------|---------|--------|------------|--------|")

    for r in results:
        cov = r.get("coverage", {})
        m = "yes" if cov.get("metrics", {}).get("present") else "**NO**"
        a = "yes" if cov.get("alerts", {}).get("present") else "**NO**"
        d = "yes" if cov.get("dashboards", {}).get("present") else "**NO**"
        need = r.get("need_state", "?")
        action = r.get("recommended_action", "skip")
        summary_short = r.get("epic_summary", "")[:50]
        lines.append(
            f"| {r['epic_key']} | {summary_short} | {need} | {m} | {a} | {d} | {action} |"
        )

    return "\n".join(lines)


def render_subtask_description(
    template: str,
    epic_key: str,
    epic_summary: str,
    why_bullets: list[str],
    scope_bullets: list[str],
    proposal_data: dict[str, list[dict[str, str]]] | list[str] | None = None,
    extra_fields: dict[str, str] | None = None,
    category: str = "",
) -> str:
    """Render a Jira sub-task description from a template.

    ``proposal_data`` can be the new two-section dict
    (``{"existing": [...], "proposed": [...]}``) or a legacy flat list.
    """
    why_section = (
        "\n".join(f"- {b}" for b in why_bullets)
        if why_bullets else "- See epic for details."
    )
    scope_section = (
        "\n".join(f"- {b}" for b in scope_bullets)
        if scope_bullets else "- See linked stories."
    )

    existing_section = ""
    proposed_section = ""

    if isinstance(proposal_data, dict):
        existing_section = _render_existing_table(
            proposal_data.get("existing", []), category,
        )
        proposed_section = _render_proposed_table(
            proposal_data.get("proposed", []), category,
        )
    elif isinstance(proposal_data, list):
        proposed_section = (
            "\n".join(f"- {item}" for item in proposal_data)
            if proposal_data
            else "- To be determined during implementation."
        )

    if not existing_section:
        existing_section = "- None discovered in current codebase."
    if not proposed_section:
        proposed_section = "- To be determined during implementation."

    fields: dict[str, str] = {
        "epic_key": epic_key,
        "epic_summary": epic_summary,
        "why_section": why_section,
        "scope_section": scope_section,
        "existing_items": existing_section,
        "proposed_items": proposed_section,
    }
    if extra_fields:
        fields.update(extra_fields)

    import string
    fmt = string.Formatter()
    result_parts: list[str] = []
    for literal, field_name, spec, conv in fmt.parse(template):
        result_parts.append(literal)
        if field_name is not None:
            result_parts.append(str(fields.get(field_name, "N/A")))
    return "".join(result_parts)


def _render_existing_table(
    items: list[dict[str, str]], category: str,
) -> str:
    """Render existing items as a markdown table."""
    if not items:
        return ""

    if category == "metrics":
        lines = ["| Metric | Type | Why relevant | Repo |",
                 "|--------|------|-------------|------|"]
        for it in items:
            lines.append(
                f"| `{it.get('name', '')}` "
                f"| {it.get('type', '')} "
                f"| {it.get('rationale', '')} "
                f"| {it.get('repo', '')} |"
            )
    elif category == "alerts":
        lines = ["| Alert | Severity | Why relevant | Repo |",
                 "|-------|----------|-------------|------|"]
        for it in items:
            lines.append(
                f"| `{it.get('name', '')}` "
                f"| {it.get('severity', '')} "
                f"| {it.get('rationale', '')} "
                f"| {it.get('repo', '')} |"
            )
    elif category == "dashboards":
        lines = ["| Panel | Dashboard | Why relevant | Repo |",
                 "|-------|-----------|-------------|------|"]
        for it in items:
            lines.append(
                f"| `{it.get('name', '')}` "
                f"| {it.get('dashboard', '')} "
                f"| {it.get('rationale', '')} "
                f"| {it.get('repo', '')} |"
            )
    else:
        lines = ["| Name | Why relevant | Repo |",
                 "|------|-------------|------|"]
        for it in items:
            lines.append(
                f"| `{it.get('name', '')}` "
                f"| {it.get('rationale', '')} "
                f"| {it.get('repo', '')} |"
            )

    return "\n".join(lines)


def _render_proposed_table(
    items: list[dict[str, str]], category: str,
) -> str:
    """Render proposed new items as a markdown table with rationale."""
    if not items:
        return ""

    if category in ("metrics", "alerts"):
        hint_key = "name_hint"
        lines = ["| Name | Type | Why needed | How to use |",
                 "|------|------|-----------|------------|"]
        for it in items:
            lines.append(
                f"| `{it.get(hint_key, '')}` "
                f"| {it.get('type', '')} "
                f"| {it.get('rationale', '')} "
                f"| {it.get('user_action', '')} |"
            )
    elif category == "dashboards":
        lines = ["| Panel | Why needed | How to use |",
                 "|-------|-----------|------------|"]
        for it in items:
            lines.append(
                f"| `{it.get('panel_hint', '')}` "
                f"| {it.get('rationale', '')} "
                f"| {it.get('user_action', '')} |"
            )
    elif category == "telemetry":
        lines = ["| Name | PromQL | Why needed | How to use |",
                 "|------|--------|-----------|------------|"]
        for it in items:
            expr_short = it.get("expr", "")
            if len(expr_short) > 60:
                expr_short = expr_short[:60] + "..."
            lines.append(
                f"| `{it.get('name_hint', '')}` "
                f"| `{expr_short}` "
                f"| {it.get('rationale', '')} "
                f"| {it.get('user_action', '')} |"
            )
    else:
        lines = ["| Name | Why needed | How to use |",
                 "|------|-----------|------------|"]
        for it in items:
            lines.append(
                f"| `{it.get('name_hint', it.get('name', ''))}` "
                f"| {it.get('rationale', '')} "
                f"| {it.get('user_action', '')} |"
            )

    return "\n".join(lines)


def build_subtask_payloads(
    result: dict[str, Any],
    cfg: dict[str, Any],
) -> list[dict[str, str]]:
    """Build the list of sub-task payloads from an analysis result."""
    templates = cfg.get("subtask_templates", {})
    summary_fmt = templates.get(
        "summary_format",
        "[Observability][{category}] Add {category_label} for {epic_key}: {epic_summary_short}",
    )
    category_templates = templates.get("categories", {})

    payloads: list[dict[str, str]] = []

    for category in result.get("gaps", []):
        cat_cfg = category_templates.get(category, {})
        label = cat_cfg.get("label", category)
        desc_template = cat_cfg.get("description_template", "")

        epic_summary_short = result["epic_summary"][:60]

        summary = summary_fmt.format(
            category=category,
            category_label=label,
            epic_key=result["epic_key"],
            epic_summary_short=epic_summary_short,
        )

        why_bullets = []
        for ev in result.get("need_evidence", {}).get("needed", []):
            why_bullets.append(f"{ev['issue_key']} involves: {', '.join(ev['matches'])}")

        scope_bullets = []
        for ev in result.get("need_evidence", {}).get("needed", []):
            scope_bullets.append(ev["issue_key"])

        proposal_data = result.get("proposals", {}).get(category)

        extra: dict[str, str] = {}

        if category == "dashboards":
            targets = result.get("dashboard_targets", [])
            extra["dashboard_targets"] = (
                "\n".join(f"- {t}" for t in targets) if targets
                else "- To be determined"
            )

        if category == "telemetry":
            telemetry = result.get("telemetry_suggestions", [])
            cmo_lines: list[str] = []
            for s in telemetry:
                cmo_lines.append(f"- `{{__name__=\"{s['name']}\"}}`")
            extra["cmo_entries"] = (
                "\n".join(cmo_lines) if cmo_lines
                else "- To be determined"
            )

        description = render_subtask_description(
            template=desc_template,
            epic_key=result["epic_key"],
            epic_summary=result["epic_summary"],
            why_bullets=why_bullets,
            scope_bullets=scope_bullets,
            proposal_data=proposal_data,
            extra_fields=extra,
            category=category,
        )

        payloads.append({
            "category": category,
            "summary": summary,
            "description": description,
        })

    return payloads

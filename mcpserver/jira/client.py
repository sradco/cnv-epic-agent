"""Jira REST helpers: authentication, querying, and story creation."""

from __future__ import annotations

import logging
import os
from typing import Any

from jira import JIRA

from agent.analyzer.analysis import IssueDoc

logger = logging.getLogger(__name__)

_PAGE_SIZE = 200


def _search_all(
    client: JIRA,
    jql: str,
    page_size: int = _PAGE_SIZE,
) -> list[Any]:
    """Page through all results for a JQL query.

    The jira library's ``search_issues`` caps at ``maxResults``
    per call.  This helper loops until all matching issues are
    collected.
    """
    all_issues: list[Any] = []
    start_at = 0
    while True:
        page = client.search_issues(
            jql, startAt=start_at, maxResults=page_size,
        )
        all_issues.extend(page)
        if len(page) < page_size:
            break
        start_at += page_size
    return all_issues


def get_jira_client(cfg: dict[str, Any]) -> JIRA:
    url = os.environ.get("JIRA_URL", cfg.get("jira", {}).get("url", "https://redhat.atlassian.net"))
    token = os.environ.get("JIRA_TOKEN", "")
    email = os.environ.get("JIRA_EMAIL", "")
    if not token:
        raise RuntimeError("JIRA_TOKEN environment variable is required")
    # Atlassian Cloud uses basic auth (email + API token)
    if email:
        return JIRA(server=url, basic_auth=(email, token))
    # Fallback to bearer token auth (Jira Server / Data Center)
    return JIRA(server=url, token_auth=token)


def search_epics(
    client: JIRA,
    cfg: dict[str, Any],
    project: str | None = None,
    since_days: int | None = None,
    jql: str | None = None,
) -> list[Any]:
    jira_cfg = cfg.get("jira", {})
    if jql is None:
        proj = project or jira_cfg.get("default_project", "CNV")
        days = since_days or int(jira_cfg.get("default_since_days", 30))
        template = jira_cfg.get(
            "jql_template",
            "project = {project} AND type = Epic AND created >= -{since_days}d",
        )
        jql = template.format(project=proj, since_days=days)
    return _search_all(client, jql)


def fetch_child_issues(
    client: JIRA,
    cfg: dict[str, Any],
    epic_key: str,
    project: str | None = None,
) -> list[Any]:
    jira_cfg = cfg.get("jira", {})
    proj = project or jira_cfg.get("default_project", "CNV")
    template = jira_cfg.get(
        "child_issues_jql_template",
        'project = {project} AND "Epic Link" = {epic_key}',
    )
    jql = template.format(project=proj, epic_key=epic_key)
    return _search_all(client, jql)


def fetch_epic_with_children(
    client: JIRA,
    cfg: dict[str, Any],
    epic_key: str,
) -> tuple[IssueDoc, list[IssueDoc]]:
    epic_issue = client.issue(epic_key)
    epic = IssueDoc.from_jira(epic_issue)
    children_raw = fetch_child_issues(client, cfg, epic_key)
    children = [IssueDoc.from_jira(c) for c in children_raw]
    return epic, children


def find_existing_obs_stories(
    client: JIRA,
    cfg: dict[str, Any],
    obs_epic_key: str,
    source_epic_key: str,
) -> list[dict[str, str]]:
    """Find existing observability stories related to a source epic.

    Uses three complementary searches to avoid duplicates:
    1. Stories under the obs epic with the scanner label referencing the source epic
    2. Any issue linked to the source epic with the scanner label
    3. Any issue linked to the source epic with "[Observability]" in the summary
    """
    creation_cfg = cfg.get("creation", {})
    story_label = creation_cfg.get("story_label", "epic-agent-generated")

    seen_keys: set[str] = set()
    existing: list[dict[str, str]] = []

    def _collect(issues: list[Any]) -> None:
        for issue in issues:
            if issue.key in seen_keys:
                continue
            seen_keys.add(issue.key)
            summary = str(getattr(issue.fields, "summary", "") or "")
            existing.append({"key": issue.key, "summary": summary})

    # 1. Stories under the obs epic with the scanner label
    if obs_epic_key and obs_epic_key != "(DRY-RUN)":
        jql = (
            f'"Epic Link" = {obs_epic_key} '
            f'AND labels = "{story_label}" '
            f'AND summary ~ "{source_epic_key}"'
        )
        try:
            _collect(client.search_issues(jql, maxResults=50))
        except Exception:
            logger.warning("JQL query failed (obs epic children): %s", jql, exc_info=True)

    # 2. Any issue with the scanner label referencing the source epic in summary
    jql_label = (
        f'labels = "{story_label}" '
        f'AND summary ~ "{source_epic_key}"'
    )
    try:
        _collect(client.search_issues(jql_label, maxResults=50))
    except Exception:
        logger.warning("JQL query failed (label search): %s", jql_label, exc_info=True)

    # 3. Issues linked to the source epic with "[Observability]" in the summary
    jql_linked = (
        f'issue in linkedIssues({source_epic_key}) '
        f'AND summary ~ "[Observability]"'
    )
    try:
        _collect(client.search_issues(jql_linked, maxResults=50))
    except Exception:
        logger.warning("JQL query failed (linked issues): %s", jql_linked, exc_info=True)

    return existing


def find_or_create_obs_epic(
    client: JIRA,
    cfg: dict[str, Any],
    version: str,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Find or create the version-scoped observability epic.

    Returns {"key": "CNV-XXXXX", "summary": "...", "created": bool}.
    """
    creation_cfg = cfg.get("creation", {})
    project = creation_cfg.get("project", "CNV")
    epic_label = creation_cfg.get("epic_label", "cnv-observability")
    component = creation_cfg.get("component", "CNV Install, Upgrade and Operators")
    epic_summary_fmt = creation_cfg.get(
        "epic_summary_format",
        "[Observability] CNV {version} — Auto-generated observability stories",
    )

    epic_summary = epic_summary_fmt.format(version=version)

    # Search for an existing observability epic for this version
    jql = (
        f'project = {project} AND type = Epic '
        f'AND labels = "{epic_label}" '
        f'AND summary ~ "CNV {version}"'
    )
    results = client.search_issues(jql, maxResults=5)
    if results:
        found = results[0]
        return {"key": found.key, "summary": str(found.fields.summary), "created": False}

    if dry_run:
        return {"key": "(DRY-RUN)", "summary": epic_summary, "created": False}

    # Create the observability epic
    # The "Epic Name" custom field is needed for Jira Cloud epics
    fields: dict[str, Any] = {
        "project": {"key": project},
        "summary": epic_summary,
        "description": (
            f"Umbrella epic for auto-generated observability stories for CNV {version}.\n\n"
            "Stories in this epic were generated by the cnv-epic-agent tool "
            "based on analysis of feature epics that need monitoring coverage.\n\n"
            f"Label: {epic_label}"
        ),
        "issuetype": {"name": "Epic"},
        "labels": [epic_label],
        "components": [{"name": component}],
    }
    issue = client.create_issue(fields=fields)
    return {"key": issue.key, "summary": epic_summary, "created": True}


_SP_UNSET_VALUES = {0, 0.42}


def _should_set_story_points(
    current_value: float | int | None,
    new_value: int | None,
) -> bool:
    """Decide whether to write story points to a Jira issue.

    Returns True only when the new value is provided AND the issue
    doesn't already carry a meaningful estimate.  Values of 0 and
    0.42 are treated as "unset" (Jira defaults); any other non-None
    value means a human or prior run already sized the story.
    """
    if new_value is None:
        return False
    if current_value is None or current_value in _SP_UNSET_VALUES:
        return True
    return False


def create_obs_story(
    client: JIRA,
    cfg: dict[str, Any],
    obs_epic_key: str,
    source_epic_key: str,
    summary: str,
    description: str,
    story_points: int | None = None,
) -> Any:
    """Create a story under the obs epic and link it to the source epic."""
    creation_cfg = cfg.get("creation", {})
    project = creation_cfg.get("project", "CNV")
    component = creation_cfg.get("component", "CNV Install, Upgrade and Operators")
    story_label = creation_cfg.get("story_label", "epic-agent-generated")
    epic_label = creation_cfg.get("epic_label", "cnv-observability")
    sp_field = creation_cfg.get(
        "story_points_field", "story_points",
    )

    fields: dict[str, Any] = {
        "project": {"key": project},
        "summary": summary,
        "description": description,
        "issuetype": {"name": "Story"},
        "labels": [epic_label, story_label],
        "components": [{"name": component}],
    }

    if _should_set_story_points(None, story_points):
        fields[sp_field] = story_points

    issue = client.create_issue(fields=fields)

    # Link the story to the observability epic via Epic Link
    try:
        client.add_issues_to_epic(obs_epic_key, [issue.key])
    except Exception:
        logger.warning(
            "Failed to add %s to epic %s (Epic Link field may vary)",
            issue.key, obs_epic_key, exc_info=True,
        )

    # Link to the source feature epic with "is caused by" / "relates to"
    try:
        client.create_issue_link(
            type="Relates",
            inwardIssue=issue.key,
            outwardIssue=source_epic_key,
        )
    except Exception:
        logger.warning(
            "Failed to link %s to %s (link type may differ)",
            issue.key, source_epic_key, exc_info=True,
        )

    return issue


def update_story_points(
    client: JIRA,
    cfg: dict[str, Any],
    issue_key: str,
    story_points: int,
    model: str = "",
) -> bool:
    """Set story points on an existing issue, unless already set.

    Adds an audit comment so humans can tell the estimate came from
    the agent.  Returns True if updated, False if skipped because the
    issue already has a meaningful SP value (anything other than 0 or
    0.42).
    """
    creation_cfg = cfg.get("creation", {})
    sp_field = creation_cfg.get(
        "story_points_field", "story_points",
    )

    issue = client.issue(issue_key)
    current = getattr(issue.fields, sp_field, None)

    if not _should_set_story_points(current, story_points):
        logger.info(
            "Skipping SP update for %s — already set to %s",
            issue_key, current,
        )
        return False

    issue.update(fields={sp_field: story_points})

    model_tag = f" (model: {model})" if model else ""
    client.add_comment(
        issue_key,
        f"Story points estimated by cnv-epic-agent{model_tag}. "
        "Override if inaccurate.",
    )

    logger.info(
        "Updated %s story points: %s", issue_key, story_points,
    )
    return True


def fetch_unsized_stories(
    client: JIRA,
    cfg: dict[str, Any],
    epic_key: str,
) -> list[Any]:
    """Find Story-type children of an epic that have no story points set.

    Returns raw Jira issue objects whose SP is None, 0, or 0.42.
    Excludes Bugs (bugs don't get story points).
    """
    creation_cfg = cfg.get("creation", {})
    sp_field = creation_cfg.get(
        "story_points_field", "story_points",
    )
    jira_cfg = cfg.get("jira", {})
    proj = jira_cfg.get("default_project", "CNV")

    jql = (
        f'project = {proj} AND "Epic Link" = {epic_key} '
        f'AND type = Story'
    )
    try:
        issues = _search_all(client, jql)
    except Exception:
        logger.warning(
            "Failed to fetch children of %s for SP scan",
            epic_key, exc_info=True,
        )
        return []

    unsized: list[Any] = []
    for issue in issues:
        current = getattr(issue.fields, sp_field, None)
        if current is None or current in _SP_UNSET_VALUES:
            unsized.append(issue)
    return unsized

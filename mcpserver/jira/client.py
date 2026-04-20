"""Jira REST helpers: authentication, querying, and story creation."""

from __future__ import annotations

import functools
import hashlib
import logging
import os
import re
import time
from typing import Any

from jira import JIRA

from schemas.issue_doc import IssueDoc

logger = logging.getLogger(__name__)

_PAGE_SIZE = 200
_JIRA_MAX_RETRIES = 3
_JIRA_INITIAL_BACKOFF_S = 1.0


def _retry_on_jira_error(func):
    """Decorator: retry Jira API calls with exponential backoff.

    Retries on transient HTTP errors (429, 5xx, network).
    Non-retryable errors (400, 401, 403, 404) are raised immediately.
    """
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        last_err: Exception | None = None
        for attempt in range(_JIRA_MAX_RETRIES):
            try:
                return func(*args, **kwargs)
            except Exception as exc:
                exc_text = str(exc)
                status = getattr(exc, "status_code", None)
                if status and status < 500 and status != 429:
                    raise
                last_err = exc
                if attempt < _JIRA_MAX_RETRIES - 1:
                    delay = _JIRA_INITIAL_BACKOFF_S * (2 ** attempt)
                    logger.warning(
                        "Jira API %s failed (attempt %d/%d), "
                        "retrying in %.1fs: %s",
                        func.__name__, attempt + 1,
                        _JIRA_MAX_RETRIES, delay, exc_text,
                    )
                    time.sleep(delay)
        raise last_err  # type: ignore[misc]
    return wrapper


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


def build_epic_jql(
    cfg: dict[str, Any],
    *,
    project: str | None = None,
    since_days: int | None = None,
    component: str | None = None,
    fix_version: str | None = None,
    target_version: str | None = None,
    labels: list[str] | None = None,
) -> str:
    """Build a JQL query for epic scanning with optional filters.

    Each non-None filter appends an AND clause to the base template.
    """
    jira_cfg = cfg.get("jira", {})
    proj = project or jira_cfg.get("default_project", "CNV")
    days = since_days or int(jira_cfg.get("default_since_days", 30))
    template = jira_cfg.get(
        "jql_template",
        "project = {project} AND type = Epic"
        " AND created >= -{since_days}d",
    )
    jql = template.format(project=proj, since_days=days)

    if component:
        jql += f' AND component = "{component}"'
    if fix_version:
        jql += f' AND fixVersion = "{fix_version}"'
    if target_version:
        jql += f' AND "Target Version" = "{target_version}"'
    if labels:
        for label in labels:
            jql += f' AND labels = "{label}"'
    return jql


@_retry_on_jira_error
def search_epics(
    client: JIRA,
    cfg: dict[str, Any],
    project: str | None = None,
    since_days: int | None = None,
    jql: str | None = None,
    *,
    component: str | None = None,
    fix_version: str | None = None,
    target_version: str | None = None,
    labels: list[str] | None = None,
) -> list[Any]:
    if jql is None:
        jql = build_epic_jql(
            cfg,
            project=project,
            since_days=since_days,
            component=component,
            fix_version=fix_version,
            target_version=target_version,
            labels=labels,
        )
    return _search_all(client, jql)


@_retry_on_jira_error
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


def _normalize_summary(summary: str) -> str:
    """Normalize a summary for dedup comparison.

    Strips brackets, parenthesized Jira keys, collapses whitespace,
    and lowercases so that minor LLM rephrasing doesn't cause
    duplicates.
    """
    s = summary.lower()
    s = re.sub(r'\[.*?\]', '', s)
    s = re.sub(r'\([A-Z]+-\d+\)', '', s, flags=re.IGNORECASE)
    s = re.sub(r'\s+', ' ', s).strip()
    return s


_JIRA_KEY_RE = re.compile(r'\b([A-Z]+-\d+)\b')


def _extract_keys_from_summary(summary: str) -> set[str]:
    """Extract any Jira issue keys embedded in a summary string."""
    return set(_JIRA_KEY_RE.findall(summary))


def find_existing_obs_stories(
    client: JIRA,
    cfg: dict[str, Any],
    obs_epic_key: str,
    source_epic_key: str,
) -> list[dict[str, Any]]:
    """Find existing observability stories related to a source epic.

    Uses label-based search (not summary text matching) for robust
    dedup.  Collects from:
    1. Stories under the obs epic with the scanner label
    2. Any issue with the scanner label referencing the source epic
    3. Any issue linked to the source epic with "[Observability]"
    """
    creation_cfg = cfg.get("creation", {})
    story_label = creation_cfg.get("story_label", "epic-agent-generated")

    seen_keys: set[str] = set()
    existing: list[dict[str, Any]] = []

    def _collect(issues: list[Any]) -> None:
        for issue in issues:
            if issue.key in seen_keys:
                continue
            seen_keys.add(issue.key)
            summary = str(getattr(issue.fields, "summary", "") or "")
            labels = getattr(issue.fields, "labels", []) or []
            desc = str(getattr(issue.fields, "description", "") or "")
            existing.append({
                "key": issue.key,
                "summary": summary,
                "labels": labels,
                "description": desc,
            })

    # 1. Stories under the obs epic with the scanner label
    if obs_epic_key and obs_epic_key != "(DRY-RUN)":
        jql = (
            f'"Epic Link" = {obs_epic_key} '
            f'AND labels = "{story_label}"'
        )
        try:
            _collect(_search_all(client, jql))
        except Exception:
            logger.warning(
                "JQL query failed (obs epic children): %s",
                jql, exc_info=True,
            )

    # 2. Any issue with the scanner label referencing the source epic
    jql_label = (
        f'labels = "{story_label}" '
        f'AND summary ~ "{source_epic_key}"'
    )
    try:
        _collect(client.search_issues(jql_label, maxResults=50))
    except Exception:
        logger.warning(
            "JQL query failed (label search): %s",
            jql_label, exc_info=True,
        )

    # 3. Issues linked to the source epic with "[Observability]"
    jql_linked = (
        f'issue in linkedIssues({source_epic_key}) '
        f'AND summary ~ "[Observability]"'
    )
    try:
        _collect(client.search_issues(jql_linked, maxResults=50))
    except Exception:
        logger.warning(
            "JQL query failed (linked issues): %s",
            jql_linked, exc_info=True,
        )

    return existing


def _extract_source_epic(story: dict[str, Any]) -> str | None:
    """Extract source epic key from description fingerprint line."""
    desc = story.get("description", "")
    m = re.search(r'source_epic=([A-Z]+-\d+)', desc)
    return m.group(1) if m else None


def _extract_summary_hash(story: dict[str, Any]) -> str | None:
    """Extract summary hash from description fingerprint line."""
    desc = story.get("description", "")
    m = re.search(r'summary_hash=([a-f0-9]+)', desc)
    return m.group(1) if m else None


def _hash_summary(summary: str) -> str:
    """Produce a short hash of a normalized summary for fingerprinting."""
    norm = _normalize_summary(summary)
    return hashlib.sha256(norm.encode()).hexdigest()[:12]


def is_duplicate_story(
    story_summary: str,
    source_epic_key: str,
    existing: list[dict[str, Any]],
) -> bool:
    """Check if a story is a duplicate using multiple strategies.

    Matching strategies (in order):
    1. Fingerprint: source_epic + summary_hash from description.
    2. Exact normalized summary match.
    3. Key reference: LLM summary embeds a Jira key (e.g.
       "(CNV-51517)") that matches an existing child's key.
    4. Containment: one normalized summary is wholly contained
       within the other (minimum 20 chars on the existing
       summary to avoid false positives on short strings;
       equal strings are caught by strategy 2).
    """
    new_hash = _hash_summary(story_summary)
    norm_new = _normalize_summary(story_summary)
    embedded_keys = _extract_keys_from_summary(story_summary)

    for e in existing:
        e_hash = _extract_summary_hash(e)
        e_source = _extract_source_epic(e)
        if e_source == source_epic_key and e_hash == new_hash:
            return True

        norm_existing = _normalize_summary(e.get("summary", ""))

        if norm_existing == norm_new:
            return True

        e_key = e.get("key", "")
        if e_key and e_key in embedded_keys:
            return True

        if norm_new != norm_existing and len(norm_existing) >= 20:
            if norm_existing in norm_new or norm_new in norm_existing:
                return True

    return False


@_retry_on_jira_error
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


class StoryLinkWarning:
    """Records partial link failures during story creation."""

    def __init__(self) -> None:
        self.epic_link_failed = False
        self.relates_link_failed = False

    @property
    def has_warnings(self) -> bool:
        return self.epic_link_failed or self.relates_link_failed

    def warning_text(self) -> str:
        parts = []
        if self.epic_link_failed:
            parts.append("epic-link failed")
        if self.relates_link_failed:
            parts.append("relates-link failed")
        return ", ".join(parts)


def create_obs_story(
    client: JIRA,
    cfg: dict[str, Any],
    obs_epic_key: str,
    source_epic_key: str,
    summary: str,
    description: str,
    story_points: int | None = None,
    category: str = "",
) -> tuple[Any, StoryLinkWarning]:
    """Create a story under the obs epic and link it to the source epic.

    Returns (issue, warnings).  Embeds a fingerprint in the
    description for robust deduplication on subsequent runs.
    """
    creation_cfg = cfg.get("creation", {})
    project = creation_cfg.get("project", "CNV")
    component = creation_cfg.get("component", "CNV Install, Upgrade and Operators")
    story_label = creation_cfg.get("story_label", "epic-agent-generated")
    epic_label = creation_cfg.get("epic_label", "cnv-observability")
    sp_field = creation_cfg.get(
        "story_points_field", "story_points",
    )

    s_hash = _hash_summary(summary)
    fingerprint = (
        f"\n\n---\n"
        f"_auto-generated by cnv-epic-agent | "
        f"source_epic={source_epic_key} | "
        f"category={category} | "
        f"summary_hash={s_hash}_"
    )
    full_description = description + fingerprint

    fields: dict[str, Any] = {
        "project": {"key": project},
        "summary": summary,
        "description": full_description,
        "issuetype": {"name": "Story"},
        "labels": [epic_label, story_label],
        "components": [{"name": component}],
    }

    if _should_set_story_points(None, story_points):
        fields[sp_field] = story_points

    issue = client.create_issue(fields=fields)
    warnings = StoryLinkWarning()

    # Link the story to the observability epic via Epic Link
    for attempt in range(2):
        try:
            client.add_issues_to_epic(obs_epic_key, [issue.key])
            break
        except Exception:
            if attempt == 0:
                logger.warning(
                    "Epic link failed for %s → %s, retrying...",
                    issue.key, obs_epic_key,
                )
                time.sleep(1)
            else:
                logger.error(
                    "Failed to add %s to epic %s after retry",
                    issue.key, obs_epic_key, exc_info=True,
                )
                warnings.epic_link_failed = True
                try:
                    client.add_comment(
                        issue.key,
                        f"⚠ Failed to set Epic Link to {obs_epic_key}. "
                        "Please add manually.",
                    )
                except Exception:
                    pass

    # Link to the source feature epic
    for attempt in range(2):
        try:
            client.create_issue_link(
                type="Relates",
                inwardIssue=issue.key,
                outwardIssue=source_epic_key,
            )
            break
        except Exception:
            if attempt == 0:
                logger.warning(
                    "Relates link failed for %s → %s, retrying...",
                    issue.key, source_epic_key,
                )
                time.sleep(1)
            else:
                logger.error(
                    "Failed to link %s to %s after retry",
                    issue.key, source_epic_key, exc_info=True,
                )
                warnings.relates_link_failed = True
                try:
                    client.add_comment(
                        issue.key,
                        f"⚠ Failed to create 'Relates' link to "
                        f"{source_epic_key}. Please add manually.",
                    )
                except Exception:
                    pass

    return issue, warnings


@_retry_on_jira_error
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


@_retry_on_jira_error
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
    issues = _search_all(client, jql)

    unsized: list[Any] = []
    for issue in issues:
        current = getattr(issue.fields, sp_field, None)
        if current is None or current in _SP_UNSET_VALUES:
            unsized.append(issue)
    return unsized

"""Jira REST helpers: authentication, querying, and story creation."""

from __future__ import annotations

import functools
import hashlib
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from jira import JIRA

from schemas.issue_doc import IssueDoc

logger = logging.getLogger(__name__)

_PAGE_SIZE = 200
_JIRA_MAX_RETRIES = 3
_JIRA_INITIAL_BACKOFF_S = 1.0


def _escape_jql(value: str) -> str:
    """Escape a value for safe use inside JQL double-quoted strings."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


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


def format_jira_version(cfg: dict[str, Any], version: str) -> str:
    """Format a short version (e.g. '4.22') into the Jira version
    string (e.g. 'CNV v4.22') using the config pattern."""
    jira_cfg = cfg.get("jira", {})
    fmt = jira_cfg.get("version_format", "CNV v{version}")
    return fmt.format(version=version)


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
    When both fix_version and target_version are provided with the
    same value, they are combined with OR so epics with either field
    set are matched.

    Epics carrying the configured ``skip_label`` are always excluded.
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

    skip_label = cfg.get("grooming", {}).get(
        "skip_label", "cnv-grooming-agent-skip",
    )
    jql += f' AND labels != "{_escape_jql(skip_label)}"'

    if component:
        jql += f' AND component = "{_escape_jql(component)}"'

    if fix_version and target_version:
        jql += (
            f' AND (fixVersion = "{_escape_jql(fix_version)}"'
            f' OR "Target Version" = "{_escape_jql(target_version)}")'
        )
    elif fix_version:
        jql += f' AND fixVersion = "{_escape_jql(fix_version)}"'
    elif target_version:
        jql += (
            f' AND "Target Version" = '
            f'"{_escape_jql(target_version)}"'
        )

    if labels:
        for label in labels:
            jql += f' AND labels = "{_escape_jql(label)}"'
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
    sp_field = (
        cfg.get("creation", {}).get(
            "story_points_field", "customfield_10028",
        )
    )
    epic_issue = client.issue(epic_key)
    epic = IssueDoc.from_jira(epic_issue, sp_field=sp_field)
    children_raw = fetch_child_issues(client, cfg, epic_key)
    children = [
        IssueDoc.from_jira(c, sp_field=sp_field)
        for c in children_raw
    ]
    return epic, children


def needs_grooming(
    epic: IssueDoc,
    children: list[IssueDoc],
    cfg: dict[str, Any],
) -> bool:
    """Return True if the epic lacks enough detail for analysis."""
    grooming_cfg = cfg.get("grooming", {})
    min_desc = int(grooming_cfg.get("min_description_length", 50))
    min_children = int(grooming_cfg.get("min_children", 1))

    desc_len = len((epic.description or "").strip())
    has_enough_desc = desc_len >= min_desc
    has_enough_children = len(children) >= min_children

    return not (has_enough_desc or has_enough_children)


@_retry_on_jira_error
def add_grooming_label(
    client: JIRA,
    cfg: dict[str, Any],
    epic_key: str,
) -> None:
    """Add the grooming label to an epic if not already present."""
    grooming_cfg = cfg.get("grooming", {})
    label = grooming_cfg.get("label", "grooming")
    issue = client.issue(epic_key)
    existing = getattr(issue.fields, "labels", []) or []
    if label not in existing:
        issue.update(fields={"labels": existing + [label]})
        logger.info("Added '%s' label to %s", label, epic_key)


@_retry_on_jira_error
def add_grooming_comment(
    client: JIRA,
    cfg: dict[str, Any],
    epic_key: str,
    comment_override: str = "",
) -> None:
    """Post a comment asking for more detail on the epic.

    If *comment_override* is provided (e.g. from an LLM clarity
    check), it is used instead of the config default.
    """
    if comment_override:
        comment_text = comment_override
    else:
        grooming_cfg = cfg.get("grooming", {})
        comment_text = grooming_cfg.get(
            "comment",
            "[Epic Agent] This epic does not have enough detail "
            "for story generation. Please add child stories and "
            "a more detailed description.",
        )
    client.add_comment(epic_key, comment_text)
    logger.info("Added grooming comment to %s", epic_key)


_AGENT_COMMENT_PREFIX = "[Epic Agent]"


def days_since_last_agent_comment(
    client: JIRA,
    epic_key: str,
) -> float | None:
    """Return days elapsed since the last agent-posted comment.

    Returns ``None`` if no agent comment exists on the issue.
    """
    try:
        issue = client.issue(epic_key, fields="comment")
    except Exception:
        logger.warning(
            "Could not fetch comments for %s", epic_key,
            exc_info=True,
        )
        return None

    comments = issue.fields.comment.comments if issue.fields.comment else []
    for comment in reversed(comments):
        body = getattr(comment, "body", "") or ""
        if body.startswith(_AGENT_COMMENT_PREFIX):
            created = getattr(comment, "created", None)
            if not created:
                continue
            ts = datetime.fromisoformat(
                created.replace("+0000", "+00:00"),
            )
            delta = datetime.now(timezone.utc) - ts
            return delta.total_seconds() / 86400.0
    return None


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
            f'AND labels = "{_escape_jql(story_label)}"'
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
        f'labels = "{_escape_jql(story_label)}" '
        f'AND summary ~ "{_escape_jql(source_epic_key)}"'
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


def find_broad_matching_stories(
    client: JIRA,
    cfg: dict[str, Any],
    source_epic_key: str,
    keywords: list[str],
) -> list[dict[str, Any]]:
    """Search the project for open stories matching domain keywords.

    Catches duplicates that live under unrelated epics or in
    previous-version observability epics.  Returns entries tagged
    with ``"_from_broad_search": True`` so the dedup logic limits
    itself to exact-summary and containment strategies.
    """
    project = cfg.get("jira", {}).get("default_project", "CNV")

    usable = [kw for kw in keywords if len(kw) >= 5][:5]
    if not usable:
        return []

    keyword_clause = " OR ".join(
        f'summary ~ "{_escape_jql(kw)}"' for kw in usable
    )
    jql = (
        f'project = "{_escape_jql(project)}" '
        f"AND type = Story "
        f"AND status not in (Closed, Done, Verified) "
        f"AND ({keyword_clause})"
    )

    results: list[dict[str, Any]] = []
    try:
        issues = client.search_issues(jql, maxResults=50)
        for issue in issues:
            if issue.key == source_epic_key:
                continue
            summary = str(
                getattr(issue.fields, "summary", "") or ""
            )
            results.append({
                "key": issue.key,
                "summary": summary,
                "_from_broad_search": True,
            })
    except Exception:
        logger.warning(
            "JQL query failed (broad search): %s",
            jql, exc_info=True,
        )

    return results


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


_QE_DOC_PREFIX_RE = re.compile(
    r'^\s*\[(QE|Docs)\]', re.IGNORECASE,
)

# Matches technical tokens: metric names (snake_case ≥10 chars),
# alert names (CamelCase starting with a capital, ≥10 chars), and
# component/exporter names like "csi-volume-device-exporter".
_TECH_TOKEN_RE = re.compile(
    r'\b(?:'
    r'[a-z][a-z0-9_]{9,}'   # snake_case metric names (≥10 chars)
    r'|[A-Z][a-zA-Z]{9,}'   # CamelCase alert/component names (≥10)
    r'|[a-z][a-z0-9]*(?:-[a-z0-9]+){2,}'  # kebab-case (≥3 segments)
    r')\b'
)


def _tech_tokens(text: str) -> frozenset[str]:
    """Extract significant technical tokens from text (lowercase)."""
    return frozenset(t.lower() for t in _TECH_TOKEN_RE.findall(text))


def is_duplicate_story(
    story_summary: str,
    source_epic_key: str,
    existing: list[dict[str, Any]],
    story_description: str = "",
) -> str | None:
    """Check if a story is a duplicate using multiple strategies.

    Returns the matching issue key on duplicate, or ``None``.
    The return value is truthy/falsy so callers that treat it
    as a bool continue to work unchanged.

    Matching strategies (in order):
    1. Fingerprint: source_epic + summary_hash from description.
    2. Exact normalized summary match.
    3. Key reference: LLM summary embeds a Jira key (e.g.
       "CNV-80580") that matches an existing issue's key.
       For source-epic children (``_from_children``), this
       only triggers when the proposed story is NOT a QE or
       docs story — those are complementary work, not dups.
    4. Containment: one normalized summary is wholly contained
       within the other (minimum 20 chars on the existing
       summary to avoid false positives on short strings;
       equal strings are caught by strategy 2).
       Disabled for source-epic children (``_from_children``)
       because QE/docs stories naturally reuse child phrasing.
    5. Technical token overlap (children only): extract
       significant tokens (metric names, alert names, kebab
       component names) from the proposed story's summary+
       description and the child's description. If ≥2 tokens
       overlap the child is considered to already cover the
       work — avoids proposing metrics/alerts that a child
       story already describes even when summaries differ.
       Not applied to QE/docs stories (complementary work).

    For broad-search entries (``_from_broad_search``), only
    strategies 2 and 4 apply — fingerprint and key-reference
    are irrelevant for issues found via keyword search.
    """
    new_hash = _hash_summary(story_summary)
    norm_new = _normalize_summary(story_summary)
    embedded_keys = _extract_keys_from_summary(story_summary)
    is_qe_or_docs = bool(_QE_DOC_PREFIX_RE.match(story_summary))

    for e in existing:
        e_key = e.get("key", "")
        from_children = e.get("_from_children", False)
        from_broad = e.get("_from_broad_search", False)

        norm_existing = _normalize_summary(e.get("summary", ""))

        if not from_broad:
            e_hash = _extract_summary_hash(e)
            e_source = _extract_source_epic(e)
            if e_source == source_epic_key and e_hash == new_hash:
                return e_key or "unknown"

        if norm_existing == norm_new:
            return e_key or "unknown"

        if not from_broad and not from_children:
            if e_key and e_key in embedded_keys:
                return e_key
        elif not from_broad and from_children:
            if e_key and e_key in embedded_keys:
                if not is_qe_or_docs:
                    return e_key

        if from_children:
            # Strategy 5: technical token overlap against child
            # description. Catches cases where a child story describes
            # the same metric/alert work with a different summary.
            # Only for observability proposals (not QE/docs which are
            # complementary and intentionally reference child tokens).
            if not is_qe_or_docs:
                proposed_tokens = _tech_tokens(
                    story_summary + " " + story_description
                )
                child_desc_tokens = _tech_tokens(
                    e.get("summary", "") + " " + e.get("description", "")
                )
                overlap = proposed_tokens & child_desc_tokens
                if len(overlap) >= 2:
                    logger.debug(
                        "Tech token overlap (%d tokens) with %s: %s",
                        len(overlap), e_key, overlap,
                    )
                    return e_key or "unknown"
            continue

        if norm_new != norm_existing and len(norm_existing) >= 20:
            if norm_existing in norm_new or norm_new in norm_existing:
                return e_key or "unknown"

    return None


@_retry_on_jira_error
def find_or_create_obs_epic(
    client: JIRA,
    cfg: dict[str, Any],
    version: str,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Find or create the version-scoped observability epic.

    Searches by the ``obs_epic_label`` label and target/fix version
    (not summary text).  When creating, sets both the label and
    target version so future searches find it reliably.

    Returns {"key": "CNV-XXXXX", "summary": "...", "created": bool}.
    """
    creation_cfg = cfg.get("creation", {})
    project = creation_cfg.get("project", "CNV")
    epic_label = creation_cfg.get("epic_label", "cnv-observability")
    obs_epic_label = creation_cfg.get(
        "obs_epic_label", "cnv-grooming-agent",
    )
    component = creation_cfg.get(
        "component", "CNV Install, Upgrade and Operators",
    )
    epic_summary_fmt = creation_cfg.get(
        "epic_summary_format",
        "[Observability] CNV {version} — Auto-generated observability stories",
    )

    jira_version = format_jira_version(cfg, version)
    epic_summary = epic_summary_fmt.format(version=version)
    target_version_field = cfg.get("jira", {}).get(
        "target_version_field", "customfield_10855"
    )

    jql = (
        f'project = {project} AND type = Epic '
        f'AND labels = "{_escape_jql(obs_epic_label)}" '
        f'AND (fixVersion = "{_escape_jql(jira_version)}"'
        f' OR "Target Version" = "{_escape_jql(jira_version)}")'
    )
    results = client.search_issues(jql, maxResults=5)
    if results:
        found = results[0]
        return {
            "key": found.key,
            "summary": str(found.fields.summary),
            "created": False,
        }

    if dry_run:
        return {
            "key": "(DRY-RUN)",
            "summary": epic_summary,
            "created": False,
        }

    fields: dict[str, Any] = {
        "project": {"key": project},
        "summary": epic_summary,
        "description": (
            f"Umbrella epic for auto-generated observability "
            f"stories for CNV {version}.\n\n"
            "Stories in this epic were generated by the "
            "cnv-epic-agent tool based on analysis of feature "
            "epics that need monitoring coverage."
        ),
        "issuetype": {"name": "Epic"},
        "labels": [epic_label, obs_epic_label],
        "components": [{"name": component}],
        # Use Target Version (not fixVersions) so the epic is
        # discoverable via "Target Version" = "CNV vX.Y.Z" JQL,
        # which is how all observability-scoped searches work.
        target_version_field: {"name": jira_version},
    }
    issue = client.create_issue(fields=fields)
    return {
        "key": issue.key,
        "summary": epic_summary,
        "created": True,
    }


_SP_UNSET_VALUES = {0, 0.42}

_CLOSED_STATUSES = frozenset({
    "closed", "done", "resolved", "verified",
})


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


def _build_feedback_footer(
    summary: str,
    source_epic_key: str,
    category: str,
    run_id: str,
    feedback_repo: str,
) -> str:
    """Build a Jira wiki markup footer with an agent attribution line.

    If *feedback_repo* is set, appends a pre-filled GitHub issue link
    using the issue template at .github/ISSUE_TEMPLATE/agent-feedback.yml.
    The URL only carries dynamic context (title, epic, category, run_id);
    the form structure lives in the template file.
    """
    if not feedback_repo:
        return (
            "\n\n----\n"
            "_Generated by cnv-grooming-agent_"
        )

    base = feedback_repo.rstrip("/")
    title = quote(
        f"Agent feedback: [{source_epic_key}] {summary}",
        safe="",
    )
    url = (
        f"{base}/issues/new"
        f"?template=agent-feedback.yml"
        f"&title={title}"
        f"&epic={quote(source_epic_key, safe='')}"
        f"&category={quote(category, safe='')}"
        f"&run-id={quote(run_id, safe='')}"
    )
    return (
        "\n\n----\n"
        f"_Generated by cnv-grooming-agent "
        f"([report issue|{url}])_"
    )


def create_obs_story(
    client: JIRA,
    cfg: dict[str, Any],
    obs_epic_key: str,
    source_epic_key: str,
    summary: str,
    description: str,
    story_points: int | None = None,
    category: str = "",
    run_id: str = "",
    parent_epic_key: str = "",
) -> tuple[Any, StoryLinkWarning]:
    """Create a story and link it to the source epic.

    By default the story is added to the observability epic
    (obs_epic_key).  Pass parent_epic_key to override the parent —
    used for QE and docs stories which belong under the source
    feature epic, not the observability epic.

    Returns (issue, warnings).  Embeds a fingerprint in the
    description for robust deduplication on subsequent runs.
    """
    creation_cfg = cfg.get("creation", {})
    project = creation_cfg.get("project", "CNV")
    component = creation_cfg.get("component", "CNV Install, Upgrade and Operators")
    story_label = creation_cfg.get("story_label", "epic-agent-generated")
    epic_label = creation_cfg.get("epic_label", "cnv-observability")
    sp_field = creation_cfg.get(
        "story_points_field", "customfield_10028",
    )
    relates_link_type = creation_cfg.get(
        "relates_link_type", "Relates",
    )

    s_hash = _hash_summary(summary)
    fingerprint = (
        f"\n\n---\n"
        f"_auto-generated by cnv-epic-agent | "
        f"source_epic={source_epic_key} | "
        f"category={category} | "
        f"summary_hash={s_hash}_"
    )
    feedback_repo = cfg.get("agent", {}).get("feedback_repo", "")
    footer = _build_feedback_footer(
        summary=summary,
        source_epic_key=source_epic_key,
        category=category,
        run_id=run_id,
        feedback_repo=feedback_repo,
    )
    full_description = description + fingerprint + footer

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

    # QE and docs stories belong under the source feature epic;
    # observability stories belong under the observability epic.
    target_epic_key = parent_epic_key or obs_epic_key

    # Link the story to the target epic via Epic Link
    for attempt in range(2):
        try:
            client.add_issues_to_epic(target_epic_key, [issue.key])
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
                    issue.key, target_epic_key, exc_info=True,
                )
                warnings.epic_link_failed = True
                try:
                    client.add_comment(
                        issue.key,
                        f"⚠ Failed to set Epic Link to {target_epic_key}. "
                        "Please add manually.",
                    )
                except Exception:
                    pass

    # Link to the source feature epic
    for attempt in range(3):
        try:
            client.create_issue_link(
                type=relates_link_type,
                inwardIssue=issue.key,
                outwardIssue=source_epic_key,
            )
            break
        except Exception:
            if attempt < 2:
                wait = 2 ** attempt  # 1s, 2s
                logger.warning(
                    "Relates link failed for %s → %s "
                    "(attempt %d/3), retrying in %ds...",
                    issue.key, source_epic_key, attempt + 1, wait,
                )
                time.sleep(wait)
            else:
                logger.error(
                    "Failed to link %s to %s after 3 attempts",
                    issue.key, source_epic_key, exc_info=True,
                )
                warnings.relates_link_failed = True
                try:
                    client.add_comment(
                        issue.key,
                        f"⚠ Failed to create '{relates_link_type}' "
                        f"link to {source_epic_key}. "
                        f"Please add manually.",
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
        "story_points_field", "customfield_10028",
    )

    issue = client.issue(issue_key)

    status_name = str(
        getattr(getattr(issue.fields, "status", None), "name", "")
    ).lower()
    if status_name in _CLOSED_STATUSES:
        logger.info(
            "Skipping SP update for %s — status is '%s'",
            issue_key, status_name,
        )
        return False

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
    Excludes Bugs (bugs don't get story points) and closed stories.
    """
    creation_cfg = cfg.get("creation", {})
    sp_field = creation_cfg.get(
        "story_points_field", "customfield_10028",
    )
    jira_cfg = cfg.get("jira", {})
    proj = jira_cfg.get("default_project", "CNV")

    jql = (
        f'project = {proj} AND "Epic Link" = {epic_key} '
        f'AND type = Story '
        f'AND status NOT IN (Closed, Done, Resolved, Verified)'
    )
    issues = _search_all(client, jql)

    unsized: list[Any] = []
    for issue in issues:
        current = getattr(issue.fields, sp_field, None)
        if current is None or current in _SP_UNSET_VALUES:
            unsized.append(issue)
    return unsized

"""Lightweight Jira issue representation for text analysis.

Shared by both the MCP layer and the agent analyzer so that neither
has a cross-layer dependency on the other.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class IssueDoc:
    """Lightweight representation of a Jira issue for text analysis."""

    key: str
    summary: str
    description: str = ""
    issue_type: str = ""
    labels: list[str] | None = None
    components: list[str] | None = None
    story_points: int = 0
    status: str = ""

    @classmethod
    def from_jira(
        cls, issue: Any, sp_field: str = "customfield_10028",
    ) -> "IssueDoc":
        is_dict = isinstance(issue, dict)
        if is_dict:
            fields = issue.get("fields", issue)
        else:
            fields = issue.fields if hasattr(issue, "fields") else issue

        def _field(obj: Any, attr: str, default: str = "") -> str:
            if isinstance(obj, dict):
                return str(obj.get(attr, default) or default)
            return str(getattr(obj, attr, default) or default)

        if is_dict:
            key = str(issue.get("key", ""))
        else:
            key = str(getattr(issue, "key", "") or "")

        raw_labels = (
            fields.get("labels", []) if isinstance(fields, dict)
            else getattr(fields, "labels", None)
        ) or []
        raw_components = (
            fields.get("components", []) if isinstance(fields, dict)
            else getattr(fields, "components", None)
        ) or []

        issuetype = (
            fields.get("issuetype", {}) if isinstance(fields, dict)
            else getattr(fields, "issuetype", None)
        )
        issue_type_name = _field(issuetype, "name") if issuetype else ""

        raw_sp = (
            fields.get(sp_field) if isinstance(fields, dict)
            else getattr(fields, sp_field, None)
        )
        try:
            sp = int(raw_sp) if raw_sp is not None else 0
        except (ValueError, TypeError):
            sp = 0

        raw_status = (
            fields.get("status", {}) if isinstance(fields, dict)
            else getattr(fields, "status", None)
        )
        status_name = _field(raw_status, "name") if raw_status else ""

        return cls(
            key=key,
            summary=_field(fields, "summary"),
            description=_field(fields, "description"),
            issue_type=issue_type_name,
            labels=list(raw_labels),
            components=[
                (c.get("name", str(c)) if isinstance(c, dict)
                 else str(getattr(c, "name", c)))
                for c in raw_components
            ],
            story_points=sp,
            status=status_name,
        )

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "IssueDoc":
        return cls(
            key=str(payload.get("key", "")),
            summary=str(payload.get("summary", "")),
            description=str(payload.get("description", "")),
            issue_type=str(payload.get("issue_type", "")),
            labels=payload.get("labels"),
            components=payload.get("components"),
        )

    def full_text(self) -> str:
        return f"{self.summary}\n{self.description}".lower()

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

    @classmethod
    def from_jira(cls, issue: Any) -> "IssueDoc":
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

        return cls(
            key=key,
            summary=_field(fields, "summary"),
            description=_field(fields, "description"),
            issue_type=issue_type_name,
            labels=list(raw_labels),
            components=[
                str(getattr(c, "name", c))
                if not isinstance(c, str) else c
                for c in raw_components
            ],
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

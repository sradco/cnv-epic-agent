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
    description: str
    issue_type: str = ""
    labels: list[str] | None = None
    components: list[str] | None = None

    @classmethod
    def from_jira(cls, issue: Any) -> "IssueDoc":
        fields = issue.fields if hasattr(issue, "fields") else issue
        raw_labels = getattr(fields, "labels", None) or []
        raw_components = getattr(fields, "components", None) or []
        return cls(
            key=str(getattr(issue, "key", "") or issue.get("key", "")),
            summary=str(getattr(fields, "summary", "") or ""),
            description=str(getattr(fields, "description", "") or ""),
            issue_type=str(
                getattr(getattr(fields, "issuetype", None), "name", "")
                or ""
            ),
            labels=list(raw_labels),
            components=[
                str(getattr(c, "name", c)) for c in raw_components
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

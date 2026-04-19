"""Data contracts for story payloads and analysis results.

Shared by both the MCP prompt layer and the standalone agent so that
both produce identically-shaped output.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


VALID_CATEGORIES = frozenset(
    {"metrics", "alerts", "dashboards", "telemetry", "docs", "qe"},
)


@dataclass
class StoryPayload:
    """A single Jira story ready for creation."""

    category: str
    summary: str
    description: str
    story_points: int | None = None


@dataclass
class AnalysisResult:
    """Typed wrapper around the dict returned by build_analysis_result.

    Kept as a dataclass (not Pydantic) to avoid adding a dependency.
    The ``from_dict`` factory handles the raw dict produced by the
    analyzer and the JSON returned by ``get_analysis_data``.
    """

    epic_key: str
    epic_summary: str
    epic_description: str
    child_issues: list[dict[str, str]]
    domain_keywords: list[str]
    need_state: str
    need_confidence: str
    gaps: list[str]
    feature_types: list[str]
    proposals: dict[str, Any]
    dashboard_targets: list[str]
    telemetry_suggestions: list[dict[str, str]]
    recommended_action: str
    apply_allowed: bool
    would_create_count: int
    need_score: int = 0
    need_evidence: dict[str, Any] = field(default_factory=dict)
    coverage: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AnalysisResult":
        return cls(
            epic_key=data["epic_key"],
            epic_summary=data["epic_summary"],
            epic_description=data.get("epic_description", ""),
            child_issues=data.get("child_issues", []),
            domain_keywords=data.get("domain_keywords", []),
            need_state=data["need_state"],
            need_confidence=data["need_confidence"],
            need_score=data.get("need_score", 0),
            need_evidence=data.get("need_evidence", {}),
            coverage=data.get("coverage", {}),
            gaps=data.get("gaps", []),
            feature_types=data.get("feature_types", []),
            proposals=data.get("proposals", {}),
            dashboard_targets=data.get("dashboard_targets", []),
            telemetry_suggestions=data.get("telemetry_suggestions", []),
            recommended_action=data["recommended_action"],
            apply_allowed=data["apply_allowed"],
            would_create_count=data.get("would_create_count", 0),
        )


SP_ESTIMATION_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "estimates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "issue_key": {
                        "type": "string",
                        "description": "Jira issue key (e.g. CNV-12345)",
                    },
                    "story_points": {
                        "type": "integer",
                        "description": (
                            "Estimated story points on the Fibonacci "
                            "scale (1, 2, 3, 5, 8, 13)"
                        ),
                    },
                    "rationale": {
                        "type": "string",
                        "description": (
                            "Brief explanation of why this estimate "
                            "was chosen"
                        ),
                    },
                },
                "required": ["issue_key", "story_points", "rationale"],
            },
        },
    },
    "required": ["estimates"],
}


STORY_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "stories": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "enum": sorted(VALID_CATEGORIES),
                    },
                    "summary": {
                        "type": "string",
                        "description": (
                            "Jira story title, e.g. "
                            "'[Observability][metrics] Add Prometheus metrics "
                            "for CNV-12345: VM snapshot controller' or "
                            "'[Docs] Update API docs for CNV-12345'"
                        ),
                    },
                    "description": {
                        "type": "string",
                        "description": (
                            "Full Jira story body in markdown with sections: "
                            "Why this is needed, Proposed changes, "
                            "Acceptance criteria"
                        ),
                    },
                    "story_points": {
                        "type": "integer",
                        "description": (
                            "Estimated story points on the Fibonacci scale "
                            "(1, 2, 3, 5, 8, 13)"
                        ),
                    },
                },
                "required": [
                    "category", "summary", "description", "story_points",
                ],
            },
        },
    },
    "required": ["stories"],
}

"""Data contracts for story payloads and analysis results.

Shared by both the MCP prompt layer and the standalone agent so that
both produce identically-shaped output.
"""

from __future__ import annotations

from dataclasses import dataclass
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

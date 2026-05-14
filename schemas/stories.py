"""Data contracts for story payloads and analysis results."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


VALID_CATEGORIES = frozenset(
    {"metrics", "alerts", "dashboards", "telemetry", "docs", "qe"},
)


@dataclass
class StoryPayload:
    """A single Jira story ready for creation.

    linked_to: For QE/Docs stories that cover a *proposed* observability
    item (not an existing Jira child issue), set this to the summary of
    the observability story being covered.  The XLSX router uses this to
    place such stories in the Observability Stories sheet alongside the
    work they validate, rather than in the QE & Docs sheet.
    """

    category: str
    summary: str
    description: str
    story_points: int | None = None
    reasoning: str = ""
    linked_to: str = ""


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
                            "Jira story title. Do NOT add any bracketed "
                            "category prefix like [QE], [Docs], or [Obs] "
                            "— the agent adds those automatically. "
                            "For observability stories use the format "
                            "'[Observability][metrics] Add Prometheus "
                            "metrics for CNV-12345: VM snapshot controller'. "
                            "For QE stories: 'Verify GPU metric unit tests "
                            "for CNV-12345'. "
                            "For docs stories: 'Update runbook for "
                            "KubeVirtVMIGPUAllocationFailed alert'."
                        ),
                    },
                    "description": {
                        "type": "string",
                        "description": (
                            "Full Jira story body in markdown. "
                            "For observability stories include: "
                            "## Why this matters (real-world problem), "
                            "## Who benefits (operator / SRE / "
                            "virt-operator), "
                            "## How it is used (concrete scenario), "
                            "## Proposed changes, "
                            "## Acceptance criteria (checklist). "
                            "For other categories: Why this is "
                            "needed, Proposed changes, "
                            "Acceptance criteria."
                        ),
                    },
                    "story_points": {
                        "type": "integer",
                        "description": (
                            "Estimated story points on the Fibonacci scale "
                            "(1, 2, 3, 5, 8, 13)"
                        ),
                    },
                    "reasoning": {
                        "type": "string",
                        "description": (
                            "One or two sentences explaining WHY this "
                            "story is needed — what new runtime behavior "
                            "does the epic introduce that is not already "
                            "covered? If you cannot articulate a clear "
                            "reason, do not propose the story."
                        ),
                    },
                    "linked_to": {
                        "type": "string",
                        "description": (
                            "For QE or Docs stories that validate a "
                            "proposed observability item (metric, alert, "
                            "dashboard, or telemetry story proposed in "
                            "this same response): set this to the exact "
                            "summary of that observability story. "
                            "Leave empty for QE/Docs stories that cover "
                            "existing Jira child issues."
                        ),
                    },
                },
                "required": [
                    "category", "summary", "description",
                    "story_points", "reasoning",
                ],
            },
        },
    },
    "required": ["stories"],
}

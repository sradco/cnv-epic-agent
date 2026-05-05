"""Typed configuration hierarchy for cnv-epic-agent.

Provides validated, typed access to config.yaml values.  Unknown top-level
keys are silently kept in ``raw`` so that template sections (e.g.
``subtask_templates``, ``observability_patterns``) remain accessible without
requiring an exhaustive schema for every template string.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import yaml


class ConfigError(ValueError):
    """Raised when config.yaml has invalid or missing values."""


@dataclass
class JiraConfig:
    url: str = "https://redhat.atlassian.net"
    default_project: str = "CNV"
    default_since_days: int = 30
    version_format: str = "CNV v{version}"
    jql_template: str = (
        "project = {project} AND type = Epic"
        " AND created >= -{since_days}d"
    )
    child_issues_jql_template: str = (
        'project = {project} AND "Epic Link" = {epic_key}'
    )


@dataclass
class CreationConfig:
    project: str = "CNV"
    component: str = "CNV Install, Upgrade and Operators"
    epic_label: str = "cnv-observability"
    obs_epic_label: str = "cnv-grooming-agent"
    story_label: str = "epic-agent-generated"
    epic_summary_format: str = (
        "[Observability] CNV {version} — "
        "Auto-generated observability stories"
    )
    story_points_field: str = "customfield_10028"


@dataclass
class GroomingConfig:
    label: str = "grooming"
    skip_label: str = "cnv-grooming-agent-skip"
    comment_cooldown_days: int = 7
    min_description_length: int = 50
    min_children: int = 1
    llm_clarity_check: bool = True


@dataclass
class StoryPointsConfig:
    enabled: bool = False
    estimate_existing: bool = False
    guidance: str = ""


@dataclass
class AgentConfig:
    default_model: str = "gpt-4o"
    max_stories_per_run: int = 50
    temperature: float = 0.2
    enabled_categories: list[str] = field(
        default_factory=lambda: [
            "metrics", "alerts", "dashboards",
            "telemetry", "docs", "qe",
        ],
    )
    category_guidance: dict[str, Any] = field(
        default_factory=dict,
    )
    story_points: StoryPointsConfig = field(
        default_factory=StoryPointsConfig,
    )
    feedback_repo: str = ""


@dataclass
class DiscoveryConfig:
    repos: list[str] = field(default_factory=list)
    cache_ttl_seconds: int = 3600


@dataclass
class TelemetryConfig:
    cmo_allowlist_url: str = ""


@dataclass
class AppConfig:
    """Top-level typed config.  Sections not modelled here are
    available via ``raw`` for backward compatibility."""

    jira: JiraConfig = field(default_factory=JiraConfig)
    creation: CreationConfig = field(default_factory=CreationConfig)
    grooming: GroomingConfig = field(default_factory=GroomingConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    discovery: DiscoveryConfig = field(default_factory=DiscoveryConfig)
    telemetry: TelemetryConfig = field(default_factory=TelemetryConfig)

    raw: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_str_list(
        val: Any, field_name: str,
    ) -> list[str]:
        """Coerce a YAML value into a list of strings safely."""
        if isinstance(val, str):
            return [v.strip() for v in val.split(",") if v.strip()]
        if isinstance(val, list):
            return [str(v) for v in val]
        raise ConfigError(
            f"{field_name} must be a list or "
            f"comma-separated string, "
            f"got {type(val).__name__}"
        )

    @staticmethod
    def _parse_category_list(val: Any) -> list[str]:
        """Coerce enabled_categories safely.

        A YAML scalar string like ``"metrics"`` would be turned into
        ``['m', 'e', 't', ...]`` by ``list()``.  Detect that and split
        on commas instead.
        """
        if isinstance(val, str):
            return [c.strip() for c in val.split(",") if c.strip()]
        if isinstance(val, list):
            return [str(c) for c in val]
        raise ConfigError(
            f"enabled_categories must be a list or "
            f"comma-separated string, got {type(val).__name__}"
        )

    @staticmethod
    def _parse_dict(
        val: Any, field_name: str,
    ) -> dict[str, Any]:
        """Ensure a value is a dict; raise ConfigError otherwise."""
        if isinstance(val, dict):
            return dict(val)
        raise ConfigError(
            f"{field_name} must be a mapping, "
            f"got {type(val).__name__}"
        )

    @staticmethod
    def _parse_float(val: Any, field_name: str) -> float:
        try:
            return float(val)
        except (ValueError, TypeError):
            raise ConfigError(
                f"{field_name} must be a number, "
                f"got {val!r}"
            )

    @staticmethod
    def _parse_int(val: Any, field_name: str) -> int:
        try:
            return int(val)
        except (ValueError, TypeError):
            raise ConfigError(
                f"{field_name} must be an integer, "
                f"got {val!r}"
            )

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> AppConfig:
        """Build a typed config from a raw YAML dict, validating as we go."""
        j = d.get("jira", {}) or {}
        c = d.get("creation", {}) or {}
        g = d.get("grooming", {}) or {}
        a = d.get("agent", {}) or {}
        disc = d.get("discovery", {}) or {}
        tel = d.get("telemetry", {}) or {}
        sp = a.get("story_points", {}) or {}

        cfg = cls(
            jira=JiraConfig(
                url=str(j.get("url", JiraConfig.url)),
                default_project=str(
                    j.get("default_project",
                           JiraConfig.default_project),
                ),
                default_since_days=cls._parse_int(
                    j.get("default_since_days",
                           JiraConfig.default_since_days),
                    "jira.default_since_days",
                ),
                version_format=str(
                    j.get("version_format",
                           JiraConfig.version_format),
                ),
                jql_template=str(
                    j.get("jql_template",
                           JiraConfig.jql_template),
                ),
                child_issues_jql_template=str(
                    j.get("child_issues_jql_template",
                           JiraConfig.child_issues_jql_template),
                ),
            ),
            creation=CreationConfig(
                project=str(c.get("project", CreationConfig.project)),
                component=str(
                    c.get("component", CreationConfig.component),
                ),
                epic_label=str(
                    c.get("epic_label", CreationConfig.epic_label),
                ),
                obs_epic_label=str(
                    c.get("obs_epic_label",
                           CreationConfig.obs_epic_label),
                ),
                story_label=str(
                    c.get("story_label",
                           CreationConfig.story_label),
                ),
                epic_summary_format=str(
                    c.get("epic_summary_format",
                           CreationConfig.epic_summary_format),
                ),
                story_points_field=str(
                    c.get("story_points_field",
                           CreationConfig.story_points_field),
                ),
            ),
            grooming=GroomingConfig(
                label=str(g.get("label", GroomingConfig.label)),
                skip_label=str(
                    g.get("skip_label",
                           GroomingConfig.skip_label),
                ),
                comment_cooldown_days=cls._parse_int(
                    g.get("comment_cooldown_days",
                           GroomingConfig.comment_cooldown_days),
                    "grooming.comment_cooldown_days",
                ),
                min_description_length=cls._parse_int(
                    g.get("min_description_length",
                           GroomingConfig.min_description_length),
                    "grooming.min_description_length",
                ),
                min_children=cls._parse_int(
                    g.get("min_children",
                           GroomingConfig.min_children),
                    "grooming.min_children",
                ),
                llm_clarity_check=bool(
                    g.get("llm_clarity_check",
                           GroomingConfig.llm_clarity_check),
                ),
            ),
            agent=AgentConfig(
                default_model=str(
                    a.get("default_model",
                           AgentConfig.default_model),
                ),
                max_stories_per_run=cls._parse_int(
                    a.get("max_stories_per_run",
                           AgentConfig.max_stories_per_run),
                    "agent.max_stories_per_run",
                ),
                temperature=cls._parse_float(
                    a.get("temperature",
                           AgentConfig.temperature),
                    "agent.temperature",
                ),
                enabled_categories=cls._parse_category_list(
                    a.get("enabled_categories", [
                        "metrics", "alerts", "dashboards",
                        "telemetry", "docs", "qe",
                    ]),
                ),
                category_guidance=cls._parse_dict(
                    a.get("category_guidance", {}),
                    "agent.category_guidance",
                ),
                story_points=StoryPointsConfig(
                    enabled=bool(sp.get("enabled", False)),
                    estimate_existing=bool(
                        sp.get("estimate_existing", False),
                    ),
                    guidance=str(sp.get("guidance", "")),
                ),
                feedback_repo=str(a.get("feedback_repo", "")),
            ),
            discovery=DiscoveryConfig(
                repos=cls._parse_str_list(
                    disc.get("repos", []),
                    "discovery.repos",
                ),
                cache_ttl_seconds=cls._parse_int(
                    disc.get("cache_ttl_seconds", 3600),
                    "discovery.cache_ttl_seconds",
                ),
            ),
            telemetry=TelemetryConfig(
                cmo_allowlist_url=str(
                    tel.get("cmo_allowlist_url", ""),
                ),
            ),
            raw=d,
        )
        cfg._validate()
        return cfg

    @classmethod
    def from_yaml(cls, path: str) -> AppConfig:
        """Load and validate config from a YAML file."""
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        if not isinstance(raw, dict):
            raise ConfigError(
                f"{path} must be a YAML mapping, "
                f"got {type(raw).__name__}"
            )
        return cls.from_dict(raw)

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def _validate(self) -> None:
        from schemas.stories import VALID_CATEGORIES

        for cat in self.agent.enabled_categories:
            if cat not in VALID_CATEGORIES:
                raise ConfigError(
                    f"Unknown category {cat!r} in "
                    f"enabled_categories. "
                    f"Valid: {sorted(VALID_CATEGORIES)}"
                )

        if not 0.0 <= self.agent.temperature <= 2.0:
            raise ConfigError(
                f"temperature must be between 0.0 and 2.0, "
                f"got {self.agent.temperature}"
            )

        if self.grooming.min_children < 0:
            raise ConfigError(
                "grooming.min_children must be >= 0"
            )
        if self.grooming.min_description_length < 0:
            raise ConfigError(
                "grooming.min_description_length must be >= 0"
            )
        if self.grooming.comment_cooldown_days < 0:
            raise ConfigError(
                "grooming.comment_cooldown_days must be >= 0"
            )
        if self.discovery.cache_ttl_seconds < 0:
            raise ConfigError(
                "discovery.cache_ttl_seconds must be >= 0"
            )
        if self.agent.max_stories_per_run < 1:
            raise ConfigError(
                "agent.max_stories_per_run must be >= 1"
            )
        if self.jira.default_since_days < 1:
            raise ConfigError(
                "jira.default_since_days must be >= 1"
            )

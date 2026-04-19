"""LLM-powered story composer.

Takes an analysis result from the analyzer, builds a prompt using
shared templates, calls the LLM via litellm, and parses the
structured JSON response into StoryPayload objects.
"""

from __future__ import annotations

import logging
from typing import Any

from prompts.templates import (
    SYSTEM_PROMPT,
    SP_ESTIMATION_SYSTEM_PROMPT,
    build_story_composition_prompt,
    build_sp_estimation_prompt,
)
from schemas.stories import (
    SP_ESTIMATION_JSON_SCHEMA,
    STORY_JSON_SCHEMA,
    StoryPayload,
)

from agent.planner.llm import complete, parse_json_response

logger = logging.getLogger(__name__)

_FIBONACCI = frozenset({1, 2, 3, 5, 8, 13})


def _clamp_fibonacci(value: int) -> int:
    """Clamp a value to the nearest Fibonacci SP value."""
    if value in _FIBONACCI:
        return value
    return min(_FIBONACCI, key=lambda f: abs(f - value))


def compose_stories(
    analysis: dict[str, Any],
    model: str = "gpt-4o",
    temperature: float = 0.2,
    categories: list[str] | None = None,
    category_guidance: dict[str, Any] | None = None,
    story_points_guidance: str = "",
) -> list[StoryPayload]:
    """Compose stories for the given analysis result.

    Calls the LLM to reason about the epic's content and produce
    stories with epic-specific rationale.  Raises ``LLMError`` on
    LLM failures and ``json.JSONDecodeError`` on unparseable output
    so the caller can surface errors explicitly.  Post-filters
    the LLM output to only include enabled categories.

    Parameters:
    - analysis: dict from build_analysis_result (or get_analysis_data)
    - model: litellm model string
    - temperature: sampling temperature
    - categories: which categories the LLM should produce
    - category_guidance: per-category trigger/criteria from config
    - story_points_guidance: sizing guidance from config
    """
    has_obs_gaps = bool(analysis.get("gaps"))
    has_llm_categories = categories and any(
        c in ("docs", "qe") for c in categories
    )
    if not has_obs_gaps and not has_llm_categories:
        logger.info(
            "No gaps and no LLM categories for %s — skipping",
            analysis.get("epic_key", "unknown"),
        )
        return []

    user_prompt = build_story_composition_prompt(
        analysis,
        categories=categories,
        category_guidance=category_guidance,
        story_points_guidance=story_points_guidance,
    )

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    response_format = {
        "type": "json_schema",
        "json_schema": {
            "name": "epic_stories",
            "schema": STORY_JSON_SCHEMA,
        },
    }

    gaps = analysis.get("gaps", [])
    logger.info(
        "Calling LLM (%s) for %s with %d gaps",
        model,
        analysis.get("epic_key", "unknown"),
        len(gaps),
    )

    raw = complete(
        model=model,
        messages=messages,
        response_format=response_format,
        temperature=temperature,
    )

    parsed = parse_json_response(raw)

    allowed = set(categories) if categories else None
    stories: list[StoryPayload] = []
    for item in parsed.get("stories", []):
        cat = item.get("category", "")
        if allowed and cat not in allowed:
            logger.warning(
                "Dropping story with category %r (not in %s)",
                cat, allowed,
            )
            continue
        sp = item.get("story_points")
        try:
            sp_int = _clamp_fibonacci(int(sp)) if sp is not None else None
        except (ValueError, TypeError):
            logger.warning(
                "Invalid story_points %r for %s, ignoring",
                sp, item.get("summary", ""),
            )
            sp_int = None
        stories.append(
            StoryPayload(
                category=cat,
                summary=item.get("summary", ""),
                description=item.get("description", ""),
                story_points=sp_int,
            )
        )

    logger.info(
        "LLM produced %d stories for %s",
        len(stories),
        analysis.get("epic_key", "unknown"),
    )
    return stories


def estimate_story_points(
    epic_summary: str,
    epic_description: str,
    stories: list[dict[str, str]],
    model: str = "gpt-4o",
    temperature: float = 0.2,
    story_points_guidance: str = "",
) -> dict[str, int]:
    """Estimate story points for a batch of unsized stories.

    Returns a mapping of issue_key -> story_points for stories
    the LLM was able to estimate.

    Parameters:
    - epic_summary: parent epic summary for context
    - epic_description: parent epic description for context
    - stories: list of dicts with keys: key, summary, description
    - model: litellm model string
    - temperature: sampling temperature
    - story_points_guidance: sizing guidance from config
    """
    if not stories:
        return {}

    user_prompt = build_sp_estimation_prompt(
        epic_summary=epic_summary,
        epic_description=epic_description,
        stories=stories,
        story_points_guidance=story_points_guidance,
    )

    messages = [
        {"role": "system", "content": SP_ESTIMATION_SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    response_format = {
        "type": "json_schema",
        "json_schema": {
            "name": "sp_estimates",
            "schema": SP_ESTIMATION_JSON_SCHEMA,
        },
    }

    logger.info(
        "Estimating SP for %d stories (model: %s)",
        len(stories), model,
    )

    raw = complete(
        model=model,
        messages=messages,
        response_format=response_format,
        temperature=temperature,
    )

    parsed = parse_json_response(raw)

    result: dict[str, int] = {}
    for item in parsed.get("estimates", []):
        key = item.get("issue_key", "")
        sp = item.get("story_points")
        if key and sp is not None:
            raw_sp = int(sp)
            sp_int = _clamp_fibonacci(raw_sp)
            if sp_int != raw_sp:
                logger.warning(
                    "Non-Fibonacci SP %d for %s, clamped to %d",
                    raw_sp, key, sp_int,
                )
            result[key] = sp_int
            rationale = item.get("rationale", "")
            if rationale:
                logger.debug(
                    "SP estimate %s=%d: %s", key, sp_int, rationale,
                )

    logger.info("Estimated SP for %d stories", len(result))
    return result

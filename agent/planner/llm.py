"""Thin litellm wrapper for LLM completion calls.

Abstracts away the LLM provider so callers only deal with messages
and structured output.  Supports any provider litellm supports
(OpenAI, Anthropic, Ollama, Azure, etc.) via model string prefixes.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


def complete(
    model: str,
    messages: list[dict[str, str]],
    response_format: dict[str, Any] | None = None,
    temperature: float = 0.2,
) -> str:
    """Call an LLM via litellm and return the response text.

    Parameters:
    - model: litellm model string, e.g. "gpt-4o", "anthropic/claude-sonnet-4-20250514"
    - messages: chat messages (system + user)
    - response_format: optional JSON schema for structured output
    - temperature: sampling temperature
    """
    try:
        import litellm
    except ImportError:
        raise ImportError(
            "litellm is required for the agent planner. "
            "Install it with: pip install litellm"
        )

    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
    }
    if response_format:
        kwargs["response_format"] = response_format

    response = litellm.completion(**kwargs)
    return response.choices[0].message.content or ""


def parse_json_response(text: str) -> dict[str, Any]:
    """Extract and parse JSON from an LLM response.

    Handles responses wrapped in markdown code fences.
    """
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        start = 1
        end = len(lines) - 1
        if lines[-1].strip() == "```":
            end = len(lines) - 1
        cleaned = "\n".join(lines[start:end])

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        logger.warning(
            "Failed to parse LLM response as JSON, "
            "attempting to find JSON object in response"
        )
        brace_start = text.find("{")
        brace_end = text.rfind("}") + 1
        if brace_start >= 0 and brace_end > brace_start:
            return json.loads(text[brace_start:brace_end])
        raise

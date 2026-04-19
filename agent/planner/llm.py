"""Thin litellm wrapper for LLM completion calls.

Abstracts away the LLM provider so callers only deal with messages
and structured output.  Supports any provider litellm supports
(OpenAI, Anthropic, Ollama, Azure, etc.) via model string prefixes.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

_MAX_RETRIES = 3
_INITIAL_BACKOFF_S = 2.0
_DEFAULT_TIMEOUT_S = 120


def complete(
    model: str,
    messages: list[dict[str, str]],
    response_format: dict[str, Any] | None = None,
    temperature: float = 0.2,
    timeout: int = _DEFAULT_TIMEOUT_S,
) -> str:
    """Call an LLM via litellm and return the response text.

    Retries up to 3 times with exponential backoff on transient
    errors (network, rate-limit, auth).  Returns empty string when
    all retries are exhausted so callers can degrade gracefully.

    Parameters:
    - model: litellm model string, e.g. "gpt-4o", "anthropic/claude-sonnet-4-20250514"
    - messages: chat messages (system + user)
    - response_format: optional JSON schema for structured output
    - temperature: sampling temperature
    - timeout: request timeout in seconds
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
        "timeout": timeout,
    }
    if response_format:
        kwargs["response_format"] = response_format

    last_err: Exception | None = None
    for attempt in range(_MAX_RETRIES):
        try:
            response = litellm.completion(**kwargs)
            usage = getattr(response, "usage", None)
            if usage:
                logger.info(
                    "LLM usage: model=%s prompt=%d completion=%d total=%d",
                    model,
                    getattr(usage, "prompt_tokens", 0),
                    getattr(usage, "completion_tokens", 0),
                    getattr(usage, "total_tokens", 0),
                )
            return response.choices[0].message.content or ""
        except Exception as exc:
            last_err = exc
            if attempt < _MAX_RETRIES - 1:
                delay = _INITIAL_BACKOFF_S * (2 ** attempt)
                logger.warning(
                    "LLM call failed (attempt %d/%d), retrying in %.1fs: %s",
                    attempt + 1, _MAX_RETRIES, delay, exc,
                )
                time.sleep(delay)
            else:
                logger.error(
                    "LLM call failed after %d attempts: %s",
                    _MAX_RETRIES, exc,
                )

    return ""


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

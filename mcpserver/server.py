#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "mcp>=1.10.1",
#     "httpx>=0.28.1",
#     "jira>=3.8.0",
#     "pyyaml>=6.0",
# ]
# ///
"""
CNV Epic Agent — MCP Server

Exposes deterministic tools for Jira epic analysis, code-level
observability discovery, and story creation.  LLM reasoning lives in
the agent layer (agent/), not here.

Run with: JIRA_TOKEN=xxx uv run mcpserver/server.py
"""

import logging
import os
import sys
from typing import Any

import yaml
from mcp.server.fastmcp import FastMCP

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from mcpserver.jira.tools import register_jira_tools
from mcpserver.github.tools import register_github_tools
from mcpserver.tools.config import register_config_tools
from prompts.templates import get_system_prompt, build_story_composition_prompt

logger = logging.getLogger(__name__)

CONFIG_PATH = os.path.join(_project_root, "config.yaml")


def load_config() -> dict[str, Any]:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def get_inventory(cfg: dict[str, Any]) -> Any:
    """Load the full discovered inventory from the cache if available."""
    try:
        from mcpserver.github.discover import build_all_inventories

        return build_all_inventories(cfg)
    except Exception:
        logger.warning(
            "Failed to build observability inventory", exc_info=True,
        )
        return None


server = FastMCP("cnv-epic-agent")

register_jira_tools(server)
register_github_tools(server)
register_config_tools(server)


@server.prompt()
async def compose_observability_stories(epic_key: str) -> str:
    """Compose observability stories for an epic using LLM reasoning.

    Returns a structured prompt containing the full analysis evidence
    for the given epic.  The MCP client's LLM uses this to write
    epic-specific story descriptions with rationale.

    Parameters:
    - epic_key: the Jira epic key (e.g. CNV-12345)
    """
    from agent.analyzer.analysis import build_analysis_result
    from mcpserver.jira.client import get_jira_client, fetch_epic_with_children

    cfg = load_config()
    agent_cfg = cfg.get("agent", {})
    client = get_jira_client(cfg)
    epic, children = fetch_epic_with_children(client, cfg, epic_key)
    inv = get_inventory(cfg)
    result = build_analysis_result(epic, children, cfg, inventory=inv)

    sp_cfg = agent_cfg.get("story_points", {})
    prompt_text = build_story_composition_prompt(
        result,
        categories=agent_cfg.get("enabled_categories"),
        category_guidance=agent_cfg.get("category_guidance"),
        story_points_guidance=(
            sp_cfg.get("guidance", "") if sp_cfg.get("enabled") else ""
        ),
        include_schema=True,
    )
    cats = agent_cfg.get("enabled_categories")
    return f"{get_system_prompt(cats)}\n\n---\n\n{prompt_text}"


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] in ("--help", "-h"):
        print(__doc__)
        sys.exit(0)
    server.run()


if __name__ == "__main__":
    main()

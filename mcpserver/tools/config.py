"""Configuration MCP tools: get_config, refresh_cache."""

from __future__ import annotations

from typing import Any

import yaml

from mcpserver.github.discover import invalidate_cache


def register_config_tools(server: Any) -> None:
    """Register config-related tools on the FastMCP server."""

    @server.tool()
    async def get_config() -> str:
        """Show the current scanner configuration.

        Returns the active keyword lists, JQL templates, need-assessment
        rules, proposal library, and story templates.
        """
        from mcpserver.server import load_config

        cfg = load_config()
        return (
            f"```yaml\n"
            f"{yaml.dump(cfg, default_flow_style=False, sort_keys=False)}"
            f"\n```"
        )

    @server.tool()
    async def refresh_cache(branch: str = "") -> str:
        """Force re-scan of all repos by clearing the in-memory cache.

        Use this after upstream code changes to get fresh results from
        list_metrics, list_alerts, list_dashboards, and other discovery
        tools.

        Parameters:
        - branch: branch cache key to clear (default: all)
        """
        if branch:
            invalidate_cache(branch)
            return (
                f"Cache cleared for branch '{branch}'. "
                "Next query will re-scan all repos."
            )
        invalidate_cache("")
        return "Cache cleared. Next query will re-scan all repos."

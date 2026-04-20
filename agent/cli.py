#!/usr/bin/env python3
"""CLI entrypoint for the CNV Epic Agent.

Usage examples::

    # Dry-run scan of recent epics
    python -m agent.cli --version 4.22

    # Analyze a single epic
    python -m agent.cli --epic CNV-84388 --version 4.22

    # Run only specific categories
    python -m agent.cli --version 4.22 --categories metrics,docs,qe

    # Apply (create stories on Jira)
    python -m agent.cli --epic CNV-84388 --version 4.22 --apply

    # Use template-based stories (no LLM)
    python -m agent.cli --version 4.22 --no-llm

    # Override the LLM model
    LLM_MODEL=anthropic/claude-sonnet-4-20250514 python -m agent.cli --version 4.22

    # Use a raw JQL query
    python -m agent.cli --jql 'project = "OpenShift Virtualization" AND type = Epic AND fixVersion = "CNV v4.22.0"'
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="CNV Epic Agent — "
        "scan epics and create stories (observability, docs, QE)",
    )
    parser.add_argument(
        "--epic",
        nargs="*",
        help="Specific epic key(s) to process (default: scan via JQL)",
    )
    parser.add_argument(
        "--jql",
        default=None,
        help=(
            "Raw JQL query to select epics. Bypasses all other "
            "filters (--version, --component, --fix-version, etc.)"
        ),
    )
    parser.add_argument(
        "--version",
        default="",
        help=(
            "CNV version (e.g. 4.22). Auto-derives fixVersion "
            "and Target Version JQL filters using the Jira "
            "version format (e.g. 'CNV v4.22'). Also used to "
            "name the observability epic."
        ),
    )
    parser.add_argument(
        "--since-days",
        type=int,
        default=None,
        help="How far back to scan for epics (overrides config)",
    )
    parser.add_argument(
        "--component",
        default=None,
        help="Filter epics by Jira component name",
    )
    parser.add_argument(
        "--fix-version",
        default=None,
        help="Filter epics by fixVersion",
    )
    parser.add_argument(
        "--target-version",
        default=None,
        help="Filter epics by Target Version",
    )
    parser.add_argument(
        "--label",
        action="append",
        default=None,
        help=(
            "Filter epics by label (repeatable, "
            "e.g. --label gpu --label cnv-4.22)"
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Create stories on Jira (default: dry-run)",
    )
    parser.add_argument(
        "--model",
        default="",
        help="LLM model string (overrides config/env)",
    )
    parser.add_argument(
        "--categories",
        default="",
        help=(
            "Comma-separated list of categories to produce "
            "(e.g. metrics,docs,qe).  Default: all enabled in config."
        ),
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        default=False,
        help="Use template-based stories instead of LLM",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        default=False,
        help="Enable verbose logging",
    )

    args = parser.parse_args()

    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    from agent.runner import run

    categories = (
        [c.strip() for c in args.categories.split(",") if c.strip()]
        if args.categories
        else None
    )

    report = run(
        epic_keys=args.epic,
        jql=args.jql,
        version=args.version,
        since_days=args.since_days,
        component=args.component,
        fix_version=args.fix_version,
        target_version=args.target_version,
        labels=args.label,
        apply=args.apply,
        model=args.model,
        use_llm=not args.no_llm,
        categories=categories,
    )

    print(report)


if __name__ == "__main__":
    main()

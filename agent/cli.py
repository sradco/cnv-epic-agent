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
from datetime import datetime, timezone


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
            "(e.g. metrics,docs,qe). Use 'observability' as a shorthand "
            "for metrics,alerts,dashboards,telemetry. "
            "Default: all enabled in config."
        ),
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        default=False,
        help="Use template-based stories instead of LLM",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        default=False,
        help="Force fresh inventory scan (skip filesystem cache)",
    )
    parser.add_argument(
        "--save-plan",
        nargs="?",
        const="",
        default=None,
        metavar="FILE",
        help=(
            "Dry-run only: save proposed stories as JSON. "
            "Omit FILE to auto-name as plan-<timestamp>.json. "
            "The plan can be reviewed/edited and later applied with "
            "--apply-plan without re-running the LLM."
        ),
    )
    parser.add_argument(
        "--apply-plan",
        default=None,
        metavar="FILE",
        help=(
            "Load a plan file saved by --save-plan and create the "
            "stories in Jira without re-running the LLM. "
            "Skips all epic scanning, inventory, and analysis."
        ),
    )
    parser.add_argument(
        "--config", "-c",
        default=None,
        help=(
            "Path to config.yaml "
            "(default: config.yaml in project root)"
        ),
    )
    parser.add_argument(
        "--output", "-o",
        nargs="?",
        const="",
        default=None,
        help=(
            "Write report to a file (in addition to stdout). "
            "Omit the filename to auto-name as report-<timestamp>.md "
            "(or .html when --format html is set or filename ends in .html). "
            "A UTC timestamp is always appended before the extension."
        ),
    )
    parser.add_argument(
        "--format",
        choices=["markdown", "html"],
        default="markdown",
        help=(
            "Output format: 'markdown' (default) or 'html'. "
            "HTML generates a self-contained, shareable page "
            "with collapsible epic sections and styled tables."
        ),
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

    if args.apply_plan and args.save_plan:
        parser.error("--apply-plan and --save-plan are mutually exclusive.")
    if args.apply_plan and args.apply:
        parser.error(
            "--apply-plan already implies apply mode; "
            "do not pass --apply together with --apply-plan."
        )

    from agent.runner import apply_plan, run

    _run_timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")

    if args.apply_plan:
        report = apply_plan(
            args.apply_plan,
            config_path=args.config,
            epic_keys=args.epic,
        )
    else:
        categories = (
            [c.strip() for c in args.categories.split(",") if c.strip()]
            if args.categories
            else None
        )

        save_plan_path: str | None = None
        if args.save_plan is not None:
            if args.apply:
                parser.error(
                    "--save-plan is for dry-run mode; "
                    "remove --apply or use --apply-plan instead."
                )
            if args.save_plan:
                base, _ext = os.path.splitext(args.save_plan)
                save_plan_path = f"{base}-{_run_timestamp}.json"
            else:
                save_plan_path = f"plan-{_run_timestamp}.json"

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
            config_path=args.config,
            no_cache=args.no_cache,
            save_plan_path=save_plan_path,
        )

    log = logging.getLogger(__name__)

    # Auto-detect format from the output filename extension.
    output_format = args.format
    if args.output:
        ext_lower = os.path.splitext(args.output)[1].lower()
        if ext_lower == ".html":
            output_format = "html"
        elif ext_lower == ".xlsx":
            output_format = "xlsx"

    # Render the markdown / html text for console output.
    if output_format == "html":
        from agent.export.html_report import markdown_to_html
        rendered = markdown_to_html(report.report)
    elif output_format == "xlsx":
        rendered = report.report   # print markdown to console
    else:
        rendered = report.report

    print(rendered)

    if args.output is not None:
        if output_format == "xlsx":
            # Write XLSX workbook (no timestamp suffix for xlsx — the
            # base name already identifies the run).
            if args.output:
                base, _ = os.path.splitext(args.output)
                xlsx_path = f"{base}-{_run_timestamp}.xlsx"
            else:
                xlsx_path = f"report-{_run_timestamp}.xlsx"

            from agent.export.xlsx_report import build_xlsx
            jira_url = ""
            try:
                import yaml
                with open(args.config or "config.yaml", encoding="utf-8") as f:
                    _cfg = yaml.safe_load(f)
                jira_url = _cfg.get("jira", {}).get(
                    "url", "https://redhat.atlassian.net"
                )
            except Exception:
                pass

            build_xlsx(
                xlsx_path,
                metadata=report.metadata,
                tallies=report.tallies,
                plan_collector=report.plan_collector,
                jira_url=jira_url,
            )
            log.info("XLSX report written to %s", xlsx_path)
        else:
            # args.output is "" when --output is given without a filename.
            ext = ".html" if output_format == "html" else ".md"
            if args.output:
                base, _ = os.path.splitext(args.output)
                output_path = f"{base}-{_run_timestamp}{ext}"
            else:
                output_path = f"report-{_run_timestamp}{ext}"
            with open(output_path, "w", encoding="utf-8") as fh:
                fh.write(rendered)
                fh.write("\n")
            log.info("Report written to %s", output_path)


if __name__ == "__main__":
    main()

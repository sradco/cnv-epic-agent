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
        "--summary-only",
        action="store_true",
        default=False,
        help=(
            "Fetch epics and emit only the Summary sheet (XLSX) or "
            "the summary tables (markdown/HTML). Skips LLM, inventory "
            "scan, and story generation. Proposed-SP columns are omitted "
            "from the XLSX Summary sheet. Useful for a quick inventory "
            "of epics with existing story points."
        ),
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
        "--apply-xlsx",
        default=None,
        metavar="FILE",
        help=(
            "Load an XLSX report (produced by --output report.xlsx) "
            "and create stories for every row where 'Approved?' is "
            "non-empty. The workbook is self-contained — no companion "
            "JSON file is required."
        ),
    )
    parser.add_argument(
        "--output-gsheet",
        action="store_true",
        default=False,
        help=(
            "Create a Google Sheet report in addition to the normal "
            "output. Requires Google credentials (see config.yaml "
            "google: section). Prints the sheet URL on completion."
        ),
    )
    parser.add_argument(
        "--apply-gsheet",
        default=None,
        metavar="URL_OR_ID",
        help=(
            "Load a Google Sheet report (created by --output-gsheet) "
            "and create stories for every row where 'Approved?' is "
            "non-empty. Pass the full sheet URL or the bare spreadsheet ID."
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
    if args.apply_xlsx and args.apply_plan:
        parser.error(
            "--apply-xlsx and --apply-plan are mutually exclusive."
        )
    if args.apply_xlsx and args.apply:
        parser.error(
            "--apply-xlsx already implies apply mode; "
            "do not pass --apply together with --apply-xlsx."
        )
    if args.apply_xlsx and args.save_plan:
        parser.error(
            "--apply-xlsx and --save-plan are mutually exclusive."
        )
    if args.apply_gsheet and (
        args.apply_plan or args.apply_xlsx or args.save_plan or args.apply
    ):
        parser.error(
            "--apply-gsheet is mutually exclusive with "
            "--apply-plan, --apply-xlsx, --save-plan, and --apply."
        )

    from agent.runner import apply_gsheet, apply_plan, apply_xlsx, run

    _run_timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")

    if args.apply_gsheet:
        report = apply_gsheet(
            args.apply_gsheet,
            config_path=args.config,
            epic_keys=args.epic,
        )
    elif args.apply_xlsx:
        report = apply_xlsx(
            args.apply_xlsx,
            config_path=args.config,
            epic_keys=args.epic,
        )
    elif args.apply_plan:
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

        # --summary-only implies no LLM and no story generation.
        use_llm = not args.no_llm and not args.summary_only

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
            use_llm=use_llm,
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
            _ver_slug = (
                f"-{args.version.replace(' ', '_')}" if args.version else ""
            )
            if args.output:
                base, _ = os.path.splitext(args.output)
                xlsx_path = f"{base}{_ver_slug}-{_run_timestamp}.xlsx"
            else:
                xlsx_path = f"report{_ver_slug}-{_run_timestamp}.xlsx"

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
                summary_only=args.summary_only,
                tallies=report.tallies,
                plan_collector=report.plan_collector,
                jira_url=jira_url,
            )
            log.info("XLSX report written to %s", xlsx_path)
        else:
            # args.output is "" when --output is given without a filename.
            ext = ".html" if output_format == "html" else ".md"
            _ver_slug = (
                f"-{args.version.replace(' ', '_')}" if args.version else ""
            )
            if args.output:
                base, _ = os.path.splitext(args.output)
                output_path = f"{base}{_ver_slug}-{_run_timestamp}{ext}"
            else:
                output_path = f"report{_ver_slug}-{_run_timestamp}{ext}"
            with open(output_path, "w", encoding="utf-8") as fh:
                fh.write(rendered)
                fh.write("\n")
            log.info("Report written to %s", output_path)

    if args.output_gsheet:
        from agent.export.gsheet_report import build_gsheet
        import yaml as _yaml
        from schemas.config import GoogleConfig
        _gcfg_raw: dict = {}
        try:
            with open(args.config or "config.yaml", encoding="utf-8") as _f:
                _gcfg_raw = _yaml.safe_load(_f).get("google", {}) or {}
        except Exception:
            pass
        _gcfg = GoogleConfig(
            credentials_file=str(
                _gcfg_raw.get("credentials_file", "")
            ),
            drive_folder_id=str(
                _gcfg_raw.get("drive_folder_id", "")
            ),
        )
        sheet_url = build_gsheet(
            report.metadata,
            report.tallies,
            report.plan_collector,
            google_cfg=_gcfg,
            summary_only=getattr(args, "summary_only", False),
        )
        log.info("Google Sheet created: %s", sheet_url)
        print(f"\nGoogle Sheet: {sheet_url}")


if __name__ == "__main__":
    main()

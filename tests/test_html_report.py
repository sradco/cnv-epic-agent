"""Snapshot-style tests for agent.export.html_report.markdown_to_html."""

import pytest

_SAMPLE_REPORT = """\
# Epic Agent Run (DRY-RUN)

- **Date:** 2026-05-05 12:00 UTC
- **Epics:** 2
- **Run ID:** abc123

---

## Summary

### Epic Planning Overview

| Epic | Status | Fix Ver | Target Ver | Dev SP | QE SP | Docs SP |
| --- | --- | --- | --- | --- | --- | --- |
| [CNV-100](#cnv-100) | groomed | CNV 5.0 | CNV v5.0.0 | 10 (+5) | 3 (+2) | no-doc |
| [CNV-200](#cnv-200) | needs grooming | - | - | 0 | no-qe | 0 |
| [CNV-300](#cnv-300) | error | - | - | 0 | 0 | 0 |

### Agent Proposed Stories

| Epic | Status | metrics | Total |
| --- | --- | --- | --- |
| [CNV-100](#cnv-100) | groomed | 2 | 2 |

| Metric | Count |
|---|---|
| Epics processed | 2 |
| Stories would create | 2 |

<a id="cnv-100"></a>
## [CNV-100](https://example.com/browse/CNV-100) — Add VM migration metrics

Components: kubevirt
Gaps: missing migration metrics

- WOULD CREATE: kubevirt_vmi_migration_succeeded (3sp)

  **Category:** metrics

  > Track the number of successful VM migrations per node.

<a id="cnv-200"></a>
## [CNV-200](https://example.com/browse/CNV-200) — NEEDS GROOMING

Epic needs more detail before analysis.

<a id="cnv-300"></a>
## CNV-300 — ERROR (fetch failed)
"""


@pytest.fixture
def html():
    from agent.export.html_report import markdown_to_html
    return markdown_to_html(_SAMPLE_REPORT)


class TestHtmlStructure:
    def test_is_valid_html_document(self, html):
        assert html.startswith("<!DOCTYPE html>")
        assert "</html>" in html

    def test_has_head_and_body(self, html):
        assert "<head>" in html
        assert "<body>" in html

    def test_has_inline_css(self, html):
        assert "<style>" in html
        assert "font-family" in html

    def test_has_title(self, html):
        assert "<title>CNV Epic Agent Report</title>" in html

    def test_has_report_wrap_div(self, html):
        assert 'class="report-wrap"' in html

    def test_h1_rendered(self, html):
        assert "<h1>" in html
        assert "DRY-RUN" in html


class TestHtmlTables:
    def test_planning_overview_table_present(self, html):
        assert "Epic Planning Overview" in html
        assert "Fix Ver" in html
        assert "Dev SP" in html

    def test_agent_stories_table_present(self, html):
        assert "Agent Proposed Stories" in html

    def test_table_tag_present(self, html):
        assert "<table" in html

    def test_table_rows_present(self, html):
        assert "<tr>" in html or "<tr " in html


class TestHtmlBadges:
    def test_groomed_badge_green(self, html):
        assert "badge-green" in html

    def test_needs_grooming_badge_yellow(self, html):
        assert "badge-yellow" in html

    def test_error_badge_red(self, html):
        assert "badge-red" in html

    def test_badge_span_present(self, html):
        assert 'class="badge' in html


class TestHtmlCollapsible:
    def test_details_sections_present(self, html):
        assert "<details" in html

    def test_summary_tag_present(self, html):
        assert "<summary>" in html

    def test_details_body_div_present(self, html):
        assert 'class="details-body"' in html

    def test_section_closed(self, html):
        count_open = html.count("<details")
        count_close = html.count("</details>")
        assert count_open == count_close


class TestHtmlLinks:
    def test_jira_links_preserved(self, html):
        assert "https://example.com/browse/CNV-100" in html

    def test_anchor_ids_preserved(self, html):
        assert 'id="cnv-100"' in html
        assert 'id="cnv-200"' in html

    def test_internal_fragment_links_preserved(self, html):
        assert "#cnv-100" in html


class TestHtmlNoMarkdownSyntax:
    def test_no_raw_pipe_tables(self, html):
        # Markdown table syntax must not appear in the output
        assert "| --- |" not in html

    def test_no_raw_hash_headings(self, html):
        # Markdown heading syntax must not appear in the output
        assert html.count("\n# ") == 0
        assert html.count("\n## ") == 0

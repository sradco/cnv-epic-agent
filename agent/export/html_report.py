"""Convert the agent's markdown report to a self-contained HTML page.

The internal report structure stays as markdown lines — HTML conversion
happens only at the final output stage.  All CSS is inlined so the
file can be shared without any external dependencies.
"""

from __future__ import annotations

import html
import re

import mistune

# Matches the epic-section ## headings produced by the runner:
# - "[CNV-12345](url) — summary text"  (epic detail sections)
# - "Summary"                           (the top-level summary section)
# - "CNV-12345 — ..."                  (no-version fallback)
_EPIC_HEADING_RE = re.compile(
    r"^(\[CNV-\d+\]|CNV-\d+\b|Summary$)",
    re.IGNORECASE,
)

# ─── Status badge colours ──────────────────────────────────────────────────

_BADGE_CLASSES: dict[str, str] = {
    "groomed": "badge-green",
    "needs grooming": "badge-yellow",
    "nothing to do": "badge-grey",
    "error": "badge-red",
    "llm error": "badge-red",
}

_STATUS_PATTERN = re.compile(
    r"\b(groomed|needs grooming|nothing to do|error|llm error)\b",
    re.IGNORECASE,
)

# ─── CSS ───────────────────────────────────────────────────────────────────

_CSS = """
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
    "Helvetica Neue", Arial, sans-serif;
  font-size: 15px;
  line-height: 1.6;
  color: #1a1a1a;
  background: #f6f8fa;
  margin: 0;
  padding: 24px;
}
.report-wrap {
  max-width: 1200px;
  margin: 0 auto;
  background: #fff;
  border-radius: 8px;
  box-shadow: 0 1px 4px rgba(0,0,0,.1);
  padding: 32px 40px;
}
h1 { font-size: 1.7em; border-bottom: 2px solid #e1e4e8; padding-bottom: 8px; }
h2 { font-size: 1.25em; margin-top: 28px; color: #24292f; }
h3 { font-size: 1.05em; margin-top: 20px; color: #444; }
h4 { font-size: 0.95em; margin-top: 14px; color: #555; font-weight: 600; }
hr { border: none; border-top: 1px solid #e1e4e8; margin: 24px 0; }
a { color: #0969da; text-decoration: none; }
a:hover { text-decoration: underline; }
code { background: #f0f2f4; padding: 2px 5px; border-radius: 3px;
  font-family: "SFMono-Regular", Consolas, monospace; font-size: 0.88em; }
pre { background: #f0f2f4; padding: 14px; border-radius: 6px; overflow-x: auto; }
pre code { background: none; padding: 0; }
blockquote {
  border-left: 4px solid #d0d7de;
  color: #57606a;
  margin: 12px 0;
  padding: 4px 16px;
}

/* Tables */
table {
  border-collapse: collapse;
  width: 100%;
  margin: 12px 0 20px;
  font-size: 0.92em;
}
th, td {
  border: 1px solid #d0d7de;
  padding: 7px 12px;
  text-align: left;
}
th { background: #f6f8fa; font-weight: 600; }
tr:nth-child(even) td { background: #f8fafc; }

/* Status badges */
.badge {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 12px;
  font-size: 0.78em;
  font-weight: 600;
  white-space: nowrap;
}
.badge-green  { background: #dafbe1; color: #116329; }
.badge-yellow { background: #fff8c5; color: #7d4e00; }
.badge-grey   { background: #eaeef2; color: #57606a; }
.badge-red    { background: #ffebe9; color: #cf222e; }

/* Collapsible epic sections */
details {
  border: 1px solid #e1e4e8;
  border-radius: 6px;
  margin: 10px 0;
}
details > summary {
  cursor: pointer;
  padding: 10px 14px;
  background: #f6f8fa;
  font-weight: 600;
  border-radius: 6px 6px 0 0;
  list-style: none;
}
details[open] > summary { border-bottom: 1px solid #e1e4e8; border-radius: 6px 6px 0 0; }
details > summary::-webkit-details-marker { display: none; }
details > summary::before { content: "▶ "; font-size: 0.75em; color: #888; }
details[open] > summary::before { content: "▼ "; }
.details-body { padding: 14px 18px; }
"""

# ─── Custom Mistune renderer ────────────────────────────────────────────────


class _EpicRenderer(mistune.HTMLRenderer):
    """Wraps ``##`` headings (epic sections) in collapsible details blocks.

    The table plugin registers ``render_table`` as a free function and
    installs it on the renderer instance at parse time (mistune 3.x),
    so we override it here to add a CSS class to every table.
    """

    def __init__(self) -> None:
        super().__init__(escape=False)
        self._in_section = False

    def heading(self, text: str, level: int, **attrs) -> str:
        if level == 1:
            return f"<h1>{text}</h1>\n"
        if level == 2:
            # Strip HTML tags from text for the pattern match so that
            # anchor tags around the epic key don't break the regex.
            plain = re.sub(r"<[^>]+>", "", text)
            if _EPIC_HEADING_RE.match(plain.strip()):
                # Top-level epic / summary section → collapsible block.
                # The Summary section starts open; epic sections start
                # collapsed so the report is scannable by default.
                prefix = "</div></details>\n" if self._in_section else ""
                self._in_section = True
                summary_text = _badges_in_heading(text)
                is_summary = plain.strip() == "Summary"
                open_attr = " open" if is_summary else ""
                return (
                    f"{prefix}"
                    f'<details{open_attr}>\n'
                    f"<summary>{summary_text}</summary>\n"
                    f'<div class="details-body">\n'
                )
            # Sub-headings inside story descriptions (e.g. "Why this
            # matters") — render as <h4> so they stay visually nested
            # inside the current collapsible section.
            return f"<h4>{text}</h4>\n"
        if level == 3:
            return f"<h3>{text}</h3>\n"
        return f"<h{level}>{text}</h{level}>\n"



def _badges_in_heading(text: str) -> str:
    """Replace known status words in heading HTML with coloured badges."""
    def _replace(m: re.Match) -> str:
        word = m.group(0)
        cls = _BADGE_CLASSES.get(word.lower(), "badge-grey")
        return f'<span class="badge {cls}">{html.escape(word)}</span>'
    return _STATUS_PATTERN.sub(_replace, text)


def _inline_badges(text: str) -> str:
    """Add badge spans to status words in table cells (plain text rows)."""
    def _replace(m: re.Match) -> str:
        word = m.group(0)
        cls = _BADGE_CLASSES.get(word.lower(), "badge-grey")
        return f'<span class="badge {cls}">{html.escape(word)}</span>'
    return _STATUS_PATTERN.sub(_replace, text)


# ─── Public API ────────────────────────────────────────────────────────────


def markdown_to_html(report_text: str) -> str:
    """Convert a markdown agent report to a self-contained HTML document.

    Uses mistune 3.x with the table plugin. Embedded HTML anchor tags
    (``<a id="...">…</a>``) pass through verbatim because
    ``escape=False`` is set on the renderer.
    """
    renderer = _EpicRenderer()
    md = mistune.create_markdown(
        renderer=renderer,
        plugins=["table"],
    )

    # Patch the table renderer to add a CSS class.
    # In mistune 3.x the table plugin registers free functions such as
    # render_table on the renderer after create_markdown is called, so
    # we wrap it here to inject the class attribute.
    _orig_render_table = getattr(renderer, "render_table", None)
    if _orig_render_table is not None:
        def _styled_table(text: str) -> str:
            out = _orig_render_table(text)
            return out.replace("<table>", '<table class="agent-table">', 1)
        renderer.render_table = _styled_table  # type: ignore[method-assign]

    body_html = md(report_text)
    # Close the last collapsible section if one is still open
    if renderer._in_section:
        body_html += "</div></details>\n"
        renderer._in_section = False
    # Apply badge spans to status words inside table cells
    body_html = _inline_badges(body_html)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>CNV Epic Agent Report</title>
  <style>{_CSS}</style>
</head>
<body>
<div class="report-wrap">
{body_html}
</div>
</body>
</html>
"""

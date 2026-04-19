# CNV Epic Agent

An agent and MCP server for CNV epic management.  Scans Jira epics
for monitoring gaps, discovers existing metrics/alerts/dashboards
in upstream repos, and creates stories — using either deterministic
templates or LLM-assisted reasoning.

Supports pluggable story categories: observability (metrics, alerts,
dashboards, telemetry), docs, and QE — with LLM-estimated story points.

## Architecture

```
cnv-epic-agent/
  mcp/                — MCP server (deterministic tools, no LLM)
    server.py         — FastMCP entrypoint, registers all tools + prompts
    jira/             — Jira tools (scan, analyze, create) + client
    github/           — Code discovery tools + scanner
    monitoring/       — Placeholder for live-cluster tools
    tools/            — Config tools (get_config, refresh_cache)
  agent/              — Standalone agent (LLM reasoning, CLI/CI)
    cli.py            — CLI entrypoint
    runner.py         — Orchestrator: discover -> analyze -> plan -> apply
    analyzer/         — Gap analysis (analysis.py, formatter.py)
    planner/          — LLM story composition (planner.py, llm.py)
    reviewer/         — Placeholder for validation
  schemas/            — Shared data contracts (StoryPayload, AnalysisResult)
  prompts/            — Shared prompt templates (SYSTEM_PROMPT)
  config.yaml         — All settings
  tests/              — Unit tests
```

**Key design principles:**

- **MCP tools are stateless and deterministic** — no LLM calls inside `mcp/`
- **Agent orchestrates LLM reasoning** — only `agent/planner/` talks to an LLM
- **Shared schemas** — both MCP prompts and agent use `schemas/stories.py`
- **Shared prompts** — both `mcp/server.py` prompt and agent use `prompts/templates.py`
- **Pluggable categories** — enabled via `config.yaml` `agent.enabled_categories`

## Quick Start

```bash
# Set credentials
export JIRA_EMAIL="you@redhat.com"
export JIRA_TOKEN="your-atlassian-api-token"
```

## Three Usage Modes

### 1. MCP Server (any MCP client: Cursor, Claude Desktop, etc.)

```bash
uv run mcp/server.py
```

The MCP server exposes tools that any MCP-compatible client can call.
Add to your MCP client config:

```json
{
  "mcpServers": {
    "cnv-epic-agent": {
      "command": "uv",
      "args": ["run", "/path/to/cnv-epic-agent/mcp/server.py"],
      "env": {
        "JIRA_EMAIL": "${JIRA_EMAIL}",
        "JIRA_TOKEN": "${JIRA_TOKEN}"
      }
    }
  }
}
```

### 2. CLI Agent (standalone, LLM-assisted)

```bash
# Dry-run: scan recent epics for CNV 4.22
python -m agent.cli --version 4.22

# Analyze a single epic
python -m agent.cli --epic CNV-84388 --version 4.22

# Run only specific categories
python -m agent.cli --version 4.22 --categories metrics,docs,qe

# Apply: create stories on Jira
python -m agent.cli --epic CNV-84388 --version 4.22 --apply

# Use template-based stories (no LLM)
python -m agent.cli --version 4.22 --no-llm

# Override the LLM model
LLM_MODEL=anthropic/claude-sonnet-4-20250514 python -m agent.cli --version 4.22
```

The CLI agent supports any LLM provider via
[litellm](https://docs.litellm.ai/docs/providers):
OpenAI, Anthropic, Ollama, Azure, etc.

### 3. Daily Automated Runs (CI/cron)

```yaml
# .github/workflows/epic-scan.yml
name: Daily Epic Scan
on:
  schedule:
    - cron: "0 8 * * 1-5"  # weekdays at 8am UTC
  workflow_dispatch:

jobs:
  scan:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v4
      - run: |
          uv pip install litellm jira pyyaml
          python -m agent.cli \
            --version 4.22 \
            --since-days 7 \
            --apply
        env:
          JIRA_EMAIL: ${{ secrets.JIRA_EMAIL }}
          JIRA_TOKEN: ${{ secrets.JIRA_TOKEN }}
          OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
```

## Story Categories

The agent supports pluggable story categories, all configurable
in `config.yaml`:

| Category | Source | Decision |
|----------|--------|----------|
| `metrics` | Code scan + gap analysis | Deterministic |
| `alerts` | Code scan + gap analysis | Deterministic |
| `dashboards` | Code scan + gap analysis | Deterministic |
| `telemetry` | Code scan + CMO allowlist | Deterministic |
| `docs` | Epic content | LLM decides if needed |
| `qe` | Epic content | LLM decides if needed |

Story points are estimated by the LLM for all categories.

## MCP Tools

### Jira Workflow

| Tool | Description |
|------|-------------|
| `scan_epics` | Bulk scan recent CNV epics for observability gaps |
| `analyze_epic` | Deep-dive a single epic with full evidence report |
| `get_analysis_data` | Structured analysis data as JSON for AI reasoning |
| `create_stories` | Batch-create stories (dry-run default) |
| `create_story` | Create a single story with client-provided content |

### Code Discovery

| Tool | Description |
|------|-------------|
| `discover_repo_observability` | Scan a repo for metrics, alerts, rules, dashboards |
| `list_metrics` | List Prometheus metrics across all CNV repos |
| `list_alerts` | List alerting rules across all CNV repos |
| `list_dashboards` | List dashboards, panels, and PromQL queries |
| `search_observability` | Search artifacts by name pattern |
| `refresh_cache` | Force re-scan of all repos |

### Telemetry

| Tool | Description |
|------|-------------|
| `suggest_telemetry` | Propose cluster-level rules for CMO allowlist |
| `list_telemetry` | Show current allowlist and candidates |

### Configuration

| Tool | Description |
|------|-------------|
| `get_config` | Show current scanner configuration |

### MCP Prompt

| Prompt | Description |
|--------|-------------|
| `compose_observability_stories` | Returns a structured prompt with analysis evidence for LLM-assisted story composition |

## Jira Structure

Stories are created under a version-scoped observability epic:

- **Epic:** `[Observability] CNV 4.22 — Auto-generated observability stories`
- **Labels:** `cnv-observability`, `epic-agent-generated`
- **Component:** `CNV Install, Upgrade and Operators`
- Each story is **linked** to the source feature epic
- **Idempotent:** existing stories are skipped
- **Story points** set via custom field when provided by the LLM

## Running Tests

```bash
cd cnv-epic-agent
uv run --with pytest,pyyaml,jira,mcp,httpx pytest tests/ -v
```

## Configuration

All settings are in `config.yaml`:

- `jira:` — JQL templates, default project
- `creation:` — project, component, labels, epic format
- `discovery:` — upstream repo URLs (9 repos)
- `telemetry:` — CMO allowlist URL
- `analysis:` — need-assessment keywords, coverage keywords
- `proposals:` — feature type signals
- `observability_patterns:` — templates for 5 domains
- `agent:` — default LLM model, max stories, enabled categories,
  category guidance, story point estimation
- `subtask_templates:` — story description templates

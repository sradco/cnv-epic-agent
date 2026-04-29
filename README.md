# CNV Epic Agent

An LLM-powered agent that helps groom KubeVirt/CNV Jira epics.
It scans epics, checks whether they have enough detail, and
proposes child stories to fill gaps — interactively from the
terminal or automated via CI/cron.

Current capabilities:

- **Grooming gate** — flags under-specified epics for grooming
  before any analysis runs
- **Observability gap analysis** — discovers existing metrics,
  alerts, and dashboards in upstream repos and proposes stories
  to close coverage gaps
- **Docs & QE story generation** — proposes documentation and
  test verification stories when warranted
- **Story point estimation** — LLM-estimated sizing (Fibonacci)
  for new and existing stories

Story categories are pluggable: observability (metrics, alerts,
dashboards, telemetry), docs, and QE.

## How the Agent Works

The agent processes epics through a multi-stage pipeline. Each epic
goes through the following steps in order:

### 1. Epic Selection

Epics are selected in one of two ways:

- **Explicit keys** — pass `--epic CNV-12345 CNV-67890`
- **JQL scan** — scan recent epics with optional filters:
  `--component`, `--fix-version`, `--target-version`, `--label`

### 2. Grooming Check (two tiers)

Before analysis, the agent checks whether the epic has enough
detail to determine its scope. The check runs in two tiers:

**Tier 1 -- Heuristic (free, instant).** An epic is immediately
flagged if it has a description shorter than 50 characters *and*
zero child issues. This catches truly empty epics without
spending LLM tokens.

**Tier 2 -- LLM clarity check** (when `grooming.llm_clarity_check`
is enabled). Epics that pass the heuristic are sent to the LLM
with their description and child issue summaries. The LLM
evaluates whether the goal, scope, and context are clear enough
for a team to start creating implementation stories. It returns
a verdict (`clear` or `needs_grooming`) and a reason explaining
what detail is missing.

If an epic is flagged by either tier:

- **Dry-run**: the report shows `NEEDS GROOMING` with the reason
- **Apply**: the agent adds a `grooming` label and posts a
  comment with the specific reason (from the LLM when available),
  then skips the epic

Thresholds and settings are configurable in `config.yaml` under
`grooming:`.

### 3. Observability Inventory Discovery

The agent clones and scans all configured upstream repos
(`discovery.repos`) to build an inventory of existing:

- **Prometheus metrics** — scraped from Go source files
- **Alerting rules** — parsed from PrometheusRule YAML files
- **Recording rules** — parsed from rule group definitions
- **Dashboards and panels** — parsed from Grafana/Perses JSON

Results are cached per branch for the lifetime of the process.

### 4. Need Assessment (advisory)

The analyzer scores the epic against two term lists from
`analysis.need_assessment`:

- **needed_terms** — words like "controller", "migration", "latency"
  that suggest runtime behavior needing observability
- **not_needed_terms** — words like "docs update", "typo",
  "release tracker" that suggest no observability is needed

The score is **advisory only** — it is included in the analysis
result for context but does not gate story generation. Every epic
that passes the grooming check is always sent to the LLM.

### 5. Coverage Evaluation

The analyzer checks whether the epic and its children already
mention work on metrics, alerts, or dashboards using keyword
lists from `analysis.coverage_keywords`. Missing categories
become **gaps**.

### 6. Feature Type Detection

The analyzer classifies the epic into feature types
(e.g. `data_path`, `api_controller`, `performance_scale`) using
signal terms from `proposals.feature_type_signals`. Feature types
drive which observability patterns are suggested.

Single-word signals (like "controller") only match in the epic
summary/description — not in child issues — to avoid false
positives from generic terms.

### 7. Proposal Generation

For each gap, the agent generates proposals:

- **Existing items** — metrics/alerts from the inventory that are
  relevant to the epic's domain keywords
- **Proposed items** — new metrics/alerts/dashboards from
  `observability_patterns` templates, parameterized by feature type

Proposals are filtered for grounding: alerts and dashboards are
only proposed if there is metric backing (existing, proposed, or
from inventory). This prevents ungrounded alert proposals.

### 8. Label-Based Category Filtering

Before story generation, the agent checks the epic's Jira labels:

- **`no-doc`** — removes the `docs` category (no docs stories)
- **`no-qe`** — removes the `qe` category (no QE stories)

### 9. Story Composition

Stories are generated in one of two modes:

#### LLM-Assisted (default)

The analysis result is formatted into a structured prompt and sent
to the LLM (via [litellm](https://docs.litellm.ai/docs/providers))
with:

- An **SRE lead persona** — the LLM evaluates proposals from the
  perspective of a cluster operator running production OpenShift
  Virtualization
- **Epic context** — component, labels, description, child issues,
  gaps, and inventory-backed proposals
- **Category-specific rules**:
  - Observability stories must include "Why this matters", "Who
    benefits", "How it is used"
  - Dashboards must serve real operator workflows, prefer adding
    panels to existing dashboards
  - Docs stories only for user-facing changes
  - QE stories split by test type (metric unit tests, alert rule
    validation, dashboard verification, end-to-end, upgrade/rollback)
  - QE distinguishes between genuinely new vs. migrated/refactored
    items
- The LLM may return an **empty list** if the epic doesn't warrant
  new stories

#### Template-Based (`--no-llm`)

Stories are generated from templates in `subtask_templates` in
`config.yaml` — no LLM call is made. Useful for deterministic
runs or when no LLM API is available.

### 10. Deduplication

Each proposed story is checked against:

1. **Existing stories** under the version-scoped observability epic
   that were previously created by the agent
2. **Child issues** of the source epic

Deduplication uses normalized summary matching (case-insensitive,
brackets stripped, whitespace collapsed) and a SHA-256 fingerprint
embedded in each story's description.

### 11. Story Creation

In **dry-run** mode (default), the report shows what would be
created — including full description, category, and story points.

In **apply** mode, stories are created on Jira under a
version-scoped observability epic:

- **Epic:** `[Observability] CNV 4.22 — Auto-generated observability stories`
- **Labels:** `cnv-observability`, `epic-agent-generated`
- **Component:** `CNV Install, Upgrade and Operators`
- Each story is **linked** to the source feature epic
- **Story points** estimated by the LLM (Fibonacci: 1,2,3,5,8,13)

### 12. Story Point Estimation for Existing Issues

When `story_points.estimate_existing` is enabled, the agent also
scans existing unsized stories under the source feature epic and
uses the LLM to estimate their story points.

Closed stories (status: Closed, Done, Resolved, Verified) are
excluded — the agent never assigns or modifies story points on
completed work.

## Architecture

```
cnv-epic-agent/
  agent/              — CLI agent (LLM-assisted)
    cli.py            — CLI entrypoint
    runner.py         — Orchestrator: discover -> analyze -> plan -> apply
    analyzer/         — Gap analysis (analysis.py, formatter.py)
    planner/          — LLM story composition (planner.py, llm.py)
    jira/             — Jira REST client (auth, query, create)
    discovery/        — Code scanning (metrics, alerts, dashboards)
  schemas/            — Shared data contracts (StoryPayload, IssueDoc)
  prompts/            — Shared prompt templates (SYSTEM_PROMPT)
  config.yaml         — All settings
  tests/              — Unit tests
```

**Key design principles:**

- **Single CLI agent** — `agent/planner/` calls LLM via litellm; runs interactively or in CI
- **Pluggable categories** — enabled via `config.yaml` `agent.enabled_categories`
- **Shared schemas and prompts** — `schemas/` and `prompts/` keep data contracts in one place

## Quick Start

```bash
# Set credentials
export JIRA_EMAIL="you@redhat.com"
export JIRA_TOKEN="your-atlassian-api-token"
```

## Usage

### CLI Agent

```bash
# Dry-run: scan recent epics for CNV 4.22
# --version auto-derives JQL filters:
#   fixVersion = "CNV v4.22" OR "Target Version" = "CNV v4.22"
python -m agent.cli --version 4.22

# Filter by component
python -m agent.cli --version 4.22 --component "CNV Virtualization"

# Analyze a single epic (no version filter needed)
python -m agent.cli --epic CNV-84388

# Explicit version overrides (skip auto-derivation)
python -m agent.cli --fix-version "CNV v4.23.0" --label gpu

# Run only specific categories
python -m agent.cli --version 4.22 --categories metrics,docs,qe

# Apply: create stories on Jira
python -m agent.cli --epic CNV-84388 --version 4.22 --apply

# Use template-based stories (no LLM)
python -m agent.cli --version 4.22 --no-llm

# Override the LLM model
LLM_MODEL=gemini/gemini-2.5-flash python -m agent.cli --version 4.22
```

The CLI agent supports any LLM provider via
[litellm](https://docs.litellm.ai/docs/providers):
OpenAI, Anthropic, Ollama, Azure, etc.

### Automated Runs (CI/cron)

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

## Configuration

All settings are in `config.yaml`:

| Section | Purpose |
|---------|---------|
| `jira:` | JQL templates, default project |
| `creation:` | Project, component, labels, epic format, story points field |
| `grooming:` | Label, thresholds, comment for under-specified epics |
| `discovery:` | Upstream repo URLs for inventory scanning |
| `telemetry:` | CMO allowlist URL |
| `analysis:` | Need-assessment keywords, coverage keywords |
| `proposals:` | Feature type signals |
| `observability_patterns:` | Templates for 5 domains (migration, storage, networking, api_controller, performance) |
| `agent:` | Default LLM model, max stories, enabled categories, category guidance, story point estimation |
| `subtask_templates:` | Story description templates for template-based mode |

## Running Tests

```bash
cd cnv-epic-agent
uv run --with pytest,pyyaml,jira,httpx pytest tests/ -v
```

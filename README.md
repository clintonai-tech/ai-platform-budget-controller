# AI Platform Budget Controller

This repository contains a take-home assignment implementation for an AI Platform Engineer role. The selected Task 2 component is **Option B: Tiered budget enforcement with graceful downgrade**.

The component will be a Python CLI that reads accumulated team spend, evaluates declarative budget policy, and emits deterministic enforcement decisions that could later be applied to a LiteLLM gateway.

## Current Scope

Scaffold and inputs are in place (Tickets 1-2):

- Python package layout under `src/`, `uv` dependency management.
- Ruff and pytest configuration, `.gitignore`, `.env.example`.
- Initial command-line entry point.
- Input spend data at `data/spend_30d.csv` (copy of the brief's file).
- Declarative policy at `config/budget_policy.yaml`.

Done: typed domain models, policy loader, spend loader (CSV parsing + aggregation
with data-quality flags), the policy evaluator (deterministic per-team threshold
decisions + governance violations, injected clock, staleness handling), the
LiteLLM policy-intent exporter (dry-run JSON only), and the `evaluate` CLI.
Remaining per [task2-plan.md](task2-plan.md): README / review polish.

### Policy-intent JSON (exporter)

`build_export()` / `export_json()` turn an evaluation into a stable
`schema_version: "1.0"` document: metadata (`as_of`, `snapshot_age_hours`,
`stale`, `hold_relaxations`), a `summary`, an `intents` list, and pass-through
`governance_violations`. It is **dry-run only** — every intent has
`apply: false` and nothing calls LiteLLM. Each intent maps its action to
admin-API-shaped `changes` (throttle → scale RPM/TPM limits; downgrade → reroute
the team alias + `app_signal` + projected saving; block/quarantine → block +
manual `recovery`; warn → notifications). When `hold_relaxations` is set (stale
snapshot), relaxing `allow` intents are `held` instead of clearing enforcement.

## Input Data and Configuration

### Spend data — `data/spend_30d.csv`

The controller treats this file as a **near-real-time snapshot of accumulated
spend** for the current billing period — the stand-in for what would be a query
against LiteLLM / Postgres in production. Spend data is inherently stale: there
is lag between a request and its cost landing in the table. Every decision the
controller emits therefore carries the snapshot's `as_of` date and its age, and
stale data is never used to *lift* an enforcement action (see
`defaults.max_staleness_hours` in the policy).

The sample contains deliberate governance problems the controller is expected to
surface rather than silently absorb: rows with no team (`key-personal-mhuber`,
~$2,003 over three days), a blank `cost_usd` value, a zero-request row with a
non-zero cost, and teams calling models outside their allow-list.

### Policy — `config/budget_policy.yaml`

All enforcement behaviour lives here; the code has no hard-coded team names or
numbers. It defines global tier thresholds (`0.75` / `0.90` / `1.00`), a model
price reference, and per-team budget, allowed models, the action to take at
100%, whether a silent downgrade is acceptable, and an escalation target.

**Monthly budgets are an assumption.** The brief lists the per-team spend
envelope as an open question, so budgets are set from the 30-day sample so that
every tier and both enforcement styles (throttle, downgrade) are exercised:

| Team | 30d spend | Budget | ~% used | Decision |
|---|---:|---:|---:|---|
| DevAgent | $20,153 | $18,000 | 112% | enforce → `throttle` (write-capable, no silent swap) |
| AdvisorChat | $9,180 | $10,000 | 92% | `urgent_warn` (customer-facing, no silent swap) |
| KYC | $2,795 | $3,600 | 78% | `warn` (regulated, block at 100%) |
| DigestBot | $1,990 | $2,600 | 77% | `warn` (batch, downgrade-eligible) |
| Research | $335 | $1,000 | 34% | `allow` |
| Marketing | $8 | $500 | 2% | `allow` |
| _unowned_ | $2,003 | — | — | `quarantine` |

## Local Setup

This project targets **Python 3.13** and uses [`uv`](https://docs.astral.sh/uv/)
for dependency management.

> If conda is your active environment, every `uv run` prints
> `VIRTUAL_ENV=/opt/anaconda3 does not match the project environment path .venv
> and will be ignored`. This is harmless — `uv run` always uses the project
> `.venv`. Run `conda deactivate` first to silence it.

Install dependencies:

```bash
uv sync
```

Run tests:

```bash
uv run pytest
```

Run linting:

```bash
uv run ruff check .
```

Check formatting:

```bash
uv run ruff format --check .
```

Run the CLI:

```bash
# human-readable table (defaults: data/spend_30d.csv, config/budget_policy.yaml)
uv run budget-controller evaluate

# pin the evaluation time so staleness is reproducible
uv run budget-controller evaluate --as-of 2025-12-01

# schema 1.0 policy-intent JSON, and also write it to a file
uv run budget-controller evaluate --format json
uv run budget-controller evaluate --export-litellm intent.json

# CI/cron mode: exit 1 if any enforcement action or governance violation is present
uv run budget-controller evaluate --strict
```

Exit codes: `0` success, `1` findings present under `--strict`, `2` inputs could
not be read or validated.

## Planned Architecture

The first implementation will run in dry-run mode:

```text
spend CSV / future LiteLLM Postgres
        -> budget controller
        -> threshold decisions
        -> LiteLLM policy intent JSON
        -> future Datadog alerts / gateway updates
```

Version 1 will not mutate LiteLLM, call AWS, emit Datadog metrics, or require provider API keys. Those integrations are intentionally deferred so the core policy logic remains deterministic and easy to test.


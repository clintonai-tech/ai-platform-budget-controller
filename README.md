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

Domain models, spend loader, policy evaluator, exporter, and the full CLI are
added in later tickets following [task2-plan.md](task2-plan.md).

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
uv run budget-controller --help
```

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


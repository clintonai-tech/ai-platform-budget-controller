# AI Platform Budget Controller

This repository contains a take-home assignment implementation for an AI Platform Engineer role. The selected Task 2 component is **Option B: Tiered budget enforcement with graceful downgrade**.

The component will be a Python CLI that reads accumulated team spend, evaluates declarative budget policy, and emits deterministic enforcement decisions that could later be applied to a LiteLLM gateway.

## Current Scope

Ticket 1 establishes the project scaffold only:

- Python package layout under `src/`.
- `uv` dependency management.
- Ruff, pytest, and mypy configuration.
- `.gitignore` and `.env.example`.
- Initial command-line entry point.
- Input spend data staged at `data/spend_30d.csv` (copy of the brief's file).

Business logic will be added in later tickets following [task2-plan.md](task2-plan.md).

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

Run type checks:

```bash
uv run mypy src
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


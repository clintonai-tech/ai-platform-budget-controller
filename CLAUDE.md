# CLAUDE.md

## What this is

Take-home for an AI Platform Engineer role at "Scalable Fictional". The platform
gives internal teams governed access to LLMs through a LiteLLM gateway.

- **Assignment**: `docs/AI Platform Engineer - Case Study 2026.pdf`
- **Task 1 — design doc** (done): `task1-design.md`
- **Task 2 — build one component**: chose **Option B — tiered budget enforcement
  with graceful downgrade**. Build plan: `task2-plan.md` (8 tickets).
- **Task 3 — architecture diagram**: not started.

The component (`budget-controller`) is a periodic controller: it reads accumulated
team spend, evaluates declarative budget policy against 75% / 90% / 100%
thresholds, and emits deterministic enforcement decisions (warn, urgent-warn,
downgrade, throttle, block, quarantine) that could later be pushed into LiteLLM.
In this version the spend source is the brief's CSV; there is no live gateway
mutation.

## Layout

| Path | Purpose |
|---|---|
| `src/budget_controller/` | The package. `cli.py` is the Typer entry point. |
| `tests/` | pytest suite. Small deterministic fixtures, not the full CSV. |
| `data/spend_30d.csv` | Input spend data (copy of the brief's file). |
| `config/` | `budget_policy.yaml` — added in Ticket 2. |
| `docs/` | Reference only: assignment PDF, spend CSV, Langfuse sample. |

## Commands

Everything runs through `uv` (Python 3.13 project):

```bash
uv sync                          # install / update .venv from uv.lock
uv run pytest                    # tests
uv run ruff check .              # lint
uv run ruff format --check .     # format check
uv run budget-controller --help  # run the CLI
```

If conda is active you'll see a harmless
`VIRTUAL_ENV=/opt/anaconda3 ... will be ignored` warning on every `uv run`;
`conda deactivate` silences it.

## Conventions

- Python 3.13, `src/` layout, Pydantic v2, Typer, PyYAML.
- **Standard-library `csv` — no pandas.** The input is small and auditability
  matters more than convenience.
- ruff lint rules `E, F, I, UP, B, SIM`, line length 100. Ruff is the only
  static-analysis gate; there is no separate type checker.
- Tests live under `tests/`; `pyproject.toml` sets `pythonpath = ["src"]`.

## Design rules that bind the code

- Policy actions are **enum values**, never free-form prose.
- **No wall-clock in decision logic** — inject `now` / `as_of` so evaluation is
  reproducible and testable.
- **Missing team ownership is a first-class governance violation**, not a warning
  to swallow. The sample data has unowned personal-key spend on purpose.
- Spend data is **stale by design** — every decision must carry `as_of` and a
  freshness/lag figure, and stale data must not be used to *lift* enforcement.
- **Never commit `.env`** — only `.env.example`. The submission repo is private.

## v1 scope guardrails (from task2-plan.md)

Not in this version: live LiteLLM admin API mutation, AWS calls, Datadog metric
emission, real provider keys, Postgres integration, Terraform / Lambda /
EventBridge deployment, Slack notifications, spend forecasting, per-request
pre-call cost blocking.

## Workflow

Follow `task2-plan.md` tickets in order. Small and complete beats large and half
done. The plan at `~/.claude/plans/you-are-helping-me-floating-dragonfly.md`
records recommended adjustments to Tickets 2+ (deterministic clock, model cost
table, tier-exercising budgets, governance surfacing, staleness behaviour).

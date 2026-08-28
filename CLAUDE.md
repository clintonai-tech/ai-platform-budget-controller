# CLAUDE.md

## What this is

Take-home for an AI Platform Engineer role at "Scalable Fictional". The platform
gives internal teams governed access to LLMs through a LiteLLM gateway.

- **Assignment**: `docs/AI Platform Engineer - Case Study 2026.pdf` (git-ignored)
- **Task 1 — design doc** (done): `task1-design.md`
- **Task 2 — build one component** (done): **Option B — tiered budget enforcement
  with graceful downgrade**. Build plan: `task2-plan.md` (8 tickets, all done).
- **Task 3 — architecture diagram** (done): `task3-architecture.drawio`
  (target-state; current solid, proposed dashed-green). README also has a
  mermaid quick-view.

The component (`budget-controller`) is a periodic controller: it reads accumulated
team spend, evaluates declarative budget policy against 75% / 90% / 100%
thresholds, and emits deterministic enforcement decisions (warn, urgent-warn,
downgrade, throttle, block, quarantine, require-explicit-signal) plus a schema
1.0 LiteLLM policy-intent JSON. The spend source is the brief's CSV; the exporter
is dry-run and there is no live gateway mutation.

Pipeline: `spend_loader` + `policy` → `evaluator.evaluate(policy, snapshot, now=)`
→ `exporters.build_export` → `cli` (`budget-controller evaluate`).

## Layout

| Path | Purpose |
|---|---|
| `src/budget_controller/` | The package. `cli.py` is the Typer entry point. |
| `tests/` | pytest suite. Small deterministic fixtures, not the full CSV. |
| `data/spend_30d.csv` | Input spend data (copy of the brief's file). |
| `config/budget_policy.yaml` | Declarative budget policy — all thresholds, budgets, per-team actions. |
| `docs/` | Reference only: assignment PDF, spend CSV, Langfuse sample. |

## Commands

Everything runs through `uv` (Python 3.13 project):

```bash
uv sync                                        # install / update .venv from uv.lock
uv run pytest                                  # 93 tests
uv run ruff check .                            # lint
uv run ruff format --check .                   # format check
uv run budget-controller evaluate              # run the CLI (table output)
uv run budget-controller evaluate --format json --as-of 2025-12-01
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
  Shared builders (`make_snapshot`, `one_team_policy`) are in `tests/_helpers.py`
  (no `test_` prefix, so pytest does not collect it).

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

Tickets 1–8 in `task2-plan.md` are complete. Small and complete beats large and
half done. Keep the README, this file, and the tests in sync when changing
behaviour. Every commit message: no "Claude Code" / co-author lines (user asked).

"""Shared test builders (not collected by pytest - no ``test_`` prefix)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from budget_controller.models import BudgetPolicy, GroupSpend, SpendSnapshot

AS_OF = date(2025, 12, 1)
FRESH_NOW = datetime(2025, 12, 2, 12, 0, tzinfo=UTC)  # 36h after AS_OF (< 48h)
STALE_NOW = datetime(2025, 12, 5, 12, 0, tzinfo=UTC)  # 108h after AS_OF (> 48h)


def make_snapshot(
    team_spend: dict[str, float],
    *,
    as_of: date | None = AS_OF,
    rows: tuple[Any, ...] = (),
) -> SpendSnapshot:
    by_team = {
        name: GroupSpend(
            label=name,
            spend_usd=amount,
            request_count=1,
            prompt_tokens=0,
            completion_tokens=0,
            row_count=1,
        )
        for name, amount in team_spend.items()
    }
    return SpendSnapshot(
        rows=rows,
        as_of=as_of,
        first_date=as_of,
        by_team=by_team,
        by_api_key={},
        by_model={},
        total_spend_usd=round(sum(team_spend.values()), 6),
    )


def one_team_policy(
    *,
    budget: float = 1000.0,
    at_100: str = "throttle",
    default_model: str = "std",
    downgrade_model: str | None = None,
    silent_downgrade: bool = False,
    throttle_to_pct: float = 50.0,
) -> BudgetPolicy:
    data: dict[str, Any] = {
        "version": 1,
        "defaults": {
            "thresholds": {"warn_at": 0.75, "urgent_warn_at": 0.90, "enforce_at": 1.00},
            "max_staleness_hours": 48,
            "throttle_to_pct": throttle_to_pct,
        },
        "models": {
            "prem": {"tier": "premium", "input_per_1m": 10.0, "output_per_1m": 30.0},
            "std": {"tier": "standard", "input_per_1m": 3.0, "output_per_1m": 15.0},
            "eco": {"tier": "economy", "input_per_1m": 0.5, "output_per_1m": 2.0},
        },
        "teams": {
            "T": {
                "monthly_budget_usd": budget,
                "default_model": default_model,
                "allowed_models": [default_model],
                "enforcement_mode": "enforce",
                "silent_downgrade_allowed": silent_downgrade,
                "at_100_percent": at_100,
                "downgrade_model": downgrade_model,
                "escalation": "#t",
            }
        },
        "unowned": {"action": "quarantine", "escalation": "#platform"},
    }
    return BudgetPolicy.model_validate(data)

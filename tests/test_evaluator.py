"""Tests for the policy evaluator (Ticket 5)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from budget_controller.evaluator import evaluate
from budget_controller.models import (
    UNOWNED_TEAM,
    Action,
    BudgetPolicy,
    BudgetStatus,
    DataQualityFlag,
    GroupSpend,
    SpendSnapshot,
)
from budget_controller.policy import load_policy
from budget_controller.spend_loader import load_spend

REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_POLICY = REPO_ROOT / "config" / "budget_policy.yaml"
REAL_SPEND = REPO_ROOT / "data" / "spend_30d.csv"

AS_OF = date(2025, 12, 1)
FRESH_NOW = datetime(2025, 12, 2, 12, 0, tzinfo=UTC)  # 36h after AS_OF


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
) -> BudgetPolicy:
    data: dict[str, Any] = {
        "version": 1,
        "defaults": {
            "thresholds": {"warn_at": 0.75, "urgent_warn_at": 0.90, "enforce_at": 1.00},
            "max_staleness_hours": 48,
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


def only(result_decisions: tuple[Any, ...], team: str = "T") -> Any:
    return next(d for d in result_decisions if d.team == team)


# --------------------------------------------------------------------------- #
# Threshold bands and exact boundaries
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("spend", "status", "action"),
    [
        (0.0, BudgetStatus.OK, Action.ALLOW),
        (749.99, BudgetStatus.OK, Action.ALLOW),
        (750.0, BudgetStatus.WARN, Action.WARN),  # exactly 75%
        (899.99, BudgetStatus.WARN, Action.WARN),
        (900.0, BudgetStatus.URGENT, Action.URGENT_WARN),  # exactly 90%
        (999.99, BudgetStatus.URGENT, Action.URGENT_WARN),
        (1000.0, BudgetStatus.OVER_BUDGET, Action.THROTTLE),  # exactly 100%
        (5000.0, BudgetStatus.OVER_BUDGET, Action.THROTTLE),
    ],
)
def test_threshold_bands(spend: float, status: BudgetStatus, action: Action) -> None:
    result = evaluate(one_team_policy(), make_snapshot({"T": spend}), now=FRESH_NOW)
    d = only(result.decisions)
    assert (d.status, d.action) == (status, action)
    assert d.percent_used == pytest.approx(spend / 10.0)  # budget is 1000


def test_percent_used_precision_keeps_sub_boundary_values() -> None:
    result = evaluate(one_team_policy(), make_snapshot({"T": 749.99}), now=FRESH_NOW)
    assert only(result.decisions).percent_used == pytest.approx(74.999)


def test_percent_used_is_percentage_not_fraction() -> None:
    result = evaluate(one_team_policy(), make_snapshot({"T": 920.0}), now=FRESH_NOW)
    assert only(result.decisions).percent_used == pytest.approx(92.0)


# --------------------------------------------------------------------------- #
# Enforcement action at 100%
# --------------------------------------------------------------------------- #


def test_at_100_percent_uses_configured_action() -> None:
    for action_name, expected in [
        ("throttle", Action.THROTTLE),
        ("block", Action.BLOCK),
        ("require_explicit_signal", Action.REQUIRE_EXPLICIT_SIGNAL),
    ]:
        policy = one_team_policy(at_100=action_name)
        result = evaluate(policy, make_snapshot({"T": 1200.0}), now=FRESH_NOW)
        assert only(result.decisions).action is expected


def test_downgrade_resolves_from_and_to_when_target_is_cheaper() -> None:
    policy = one_team_policy(
        at_100="downgrade", default_model="prem", downgrade_model="eco", silent_downgrade=True
    )
    d = only(evaluate(policy, make_snapshot({"T": 1500.0}), now=FRESH_NOW).decisions)
    assert d.action is Action.DOWNGRADE
    assert (d.downgrade_from, d.downgrade_to) == ("prem", "eco")


def test_downgrade_falls_back_to_throttle_when_already_economy() -> None:
    policy = one_team_policy(
        at_100="downgrade", default_model="eco", downgrade_model="eco", silent_downgrade=True
    )
    d = only(evaluate(policy, make_snapshot({"T": 1500.0}), now=FRESH_NOW).decisions)
    assert d.action is Action.THROTTLE
    assert "already on the economy tier" in d.reason


# --------------------------------------------------------------------------- #
# Unowned spend
# --------------------------------------------------------------------------- #


def test_unowned_spend_is_quarantined() -> None:
    snap = make_snapshot({"T": 10.0, UNOWNED_TEAM: 500.0})
    result = evaluate(one_team_policy(), snap, now=FRESH_NOW)
    d = only(result.decisions, UNOWNED_TEAM)
    assert d.status is BudgetStatus.QUARANTINED
    assert d.action is Action.QUARANTINE
    assert d.budget_usd is None
    assert d.percent_used is None
    assert d.is_owned is False
    assert DataQualityFlag.MISSING_TEAM in d.data_quality_flags


def test_no_unowned_decision_when_no_unowned_spend() -> None:
    result = evaluate(one_team_policy(), make_snapshot({"T": 10.0}), now=FRESH_NOW)
    assert all(d.team != UNOWNED_TEAM for d in result.decisions)


# --------------------------------------------------------------------------- #
# Staleness
# --------------------------------------------------------------------------- #


def test_fresh_snapshot_not_stale() -> None:
    now = datetime.combine(AS_OF, datetime.min.time(), tzinfo=UTC) + timedelta(hours=47)
    result = evaluate(one_team_policy(), make_snapshot({"T": 10.0}), now=now)
    assert result.stale is False
    assert result.hold_relaxations is False
    assert result.decisions[0].snapshot_stale is False


def test_stale_snapshot_sets_hold_relaxations_and_annotates() -> None:
    now = datetime.combine(AS_OF, datetime.min.time(), tzinfo=UTC) + timedelta(hours=49)
    result = evaluate(one_team_policy(), make_snapshot({"T": 10.0}), now=now)
    assert result.stale is True
    assert result.hold_relaxations is True
    d = result.decisions[0]
    assert d.snapshot_stale is True
    assert "hold relaxations" in d.reason


def test_stale_does_not_rewrite_the_action() -> None:
    now = datetime.combine(AS_OF, datetime.min.time(), tzinfo=UTC) + timedelta(days=30)
    fresh = evaluate(one_team_policy(), make_snapshot({"T": 800.0}), now=FRESH_NOW)
    stale = evaluate(one_team_policy(), make_snapshot({"T": 800.0}), now=now)
    assert only(fresh.decisions).action is only(stale.decisions).action is Action.WARN


def test_age_hours_never_negative_for_future_snapshot() -> None:
    past = datetime(2025, 11, 1, tzinfo=UTC)
    result = evaluate(one_team_policy(), make_snapshot({"T": 10.0}), now=past)
    assert result.snapshot_age_hours == 0.0


# --------------------------------------------------------------------------- #
# Empty snapshot
# --------------------------------------------------------------------------- #


def test_empty_snapshot_gives_allow_for_every_team() -> None:
    result = evaluate(load_policy(REAL_POLICY), make_snapshot({}, as_of=None), now=FRESH_NOW)
    assert {d.team for d in result.decisions} == {
        "AdvisorChat",
        "KYC",
        "DevAgent",
        "DigestBot",
        "Research",
        "Marketing",
    }
    assert all(d.action is Action.ALLOW for d in result.decisions)
    assert all(d.as_of is None for d in result.decisions)


# --------------------------------------------------------------------------- #
# Real policy + real spend
# --------------------------------------------------------------------------- #


def test_real_data_workload_specific_behaviour() -> None:
    policy = load_policy(REAL_POLICY)
    snap = load_spend(REAL_SPEND)
    result = evaluate(policy, snap, now=FRESH_NOW)
    by_team = {d.team: d for d in result.decisions}

    assert by_team["DevAgent"].status is BudgetStatus.OVER_BUDGET
    assert by_team["DevAgent"].action is Action.THROTTLE  # write-capable, never downgrade
    assert by_team["AdvisorChat"].action is Action.URGENT_WARN
    assert by_team["KYC"].action is Action.WARN
    assert by_team["DigestBot"].action is Action.WARN
    assert by_team["Research"].action is Action.ALLOW
    assert by_team["Marketing"].action is Action.ALLOW
    assert by_team[UNOWNED_TEAM].action is Action.QUARANTINE

    # over-budget / quarantined decisions sort to the front
    assert result.decisions[0].team in {UNOWNED_TEAM, "DevAgent"}


def test_advisorchat_and_kyc_never_downgrade_even_over_budget() -> None:
    policy = load_policy(REAL_POLICY)
    # force both far over budget
    snap = make_snapshot({"AdvisorChat": 99_999.0, "KYC": 99_999.0})
    result = evaluate(policy, snap, now=FRESH_NOW)
    by_team = {d.team: d for d in result.decisions}
    assert by_team["AdvisorChat"].action is Action.REQUIRE_EXPLICIT_SIGNAL
    assert by_team["KYC"].action is Action.BLOCK
    assert by_team["AdvisorChat"].action is not Action.DOWNGRADE
    assert by_team["KYC"].action is not Action.DOWNGRADE


def test_governance_violations_from_real_data() -> None:
    policy = load_policy(REAL_POLICY)
    snap = load_spend(REAL_SPEND)
    violations = evaluate(policy, snap, now=FRESH_NOW).governance_violations
    kinds = {v.kind for v in violations}
    assert DataQualityFlag.MISSING_TEAM in kinds
    assert DataQualityFlag.BLANK_COST in kinds
    assert DataQualityFlag.ZERO_REQUESTS_NONZERO_COST in kinds
    assert DataQualityFlag.OFF_CATALOGUE_MODEL in kinds

    off = {(v.team, v.model) for v in violations if v.kind is DataQualityFlag.OFF_CATALOGUE_MODEL}
    assert ("AdvisorChat", "gpt-4o") in off
    assert ("Research", "claude-opus-4-7") in off


def test_unknown_team_flagged(tmp_path: Path) -> None:
    csv = tmp_path / "spend.csv"
    csv.write_text(
        "date,api_key,team,model,request_count,prompt_tokens,completion_tokens,cost_usd\n"
        "2025-12-01,key-ghost,GhostTeam,claude-haiku-4-5,10,100,20,5.0\n",
        encoding="utf-8",
    )
    result = evaluate(load_policy(REAL_POLICY), load_spend(csv), now=FRESH_NOW)
    ghost = [v for v in result.governance_violations if v.kind is DataQualityFlag.UNKNOWN_TEAM]
    assert len(ghost) == 1
    assert ghost[0].team == "GhostTeam"
    # a team with no policy gets no decision
    assert all(d.team != "GhostTeam" for d in result.decisions)


def test_evaluation_is_deterministic() -> None:
    policy = load_policy(REAL_POLICY)
    snap = load_spend(REAL_SPEND)
    a = evaluate(policy, snap, now=FRESH_NOW)
    b = evaluate(policy, snap, now=FRESH_NOW)
    assert [d.model_dump() for d in a.decisions] == [d.model_dump() for d in b.decisions]
    assert a.governance_violations == b.governance_violations

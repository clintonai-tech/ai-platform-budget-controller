"""Turn a policy + a spend snapshot into deterministic per-team decisions.

The evaluator is pure: same ``(policy, snapshot, now)`` in, same
:class:`EvaluationResult` out. There is no wall clock inside - the caller passes
``now`` so runs are reproducible and testable.

Threshold bands (fraction of monthly budget):

* ``< warn_at``                -> ``allow``
* ``warn_at .. urgent_warn_at``  -> ``warn``
* ``urgent_warn_at .. enforce_at`` -> ``urgent_warn``
* ``>= enforce_at``            -> the team's ``at_100_percent`` action

Spend on a key with no team is not scored against a budget: it gets the
``unowned`` action (quarantine / block).

Staleness: spend data always lags. If the snapshot is older than
``defaults.max_staleness_hours`` the result sets ``hold_relaxations`` - a signal
to whatever applies these decisions that it may *raise* enforcement but must not
*lower* it from stale data. The evaluator never silently rewrites an action for
staleness; it only annotates.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time

from budget_controller.models import (
    TIER_RANK,
    UNOWNED_TEAM,
    Action,
    BudgetPolicy,
    BudgetStatus,
    DataQualityFlag,
    Decision,
    SpendSnapshot,
    TeamPolicy,
)


@dataclass(frozen=True)
class GovernanceViolation:
    """A governance / data-quality problem worth surfacing alongside decisions."""

    kind: DataQualityFlag
    detail: str
    team: str | None = None
    api_key: str | None = None
    model: str | None = None
    line_number: int | None = None
    spend_usd: float = 0.0


@dataclass(frozen=True)
class EvaluationResult:
    as_of: date | None
    evaluated_at: datetime
    snapshot_age_hours: float
    stale: bool
    hold_relaxations: bool
    max_staleness_hours: float
    total_spend_usd: float
    decisions: tuple[Decision, ...] = ()
    governance_violations: tuple[GovernanceViolation, ...] = field(default_factory=tuple)


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #


def evaluate(
    policy: BudgetPolicy,
    snapshot: SpendSnapshot,
    *,
    now: datetime,
) -> EvaluationResult:
    """Evaluate *snapshot* against *policy* as at *now*."""
    age_hours = _age_hours(snapshot.as_of, now)
    stale = age_hours > policy.defaults.max_staleness_hours

    decisions: list[Decision] = []
    for team_name, team_policy in policy.teams.items():
        group = snapshot.by_team.get(team_name)
        spend = group.spend_usd if group is not None else 0.0
        flags = list(group.flags) if group is not None else []
        decisions.append(
            _decide_team(
                team_name=team_name,
                team_policy=team_policy,
                policy=policy,
                spend=spend,
                data_quality_flags=flags,
                as_of=snapshot.as_of,
                age_hours=age_hours,
                stale=stale,
            )
        )

    if snapshot.has_unowned_spend:
        decisions.append(
            _decide_unowned(
                policy=policy,
                spend=snapshot.by_team[UNOWNED_TEAM].spend_usd,
                as_of=snapshot.as_of,
                age_hours=age_hours,
                stale=stale,
            )
        )

    decisions.sort(key=_decision_sort_key)

    return EvaluationResult(
        as_of=snapshot.as_of,
        evaluated_at=now,
        snapshot_age_hours=round(age_hours, 2),
        stale=stale,
        hold_relaxations=stale,
        max_staleness_hours=policy.defaults.max_staleness_hours,
        total_spend_usd=snapshot.total_spend_usd,
        decisions=tuple(decisions),
        governance_violations=_governance_violations(policy, snapshot),
    )


# --------------------------------------------------------------------------- #
# Per-team decision
# --------------------------------------------------------------------------- #


def _decide_team(
    *,
    team_name: str,
    team_policy: TeamPolicy,
    policy: BudgetPolicy,
    spend: float,
    data_quality_flags: list[DataQualityFlag],
    as_of: date | None,
    age_hours: float,
    stale: bool,
) -> Decision:
    budget = team_policy.monthly_budget_usd
    fraction = spend / budget
    percent = fraction * 100.0
    thresholds = policy.defaults.thresholds

    downgrade_from: str | None = None
    downgrade_to: str | None = None

    if fraction >= thresholds.enforce_at:
        status = BudgetStatus.OVER_BUDGET
        action = team_policy.at_100_percent
        if action is Action.DOWNGRADE:
            action, downgrade_from, downgrade_to, note = _resolve_downgrade(team_policy, policy)
            reason = (
                f"{percent:.1f}% of ${budget:,.0f} budget: {note}"
                if note
                else f"{percent:.1f}% of ${budget:,.0f} budget: downgrade "
                f"{downgrade_from} -> {downgrade_to}"
            )
        else:
            reason = f"{percent:.1f}% of ${budget:,.0f} budget: {action.value}"
    elif fraction >= thresholds.urgent_warn_at:
        status, action = BudgetStatus.URGENT, Action.URGENT_WARN
        reason = f"{percent:.1f}% of ${budget:,.0f} budget: notify team and platform"
    elif fraction >= thresholds.warn_at:
        status, action = BudgetStatus.WARN, Action.WARN
        reason = f"{percent:.1f}% of ${budget:,.0f} budget: notify owning team"
    else:
        status, action = BudgetStatus.OK, Action.ALLOW
        reason = f"{percent:.1f}% of ${budget:,.0f} budget"

    if stale:
        reason += f" [snapshot {age_hours:.0f}h old - hold relaxations]"

    return Decision(
        team=team_name,
        is_owned=True,
        budget_usd=budget,
        spend_usd=round(spend, 2),
        percent_used=round(percent, 4),
        status=status,
        action=action,
        reason=reason,
        enforcement_mode=team_policy.enforcement_mode,
        downgrade_from=downgrade_from,
        downgrade_to=downgrade_to,
        as_of=as_of,
        snapshot_age_hours=round(age_hours, 2),
        snapshot_stale=stale,
        data_quality_flags=data_quality_flags,
    )


def _resolve_downgrade(
    team_policy: TeamPolicy, policy: BudgetPolicy
) -> tuple[Action, str, str, str | None]:
    """Return ``(action, from_model, to_model, note)``.

    If the target is not actually a cheaper tier than the team's default model
    (e.g. a team already on the economy tier), the downgrade cannot help and
    the action falls back to ``throttle``.
    """
    from_model = team_policy.default_model
    to_model = team_policy.downgrade_model or from_model

    from_rank = _tier_rank(from_model, policy)
    to_rank = _tier_rank(to_model, policy)

    if to_rank is None or from_rank is None or to_rank >= from_rank:
        return (
            Action.THROTTLE,
            from_model,
            to_model,
            f"already on the {policy.models[from_model].tier.value} tier, "
            "downgrade cannot help - throttle instead",
        )
    return Action.DOWNGRADE, from_model, to_model, None


def _tier_rank(model: str, policy: BudgetPolicy) -> int | None:
    price = policy.models.get(model)
    return None if price is None else TIER_RANK[price.tier]


def _decide_unowned(
    *,
    policy: BudgetPolicy,
    spend: float,
    as_of: date | None,
    age_hours: float,
    stale: bool,
) -> Decision:
    reason = (
        f"${spend:,.2f} on keys with no team - {policy.unowned.action.value}; "
        f"escalate {policy.unowned.escalation}"
    )
    if stale:
        reason += f" [snapshot {age_hours:.0f}h old]"
    return Decision(
        team=UNOWNED_TEAM,
        is_owned=False,
        budget_usd=None,
        spend_usd=round(spend, 2),
        percent_used=None,
        status=BudgetStatus.QUARANTINED,
        action=policy.unowned.action,
        reason=reason,
        enforcement_mode=None,
        as_of=as_of,
        snapshot_age_hours=round(age_hours, 2),
        snapshot_stale=stale,
        data_quality_flags=[DataQualityFlag.MISSING_TEAM],
    )


# --------------------------------------------------------------------------- #
# Governance
# --------------------------------------------------------------------------- #


def _governance_violations(
    policy: BudgetPolicy, snapshot: SpendSnapshot
) -> tuple[GovernanceViolation, ...]:
    out: list[GovernanceViolation] = []

    # Row-level data quality problems, one entry per flagged row.
    for row in snapshot.flagged_rows:
        for flag in row.flags:
            out.append(
                GovernanceViolation(
                    kind=flag,
                    detail=_row_detail(flag),
                    team=row.team,
                    api_key=row.api_key or None,
                    model=row.model or None,
                    line_number=row.line_number,
                    spend_usd=round(row.cost_usd, 2),
                )
            )

    # Teams that appear in spend but have no policy entry.
    for team_name, group in snapshot.by_team.items():
        if team_name == UNOWNED_TEAM or team_name in policy.teams:
            continue
        out.append(
            GovernanceViolation(
                kind=DataQualityFlag.UNKNOWN_TEAM,
                detail="team has spend but no entry in budget_policy.yaml",
                team=team_name,
                spend_usd=round(group.spend_usd, 2),
            )
        )

    # Owned rows using a model outside the team's allow-list.
    off_catalogue: dict[tuple[str, str], float] = {}
    for row in snapshot.rows:
        if row.team is None or row.team not in policy.teams:
            continue
        if row.model and row.model not in policy.teams[row.team].allowed_models:
            off_catalogue[(row.team, row.model)] = (
                off_catalogue.get((row.team, row.model), 0.0) + row.cost_usd
            )
    for (team_name, model), spend in sorted(off_catalogue.items()):
        allowed = ", ".join(policy.teams[team_name].allowed_models)
        out.append(
            GovernanceViolation(
                kind=DataQualityFlag.OFF_CATALOGUE_MODEL,
                detail=f"used '{model}'; allowed: [{allowed}]",
                team=team_name,
                model=model,
                spend_usd=round(spend, 2),
            )
        )

    return tuple(out)


def _row_detail(flag: DataQualityFlag) -> str:
    return {
        DataQualityFlag.MISSING_TEAM: "row has no team - ownership is required",
        DataQualityFlag.BLANK_COST: "cost_usd is blank - counted as $0, real cost unknown",
        DataQualityFlag.NEGATIVE_COST: "cost_usd is negative",
        DataQualityFlag.ZERO_REQUESTS_NONZERO_COST: "0 requests but non-zero cost",
        DataQualityFlag.UNPARSEABLE_ROW: "row is malformed (wrong field count or bad values)",
    }.get(flag, flag.value)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _age_hours(as_of: date | None, now: datetime) -> float:
    if as_of is None:
        return 0.0
    as_of_dt = datetime.combine(as_of, time.min)
    if now.tzinfo is not None:
        as_of_dt = as_of_dt.replace(tzinfo=now.tzinfo)
    return max(0.0, (now - as_of_dt).total_seconds() / 3600.0)


_STATUS_ORDER = {
    BudgetStatus.QUARANTINED: 0,
    BudgetStatus.OVER_BUDGET: 1,
    BudgetStatus.URGENT: 2,
    BudgetStatus.WARN: 3,
    BudgetStatus.OK: 4,
}


def _decision_sort_key(d: Decision) -> tuple[int, float, str]:
    return (_STATUS_ORDER[d.status], -(d.percent_used or 0.0), d.team)

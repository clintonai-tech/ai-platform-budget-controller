"""Render an :class:`EvaluationResult` as LiteLLM-flavoured policy intent.

Version 1 is **dry-run only**: this module builds a JSON document describing the
changes a worker *would* push to the LiteLLM admin API. It never calls LiteLLM.
The stable shape is::

    {
      "schema_version": "1.0",
      "generated_by": "budget-controller",
      "dry_run": true,
      "evaluated_at": "<iso8601>",
      "as_of": "<date|null>",
      "snapshot_age_hours": <float>,
      "stale": <bool>,
      "hold_relaxations": <bool>,
      "summary": {"teams_evaluated", "actionable", "held", "by_action"},
      "intents": [ <intent>, ... ],
      "governance_violations": [ <violation>, ... ]
    }

Each ``intent`` carries the decision context plus:

* ``changes``      - list of admin-API-shaped operations (target / id / operation / params)
* ``notifications``- list of {channel, severity, message}
* ``app_signal``   - object the application layer should receive, or null
* ``projected_monthly_saving_usd`` - rough estimate for downgrades, or null
* ``recovery``     - how enforcement is cleared, or null
* ``relaxing`` / ``held`` / ``held_reason`` - a relaxing intent is suppressed
  (``apply`` stays false, ``held`` true) whenever ``hold_relaxations`` is set,
  so stale data can raise enforcement but never lower it.

Action -> changes mapping:

===================== =================================================
action                gateway effect
===================== =================================================
allow                 clear any stale downgrade/throttle (relaxing)
warn / urgent_warn    notification(s) only, no gateway change
throttle              scale team RPM/TPM/parallel limits
downgrade             re-route the team's default alias + app_signal
require_explicit_signal keep serving, app_signal only
block                 block the team, manual reset
quarantine            block each un-attributed key, manual reset
===================== =================================================
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

from budget_controller.evaluator import EvaluationResult, GovernanceViolation
from budget_controller.models import (
    UNOWNED_TEAM,
    Action,
    BudgetPolicy,
    DataQualityFlag,
    Decision,
)

SCHEMA_VERSION = "1.0"


def build_export(result: EvaluationResult, policy: BudgetPolicy) -> dict[str, Any]:
    """Build the policy-intent document (plain JSON-serialisable dict)."""
    unowned_keys = sorted(
        {
            v.api_key
            for v in result.governance_violations
            if v.kind is DataQualityFlag.MISSING_TEAM and v.api_key
        }
    )
    intents = [_intent(d, result, policy, unowned_keys) for d in result.decisions]
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_by": "budget-controller",
        "dry_run": True,
        "evaluated_at": result.evaluated_at.isoformat(),
        "as_of": result.as_of.isoformat() if result.as_of else None,
        "snapshot_age_hours": result.snapshot_age_hours,
        "stale": result.stale,
        "hold_relaxations": result.hold_relaxations,
        "summary": {
            "teams_evaluated": sum(1 for d in result.decisions if d.is_owned),
            "actionable": sum(1 for i in intents if _is_actionable(i)),
            "held": sum(1 for i in intents if i.get("held")),
            "by_action": dict(Counter(d.action.value for d in result.decisions)),
        },
        "intents": intents,
        "governance_violations": [_violation(v) for v in result.governance_violations],
    }


def export_json(result: EvaluationResult, policy: BudgetPolicy, *, indent: int | None = 2) -> str:
    """Serialise :func:`build_export` to a JSON string."""
    return json.dumps(build_export(result, policy), indent=indent)


# --------------------------------------------------------------------------- #
# Per-decision intent
# --------------------------------------------------------------------------- #


def _intent(
    d: Decision,
    result: EvaluationResult,
    policy: BudgetPolicy,
    unowned_keys: list[str],
) -> dict[str, Any]:
    intent: dict[str, Any] = {
        "team": d.team,
        "action": d.action.value,
        "status": d.status.value,
        "spend_usd": d.spend_usd,
        "budget_usd": d.budget_usd,
        "percent_used": d.percent_used,
        "enforcement_mode": d.enforcement_mode.value if d.enforcement_mode else None,
        "reason": d.reason,
        "apply": False,
        "relaxing": False,
        "changes": [],
        "notifications": [],
        "app_signal": None,
        "projected_monthly_saving_usd": None,
        "recovery": None,
        "data_quality_flags": [f.value for f in d.data_quality_flags],
    }
    escalation = _escalation(d.team, policy)
    platform = policy.unowned.escalation

    if d.action is Action.ALLOW:
        intent["relaxing"] = True
        if result.hold_relaxations:
            intent["held"] = True
            intent["held_reason"] = (
                f"snapshot {result.snapshot_age_hours:.0f}h old "
                f"(> {result.max_staleness_hours:.0f}h); not clearing enforcement from stale data"
            )
        else:
            intent["changes"].append(
                _change(
                    "team",
                    d.team,
                    "clear_overrides",
                    {},
                    "within budget; ensure no stale downgrade/throttle remains",
                )
            )

    elif d.action is Action.WARN:
        intent["notifications"].append(
            _note(escalation, "warning", f"{d.team} at {d.percent_used:.1f}% of budget")
        )

    elif d.action is Action.URGENT_WARN:
        msg = f"{d.team} at {d.percent_used:.1f}% of budget - approaching enforcement"
        intent["notifications"].append(_note(escalation, "urgent", msg))
        intent["notifications"].append(_note(platform, "urgent", msg))

    elif d.action is Action.THROTTLE:
        factor = round(policy.defaults.throttle_to_pct / 100.0, 4)
        intent["changes"].append(
            _change(
                "team",
                d.team,
                "update",
                {
                    "scale": {
                        "rpm_limit": factor,
                        "tpm_limit": factor,
                        "max_parallel_requests": factor,
                    }
                },
                f"reduce throughput to {policy.defaults.throttle_to_pct:.0f}% of current limits",
            )
        )
        intent["notifications"].append(
            _note(escalation, "urgent", f"{d.team} over budget - throttled")
        )
        intent["recovery"] = "automatic when the spend window resets"

    elif d.action is Action.DOWNGRADE:
        default_alias = policy.teams[d.team].default_model
        intent["changes"].append(
            _change(
                "team_model_map",
                d.team,
                "reroute",
                {
                    "alias": default_alias,
                    "from_model": d.downgrade_from,
                    "to_model": d.downgrade_to,
                },
                "route this team's default alias to the cheaper model until spend resets",
            )
        )
        intent["app_signal"] = {
            "budget_status": "over_budget",
            "model_downgraded": True,
            "from": d.downgrade_from,
            "to": d.downgrade_to,
            "message": f"Budget exceeded; responses now use {d.downgrade_to}.",
        }
        intent["projected_monthly_saving_usd"] = _saving(
            d.spend_usd, d.downgrade_from, d.downgrade_to, policy
        )
        intent["notifications"].append(
            _note(escalation, "urgent", f"{d.team} over budget - downgraded to {d.downgrade_to}")
        )
        intent["recovery"] = "automatic when the spend window resets; or manual policy reset"

    elif d.action is Action.REQUIRE_EXPLICIT_SIGNAL:
        intent["app_signal"] = {
            "budget_status": "over_budget",
            "model_downgraded": False,
            "message": (
                "Budget exceeded. No automatic downgrade for this workload; "
                "switch to an approved-equivalent model or reduce usage."
            ),
        }
        intent["notifications"].append(
            _note(escalation, "urgent", f"{d.team} over budget - app signalled, no downgrade")
        )

    elif d.action is Action.BLOCK:
        intent["changes"].append(
            _change(
                "team",
                d.team,
                "update",
                {"blocked": True},
                "reject further requests until an explicit reset",
            )
        )
        intent["notifications"].append(
            _note(escalation, "critical", f"{d.team} over budget - blocked")
        )
        intent["recovery"] = "manual: platform re-enables the team after review"

    elif d.action is Action.QUARANTINE:
        intent["changes"] = [
            _change(
                "virtual_key",
                key,
                "update",
                {"blocked": True, "quarantine": True},
                "spend on a key with no team",
            )
            for key in unowned_keys
        ]
        keys_str = ", ".join(unowned_keys) or "unknown"
        intent["notifications"].append(
            _note(
                platform,
                "critical",
                f"${d.spend_usd:,.2f} un-attributed spend on: {keys_str}",
            )
        )
        intent["recovery"] = "manual: assign each key to a team, then reset"

    return intent


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _change(
    target: str, ident: str, operation: str, params: dict[str, Any], note: str
) -> dict[str, Any]:
    return {"target": target, "id": ident, "operation": operation, "params": params, "note": note}


def _note(channel: str | None, severity: str, message: str) -> dict[str, Any]:
    return {"channel": channel, "severity": severity, "message": message}


def _violation(v: GovernanceViolation) -> dict[str, Any]:
    return {
        "kind": v.kind.value,
        "detail": v.detail,
        "team": v.team,
        "api_key": v.api_key,
        "model": v.model,
        "line_number": v.line_number,
        "spend_usd": v.spend_usd,
    }


def _escalation(team: str, policy: BudgetPolicy) -> str | None:
    if team == UNOWNED_TEAM:
        return policy.unowned.escalation
    team_policy = policy.teams.get(team)
    return team_policy.escalation if team_policy else None


def _is_actionable(intent: dict[str, Any]) -> bool:
    return bool(intent["changes"] or intent["notifications"] or intent["app_signal"])


def _blended_price(model: str | None, policy: BudgetPolicy) -> float | None:
    price = policy.models.get(model) if model else None
    if price is None:
        return None
    return (price.input_per_1m + price.output_per_1m) / 2.0


def _saving(
    spend_usd: float, from_model: str | None, to_model: str | None, policy: BudgetPolicy
) -> float | None:
    """Rough: this period's spend re-priced at the cheaper model's blended rate."""
    from_price = _blended_price(from_model, policy)
    to_price = _blended_price(to_model, policy)
    if not from_price or to_price is None:
        return None
    ratio = max(0.0, 1.0 - to_price / from_price)
    return round(spend_usd * ratio, 2)

"""Tests for policy domain models and loading (Ticket 3)."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from budget_controller.models import Action, BudgetPolicy, TeamPolicy
from budget_controller.policy import PolicyError, load_policy

REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_POLICY = REPO_ROOT / "config" / "budget_policy.yaml"


def minimal_policy() -> dict[str, Any]:
    """Smallest dict that validates as a BudgetPolicy."""
    return {
        "version": 1,
        "defaults": {
            "thresholds": {"warn_at": 0.75, "urgent_warn_at": 0.90, "enforce_at": 1.00},
            "max_staleness_hours": 48,
        },
        "models": {
            "big": {"tier": "premium", "input_per_1m": 10.0, "output_per_1m": 30.0},
            "small": {"tier": "economy", "input_per_1m": 0.5, "output_per_1m": 2.0},
        },
        "teams": {
            "Alpha": {
                "monthly_budget_usd": 1000,
                "default_model": "big",
                "allowed_models": ["big"],
                "enforcement_mode": "enforce",
                "silent_downgrade_allowed": True,
                "at_100_percent": "downgrade",
                "downgrade_model": "small",
                "escalation": "#alpha",
            }
        },
        "unowned": {"action": "quarantine", "escalation": "#platform"},
    }


def write_policy(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "policy.yaml"
    path.write_text(text, encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# The real committed policy file
# --------------------------------------------------------------------------- #


def test_real_policy_file_loads() -> None:
    policy = load_policy(REAL_POLICY)
    assert set(policy.teams) == {
        "AdvisorChat",
        "KYC",
        "DevAgent",
        "DigestBot",
        "Research",
        "Marketing",
    }
    assert policy.defaults.thresholds.warn_at == 0.75
    assert policy.unowned.action is Action.QUARANTINE
    # every downgrade team names a target that exists in the price table
    for team in policy.teams.values():
        if team.at_100_percent is Action.DOWNGRADE:
            assert team.downgrade_model in policy.models


# --------------------------------------------------------------------------- #
# Valid construction
# --------------------------------------------------------------------------- #


def test_minimal_policy_validates() -> None:
    policy = BudgetPolicy.model_validate(minimal_policy())
    assert policy.teams["Alpha"].at_100_percent is Action.DOWNGRADE


# --------------------------------------------------------------------------- #
# Invalid config fails clearly
# --------------------------------------------------------------------------- #


def test_thresholds_out_of_order_rejected() -> None:
    data = minimal_policy()
    data["defaults"]["thresholds"] = {
        "warn_at": 0.90,
        "urgent_warn_at": 0.75,
        "enforce_at": 1.00,
    }
    with pytest.raises(ValueError, match="warn_at < urgent_warn_at"):
        BudgetPolicy.model_validate(data)


def test_threshold_above_one_rejected() -> None:
    data = minimal_policy()
    data["defaults"]["thresholds"]["warn_at"] = 1.5
    with pytest.raises(ValueError):
        BudgetPolicy.model_validate(data)


def test_unknown_action_rejected() -> None:
    data = minimal_policy()
    data["teams"]["Alpha"]["at_100_percent"] = "explode"
    with pytest.raises(ValueError, match="explode"):
        BudgetPolicy.model_validate(data)


def test_non_enforcement_action_rejected() -> None:
    data = minimal_policy()
    data["teams"]["Alpha"]["at_100_percent"] = "warn"
    data["teams"]["Alpha"]["silent_downgrade_allowed"] = False
    data["teams"]["Alpha"]["downgrade_model"] = None
    with pytest.raises(ValueError, match="at_100_percent must be one of"):
        BudgetPolicy.model_validate(data)


def test_missing_budget_rejected() -> None:
    data = minimal_policy()
    del data["teams"]["Alpha"]["monthly_budget_usd"]
    with pytest.raises(ValueError, match="monthly_budget_usd"):
        BudgetPolicy.model_validate(data)


def test_negative_budget_rejected() -> None:
    data = minimal_policy()
    data["teams"]["Alpha"]["monthly_budget_usd"] = -10
    with pytest.raises(ValueError):
        BudgetPolicy.model_validate(data)


def test_downgrade_without_target_rejected() -> None:
    data = minimal_policy()
    data["teams"]["Alpha"]["downgrade_model"] = None
    with pytest.raises(ValueError, match="requires a downgrade_model"):
        BudgetPolicy.model_validate(data)


def test_unknown_model_reference_rejected() -> None:
    data = minimal_policy()
    data["teams"]["Alpha"]["allowed_models"] = ["nope"]
    data["teams"]["Alpha"]["default_model"] = "nope"
    with pytest.raises(ValueError, match="not in models"):
        BudgetPolicy.model_validate(data)


def test_default_model_not_in_allowed_rejected() -> None:
    data = minimal_policy()
    data["teams"]["Alpha"]["default_model"] = "small"  # allowed_models is ["big"]
    with pytest.raises(ValueError, match="not in its allowed_models"):
        BudgetPolicy.model_validate(data)


def test_unknown_field_rejected() -> None:
    data = minimal_policy()
    data["teams"]["Alpha"]["budget"] = 999  # typo for monthly_budget_usd
    with pytest.raises(ValueError, match="budget"):
        BudgetPolicy.model_validate(data)


def test_unowned_action_must_be_quarantine_or_block() -> None:
    data = minimal_policy()
    data["unowned"]["action"] = "downgrade"
    with pytest.raises(ValueError, match="quarantine.*block"):
        BudgetPolicy.model_validate(data)


# --------------------------------------------------------------------------- #
# load_policy: file-level failures
# --------------------------------------------------------------------------- #


def test_missing_file_raises_policy_error() -> None:
    with pytest.raises(PolicyError, match="cannot read"):
        load_policy("/no/such/policy.yaml")


def test_non_mapping_top_level_raises_policy_error(tmp_path: Path) -> None:
    path = write_policy(tmp_path, "- just\n- a\n- list\n")
    with pytest.raises(PolicyError, match="mapping at the top level"):
        load_policy(path)


def test_broken_yaml_raises_policy_error(tmp_path: Path) -> None:
    path = write_policy(tmp_path, "version: 1\n  bad: : indent\n")
    with pytest.raises(PolicyError, match="not valid YAML"):
        load_policy(path)


DUP_TEAM_YAML = """\
version: 1
defaults:
  thresholds: {warn_at: 0.75, urgent_warn_at: 0.90, enforce_at: 1.00}
  max_staleness_hours: 48
models:
  big: {tier: premium, input_per_1m: 10.0, output_per_1m: 30.0}
teams:
  Alpha:
    monthly_budget_usd: 1000
    default_model: big
    allowed_models: [big]
    enforcement_mode: enforce
    silent_downgrade_allowed: false
    at_100_percent: block
    escalation: "#alpha"
  Alpha:
    monthly_budget_usd: 5
    default_model: big
    allowed_models: [big]
    enforcement_mode: enforce
    silent_downgrade_allowed: false
    at_100_percent: block
    escalation: "#alpha2"
unowned: {action: quarantine, escalation: "#platform"}
"""


def test_duplicate_team_key_raises_policy_error(tmp_path: Path) -> None:
    path = write_policy(tmp_path, DUP_TEAM_YAML)
    with pytest.raises(PolicyError, match="duplicate key 'Alpha'"):
        load_policy(path)


def test_validation_failure_raises_policy_error(tmp_path: Path) -> None:
    import yaml

    data = minimal_policy()
    data["teams"]["Alpha"]["monthly_budget_usd"] = -1
    path = write_policy(tmp_path, yaml.safe_dump(data))
    with pytest.raises(PolicyError, match="failed validation"):
        load_policy(path)


# --------------------------------------------------------------------------- #
# TeamPolicy defaults
# --------------------------------------------------------------------------- #


def test_team_policy_notes_default_empty() -> None:
    tp = TeamPolicy.model_validate(
        {
            "monthly_budget_usd": 100,
            "default_model": "m",
            "allowed_models": ["m"],
            "enforcement_mode": "monitor",
            "silent_downgrade_allowed": False,
            "at_100_percent": "block",
            "escalation": "#x",
        }
    )
    assert tp.notes == ""
    assert tp.downgrade_model is None


def test_deepcopy_helper_is_isolated() -> None:
    # guard: minimal_policy() must return a fresh structure each call
    a = minimal_policy()
    b = minimal_policy()
    a["teams"]["Alpha"]["monthly_budget_usd"] = 1
    assert b["teams"]["Alpha"]["monthly_budget_usd"] == 1000
    assert copy.deepcopy(a) is not a

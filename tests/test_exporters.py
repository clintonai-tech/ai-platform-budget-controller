"""Tests for the LiteLLM policy-intent exporter (Ticket 6)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from _helpers import AS_OF, FRESH_NOW, STALE_NOW, make_snapshot, one_team_policy

from budget_controller.evaluator import evaluate
from budget_controller.exporters import SCHEMA_VERSION, build_export, export_json
from budget_controller.policy import load_policy
from budget_controller.spend_loader import load_spend

REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_POLICY = REPO_ROOT / "config" / "budget_policy.yaml"
REAL_SPEND = REPO_ROOT / "data" / "spend_30d.csv"


def export_for(policy, spend, *, now=FRESH_NOW):
    return build_export(evaluate(policy, spend, now=now), policy)


def intent_for(doc: dict, team: str) -> dict:
    return next(i for i in doc["intents"] if i["team"] == team)


# --------------------------------------------------------------------------- #
# Document shape
# --------------------------------------------------------------------------- #


def test_document_shape() -> None:
    doc = export_for(one_team_policy(), make_snapshot({"T": 100.0}))
    assert doc["schema_version"] == SCHEMA_VERSION
    assert doc["generated_by"] == "budget-controller"
    assert doc["dry_run"] is True
    assert set(doc) == {
        "schema_version",
        "generated_by",
        "dry_run",
        "evaluated_at",
        "as_of",
        "snapshot_age_hours",
        "stale",
        "hold_relaxations",
        "summary",
        "intents",
        "governance_violations",
    }
    assert doc["as_of"] == AS_OF.isoformat()
    assert all(i["apply"] is False for i in doc["intents"])


def test_no_live_calls_ever_apply() -> None:
    doc = export_for(load_policy(REAL_POLICY), load_spend(REAL_SPEND))
    assert doc["dry_run"] is True
    assert all(i["apply"] is False for i in doc["intents"])


# --------------------------------------------------------------------------- #
# Per-action change shapes
# --------------------------------------------------------------------------- #


def test_throttle_intent_scales_limits() -> None:
    doc = export_for(
        one_team_policy(at_100="throttle", throttle_to_pct=40), make_snapshot({"T": 1200.0})
    )
    intent = intent_for(doc, "T")
    assert intent["action"] == "throttle"
    (change,) = intent["changes"]
    assert change["target"] == "team"
    assert change["operation"] == "update"
    assert change["params"]["scale"] == {
        "rpm_limit": 0.4,
        "tpm_limit": 0.4,
        "max_parallel_requests": 0.4,
    }
    assert intent["recovery"]
    assert intent["notifications"]


def test_downgrade_intent_reroutes_and_signals_app() -> None:
    policy = one_team_policy(
        at_100="downgrade", default_model="prem", downgrade_model="eco", silent_downgrade=True
    )
    intent = intent_for(export_for(policy, make_snapshot({"T": 1500.0})), "T")
    assert intent["action"] == "downgrade"
    (change,) = intent["changes"]
    assert change["target"] == "team_model_map"
    assert change["operation"] == "reroute"
    assert change["params"]["from_model"] == "prem"
    assert change["params"]["to_model"] == "eco"
    assert intent["app_signal"]["model_downgraded"] is True
    assert intent["app_signal"]["to"] == "eco"
    assert intent["projected_monthly_saving_usd"] > 0


def test_downgrade_projected_saving_math() -> None:
    # prem blended = (10+30)/2 = 20 ; eco blended = (0.5+2)/2 = 1.25
    # ratio = 1 - 1.25/20 = 0.9375 ; spend 1000 -> saving 937.5
    policy = one_team_policy(
        at_100="downgrade", default_model="prem", downgrade_model="eco", silent_downgrade=True
    )
    intent = intent_for(export_for(policy, make_snapshot({"T": 1000.0})), "T")
    assert intent["projected_monthly_saving_usd"] == pytest.approx(937.5)


def test_downgrade_that_falls_back_to_throttle_has_no_app_signal() -> None:
    policy = one_team_policy(
        at_100="downgrade", default_model="eco", downgrade_model="eco", silent_downgrade=True
    )
    intent = intent_for(export_for(policy, make_snapshot({"T": 1500.0})), "T")
    assert intent["action"] == "throttle"
    assert intent["app_signal"] is None
    assert "scale" in intent["changes"][0]["params"]


def test_block_intent_blocks_and_requires_manual_reset() -> None:
    intent = intent_for(
        export_for(one_team_policy(at_100="block"), make_snapshot({"T": 1200.0})), "T"
    )
    assert intent["changes"][0]["params"] == {"blocked": True}
    assert "manual" in intent["recovery"]
    assert intent["notifications"][0]["severity"] == "critical"


def test_require_explicit_signal_keeps_serving() -> None:
    intent = intent_for(
        export_for(one_team_policy(at_100="require_explicit_signal"), make_snapshot({"T": 1200.0})),
        "T",
    )
    assert intent["changes"] == []
    assert intent["app_signal"]["model_downgraded"] is False
    assert "budget" in intent["app_signal"]["message"].lower()


def test_warn_and_urgent_warn_are_notifications_only() -> None:
    warn = intent_for(export_for(one_team_policy(), make_snapshot({"T": 800.0})), "T")
    assert warn["action"] == "warn"
    assert warn["changes"] == []
    assert len(warn["notifications"]) == 1

    urgent = intent_for(export_for(one_team_policy(), make_snapshot({"T": 950.0})), "T")
    assert urgent["action"] == "urgent_warn"
    assert urgent["changes"] == []
    assert {n["channel"] for n in urgent["notifications"]} == {"#t", "#platform"}


# --------------------------------------------------------------------------- #
# Staleness gates relaxing intents
# --------------------------------------------------------------------------- #


def test_allow_clears_overrides_when_fresh() -> None:
    intent = intent_for(export_for(one_team_policy(), make_snapshot({"T": 10.0})), "T")
    assert intent["relaxing"] is True
    assert intent.get("held") is None
    assert intent["changes"][0]["operation"] == "clear_overrides"


def test_allow_is_held_when_stale() -> None:
    doc = export_for(one_team_policy(), make_snapshot({"T": 10.0}), now=STALE_NOW)
    assert doc["stale"] is True
    assert doc["hold_relaxations"] is True
    intent = intent_for(doc, "T")
    assert intent["held"] is True
    assert "not clearing enforcement" in intent["held_reason"]
    assert intent["changes"] == []
    assert doc["summary"]["held"] == 1


def test_enforcement_still_emitted_when_stale() -> None:
    # stale data must still be able to RAISE enforcement
    intent = intent_for(
        export_for(one_team_policy(at_100="block"), make_snapshot({"T": 5000.0}), now=STALE_NOW),
        "T",
    )
    assert intent["action"] == "block"
    assert intent["changes"][0]["params"] == {"blocked": True}


# --------------------------------------------------------------------------- #
# Quarantine + governance pass-through
# --------------------------------------------------------------------------- #


def test_quarantine_lists_each_unowned_key(tmp_path: Path) -> None:
    csv = tmp_path / "spend.csv"
    csv.write_text(
        "date,api_key,team,model,request_count,prompt_tokens,completion_tokens,cost_usd\n"
        "2025-11-30,key-ghost-a,,gpt-5.4,10,100,20,50.0\n"
        "2025-11-30,key-ghost-b,,gpt-5.4,10,100,20,25.0\n",
        encoding="utf-8",
    )
    doc = export_for(load_policy(REAL_POLICY), load_spend(csv))
    intent = intent_for(doc, "__unowned__")
    assert intent["action"] == "quarantine"
    assert {c["id"] for c in intent["changes"]} == {"key-ghost-a", "key-ghost-b"}
    assert all(c["params"] == {"blocked": True, "quarantine": True} for c in intent["changes"])
    assert "manual" in intent["recovery"]


def test_governance_violations_passed_through() -> None:
    doc = export_for(load_policy(REAL_POLICY), load_spend(REAL_SPEND))
    kinds = {v["kind"] for v in doc["governance_violations"]}
    assert {
        "missing_team",
        "blank_cost",
        "zero_requests_nonzero_cost",
        "off_catalogue_model",
    } <= kinds
    off = {
        (v["team"], v["model"])
        for v in doc["governance_violations"]
        if v["kind"] == "off_catalogue_model"
    }
    assert ("AdvisorChat", "gpt-4o") in off


# --------------------------------------------------------------------------- #
# Summary + serialisation
# --------------------------------------------------------------------------- #


def test_summary_counts() -> None:
    doc = export_for(load_policy(REAL_POLICY), load_spend(REAL_SPEND))
    assert doc["summary"]["teams_evaluated"] == 6  # unowned excluded
    total = sum(doc["summary"]["by_action"].values())
    assert total == len(doc["intents"])


def test_export_json_roundtrips() -> None:
    result = evaluate(load_policy(REAL_POLICY), load_spend(REAL_SPEND), now=FRESH_NOW)
    policy = load_policy(REAL_POLICY)
    text = export_json(result, policy)
    assert isinstance(text, str)
    assert json.loads(text) == build_export(result, policy)


def test_stable_document_snapshot() -> None:
    doc = export_for(one_team_policy(at_100="block"), make_snapshot({"T": 10.0}))
    assert doc == {
        "schema_version": "1.0",
        "generated_by": "budget-controller",
        "dry_run": True,
        "evaluated_at": "2025-12-02T12:00:00+00:00",
        "as_of": "2025-12-01",
        "snapshot_age_hours": 36.0,
        "stale": False,
        "hold_relaxations": False,
        "summary": {
            "teams_evaluated": 1,
            "actionable": 1,
            "held": 0,
            "by_action": {"allow": 1},
        },
        "intents": [
            {
                "team": "T",
                "action": "allow",
                "status": "ok",
                "spend_usd": 10.0,
                "budget_usd": 1000.0,
                "percent_used": 1.0,
                "enforcement_mode": "enforce",
                "reason": "1.0% of $1,000 budget",
                "apply": False,
                "relaxing": True,
                "changes": [
                    {
                        "target": "team",
                        "id": "T",
                        "operation": "clear_overrides",
                        "params": {},
                        "note": "within budget; ensure no stale downgrade/throttle remains",
                    }
                ],
                "notifications": [],
                "app_signal": None,
                "projected_monthly_saving_usd": None,
                "recovery": None,
                "data_quality_flags": [],
            }
        ],
        "governance_violations": [],
    }

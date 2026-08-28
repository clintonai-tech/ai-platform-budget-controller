"""Tests for the CLI (Ticket 7)."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from budget_controller import __version__
from budget_controller.cli import app

runner = CliRunner()

REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_POLICY = str(REPO_ROOT / "config" / "budget_policy.yaml")
REAL_SPEND = str(REPO_ROOT / "data" / "spend_30d.csv")
FRESH = "2025-12-01"  # 1 day after the sample's last date -> not stale


def run(*args: str):
    return runner.invoke(app, ["evaluate", "--spend", REAL_SPEND, "--policy", REAL_POLICY, *args])


# --------------------------------------------------------------------------- #
# Scaffold smoke tests
# --------------------------------------------------------------------------- #


def test_help_exits_zero() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "budget" in result.output.lower()


def test_version_is_set() -> None:
    assert __version__


def test_evaluate_help_lists_options() -> None:
    result = runner.invoke(app, ["evaluate", "--help"])
    assert result.exit_code == 0
    for opt in ("--spend", "--policy", "--format", "--as-of", "--export-litellm", "--strict"):
        assert opt in result.output


# --------------------------------------------------------------------------- #
# Table output
# --------------------------------------------------------------------------- #


def test_evaluate_table_success() -> None:
    result = run("--as-of", FRESH)
    assert result.exit_code == 0
    assert "DevAgent" in result.output
    assert "over_budget" in result.output
    assert "throttle" in result.output
    assert "Governance violations" in result.output


def test_over_budget_does_not_crash_and_exits_zero_by_default() -> None:
    result = run("--as-of", FRESH)
    assert result.exit_code == 0  # DevAgent is 112% but evaluation succeeded


# --------------------------------------------------------------------------- #
# JSON output
# --------------------------------------------------------------------------- #


def test_evaluate_json_format_is_the_schema_1_doc() -> None:
    result = run("--format", "json", "--as-of", FRESH)
    assert result.exit_code == 0
    doc = json.loads(result.output)
    assert doc["schema_version"] == "1.0"
    assert doc["dry_run"] is True
    assert doc["as_of"] == "2025-11-30"
    assert {i["team"] for i in doc["intents"]} >= {"DevAgent", "__unowned__"}


# --------------------------------------------------------------------------- #
# --as-of drives staleness
# --------------------------------------------------------------------------- #


def test_as_of_fresh_is_not_stale() -> None:
    doc = json.loads(run("--format", "json", "--as-of", "2025-12-01").output)
    assert doc["stale"] is False
    assert doc["hold_relaxations"] is False


def test_as_of_old_is_stale() -> None:
    doc = json.loads(run("--format", "json", "--as-of", "2026-06-01").output)
    assert doc["stale"] is True
    assert doc["hold_relaxations"] is True


def test_bad_as_of_exits_nonzero() -> None:
    result = run("--as-of", "not-a-date")
    assert result.exit_code != 0
    assert "as-of" in result.output.lower()


# --------------------------------------------------------------------------- #
# --export-litellm
# --------------------------------------------------------------------------- #


def test_export_litellm_to_file(tmp_path: Path) -> None:
    out = tmp_path / "intent.json"
    result = run("--as-of", FRESH, "--export-litellm", str(out))
    assert result.exit_code == 0
    doc = json.loads(out.read_text())
    assert doc["schema_version"] == "1.0"
    assert doc["dry_run"] is True
    # table still went to stdout
    assert "DevAgent" in result.output


def test_export_litellm_to_stdout() -> None:
    result = run("--as-of", FRESH, "--format", "json", "--export-litellm", "-")
    assert result.exit_code == 0
    # the doc appears (once for --format json, once for the export)
    assert result.output.count('"schema_version": "1.0"') == 2


def test_export_litellm_unwritable_path_exits_2(tmp_path: Path) -> None:
    bad = tmp_path / "missing-dir" / "intent.json"
    result = run("--as-of", FRESH, "--export-litellm", str(bad))
    assert result.exit_code == 2


# --------------------------------------------------------------------------- #
# Failure paths
# --------------------------------------------------------------------------- #


def test_missing_spend_file_exits_2() -> None:
    result = runner.invoke(
        app, ["evaluate", "--spend", "/no/such.csv", "--policy", REAL_POLICY, "--as-of", FRESH]
    )
    assert result.exit_code == 2
    assert "error" in result.output.lower()


def test_invalid_policy_exits_2(tmp_path: Path) -> None:
    bad_policy = tmp_path / "policy.yaml"
    bad_policy.write_text("version: 1\nteams: {}\n", encoding="utf-8")  # fails validation
    result = runner.invoke(
        app,
        ["evaluate", "--spend", REAL_SPEND, "--policy", str(bad_policy), "--as-of", FRESH],
    )
    assert result.exit_code == 2
    assert "error" in result.output.lower()


# --------------------------------------------------------------------------- #
# --strict
# --------------------------------------------------------------------------- #


def test_strict_exits_1_when_findings_present() -> None:
    result = run("--as-of", FRESH, "--strict")
    assert result.exit_code == 1  # DevAgent over budget + governance violations
    assert "DevAgent" in result.output  # output still printed


def test_strict_exits_0_when_clean(tmp_path: Path) -> None:
    clean = tmp_path / "spend.csv"
    clean.write_text(
        "date,api_key,team,model,request_count,prompt_tokens,completion_tokens,cost_usd\n"
        "2025-12-01,key-mkt,Marketing,gpt-5.4-mini,10,1000,200,1.00\n",
        encoding="utf-8",
    )
    result = runner.invoke(
        app,
        ["evaluate", "--spend", str(clean), "--policy", REAL_POLICY, "--as-of", FRESH, "--strict"],
    )
    assert result.exit_code == 0

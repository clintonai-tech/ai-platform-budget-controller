"""Tests for the spend loader (Ticket 4)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from budget_controller.models import UNOWNED_TEAM, DataQualityFlag
from budget_controller.spend_loader import SpendLoadError, load_spend

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
VALID = FIXTURES / "spend_valid.csv"
VIOLATIONS = FIXTURES / "spend_with_violations.csv"


def write_csv(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "spend.csv"
    path.write_text(text, encoding="utf-8")
    return path


HEADER = "date,api_key,team,model,request_count,prompt_tokens,completion_tokens,cost_usd\n"


# --------------------------------------------------------------------------- #
# Clean input
# --------------------------------------------------------------------------- #


def test_valid_fixture_aggregates_by_team() -> None:
    snap = load_spend(VALID)
    assert snap.by_team["Alpha"].spend_usd == pytest.approx(7.50)
    assert snap.by_team["Alpha"].row_count == 3
    assert snap.by_team["Alpha"].request_count == 230
    assert snap.by_team["Beta"].spend_usd == pytest.approx(2.25)
    assert snap.total_spend_usd == pytest.approx(9.75)
    assert not snap.flagged_rows
    assert not snap.has_unowned_spend


def test_valid_fixture_aggregates_by_api_key_and_model() -> None:
    snap = load_spend(VALID)
    assert set(snap.by_api_key) == {"key-alpha-prod", "key-beta-prod"}
    assert snap.by_api_key["key-alpha-prod"].spend_usd == pytest.approx(7.50)
    assert snap.by_model["claude-sonnet-4-6"].spend_usd == pytest.approx(7.00)
    assert snap.by_model["gpt-4o"].spend_usd == pytest.approx(0.50)
    assert set(snap.by_team["Alpha"].models) == {"claude-sonnet-4-6", "gpt-4o"}


def test_as_of_is_max_date() -> None:
    snap = load_spend(VALID)
    assert snap.as_of == date(2025, 11, 2)
    assert snap.first_date == date(2025, 11, 1)


# --------------------------------------------------------------------------- #
# Governance / data-quality flags
# --------------------------------------------------------------------------- #


def test_missing_team_flagged_and_bucketed_as_unowned() -> None:
    snap = load_spend(VIOLATIONS)
    assert snap.has_unowned_spend
    unowned = snap.by_team[UNOWNED_TEAM]
    assert unowned.spend_usd == pytest.approx(50.00)
    assert DataQualityFlag.MISSING_TEAM in unowned.flags
    missing_rows = [r for r in snap.rows if DataQualityFlag.MISSING_TEAM in r.flags]
    assert len(missing_rows) == 1
    assert missing_rows[0].team is None
    assert missing_rows[0].is_owned is False


def test_blank_cost_flagged_and_counted_as_zero() -> None:
    snap = load_spend(VIOLATIONS)
    row = next(r for r in snap.rows if DataQualityFlag.BLANK_COST in r.flags)
    assert row.cost_usd == 0.0
    # Alpha = 3.00 (clean) + 7.25 (zero-request) + 0.00 (blank) = 10.25
    assert snap.by_team["Alpha"].spend_usd == pytest.approx(10.25)


def test_zero_requests_nonzero_cost_preserved_and_flagged() -> None:
    snap = load_spend(VIOLATIONS)
    row = next(r for r in snap.rows if DataQualityFlag.ZERO_REQUESTS_NONZERO_COST in r.flags)
    assert row.request_count == 0
    assert row.cost_usd == pytest.approx(7.25)


def test_negative_cost_flagged_and_kept() -> None:
    snap = load_spend(VIOLATIONS)
    row = next(r for r in snap.rows if DataQualityFlag.NEGATIVE_COST in r.flags)
    assert row.cost_usd == pytest.approx(-1.50)
    assert snap.by_team["Beta"].spend_usd == pytest.approx(-1.50)


def test_flagged_rows_collects_every_problem_row() -> None:
    snap = load_spend(VIOLATIONS)
    assert len(snap.flagged_rows) == 4
    assert snap.total_spend_usd == pytest.approx(58.75)


def test_short_row_is_flagged_not_fatal(tmp_path: Path) -> None:
    path = write_csv(tmp_path, HEADER + "2025-11-01,key-a,Alpha,claude-haiku-4-5,10\n")
    snap = load_spend(path)
    assert DataQualityFlag.UNPARSEABLE_ROW in snap.rows[0].flags


def test_long_row_is_flagged_not_fatal(tmp_path: Path) -> None:
    path = write_csv(
        tmp_path,
        HEADER + "2025-11-01,key-a,Alpha,claude-haiku-4-5,10,20,5,1.0,surprise\n",
    )
    snap = load_spend(path)
    assert DataQualityFlag.UNPARSEABLE_ROW in snap.rows[0].flags


def test_unparseable_numbers_flagged(tmp_path: Path) -> None:
    path = write_csv(
        tmp_path,
        HEADER + "2025-13-99,key-a,Alpha,claude-haiku-4-5,ten,x,y,1.0\n",
    )
    snap = load_spend(path)
    row = snap.rows[0]
    assert row.date is None
    assert row.request_count == 0
    assert DataQualityFlag.UNPARSEABLE_ROW in row.flags
    # flag recorded once, not once per bad field
    assert row.flags.count(DataQualityFlag.UNPARSEABLE_ROW) == 1


# --------------------------------------------------------------------------- #
# Structural failures
# --------------------------------------------------------------------------- #


def test_missing_file_raises() -> None:
    with pytest.raises(SpendLoadError, match="cannot read"):
        load_spend("/no/such/spend.csv")


def test_missing_required_column_raises(tmp_path: Path) -> None:
    path = write_csv(tmp_path, "date,api_key,team,model\n2025-11-01,k,Alpha,m\n")
    with pytest.raises(SpendLoadError, match="missing column"):
        load_spend(path)


def test_header_only_file_is_empty_snapshot(tmp_path: Path) -> None:
    path = write_csv(tmp_path, HEADER)
    snap = load_spend(path)
    assert snap.rows == ()
    assert snap.as_of is None
    assert snap.total_spend_usd == 0.0
    assert snap.by_team == {}


# --------------------------------------------------------------------------- #
# Real file smoke test
# --------------------------------------------------------------------------- #


def test_real_spend_file() -> None:
    snap = load_spend(REPO_ROOT / "data" / "spend_30d.csv")
    assert snap.as_of == date(2025, 11, 30)
    assert snap.first_date == date(2025, 11, 1)
    assert set(snap.by_team) == {
        "AdvisorChat",
        "DevAgent",
        "DigestBot",
        "KYC",
        "Marketing",
        "Research",
        UNOWNED_TEAM,
    }
    assert snap.by_team["DevAgent"].spend_usd == pytest.approx(20153.30, abs=0.01)
    assert snap.by_team[UNOWNED_TEAM].spend_usd == pytest.approx(2008.29, abs=0.01)
    # the three known anomalies are all caught
    flags = {f for r in snap.flagged_rows for f in r.flags}
    assert flags == {
        DataQualityFlag.MISSING_TEAM,
        DataQualityFlag.BLANK_COST,
        DataQualityFlag.ZERO_REQUESTS_NONZERO_COST,
    }
    # AdvisorChat and Research each touched a model outside their norm
    assert set(snap.by_team["AdvisorChat"].models) == {"claude-sonnet-4-6", "gpt-4o"}

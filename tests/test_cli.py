"""Smoke tests for the CLI scaffold.

These exist so the test suite is green with real assertions while the
logic-specific tests (spend loader, policy evaluator, exporter) are still
pending. See task2-plan.md.
"""

from typer.testing import CliRunner

from budget_controller import __version__
from budget_controller.cli import app

runner = CliRunner()


def test_help_exits_zero() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "budget" in result.output.lower()


def test_version_is_set() -> None:
    assert __version__

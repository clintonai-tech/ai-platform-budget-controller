"""Command-line interface for the budget controller.

    budget-controller evaluate --spend data/spend_30d.csv --policy config/budget_policy.yaml

Reads a spend snapshot and a budget policy, evaluates every team against the
tiered thresholds, and prints the decisions as a table (default) or as the
schema 1.0 policy-intent JSON. ``--export-litellm`` also writes that JSON to a
file. Nothing is ever pushed to a live gateway.

Exit codes: ``0`` success, ``1`` findings present with ``--strict``,
``2`` the inputs could not be read or validated.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer

from budget_controller.evaluator import EvaluationResult, evaluate
from budget_controller.exporters import export_json
from budget_controller.models import BudgetStatus
from budget_controller.policy import PolicyError, load_policy
from budget_controller.spend_loader import SpendLoadError, load_spend

app = typer.Typer(
    help="Evaluate team LLM spend against tiered budget policy.",
    no_args_is_help=True,
    add_completion=False,
)

DEFAULT_SPEND = Path("data/spend_30d.csv")
DEFAULT_POLICY = Path("config/budget_policy.yaml")


class OutputFormat(StrEnum):
    table = "table"
    json = "json"


@app.callback()
def main() -> None:
    """Budget controller CLI entry point."""


@app.command("evaluate")
def evaluate_command(
    spend: Annotated[Path, typer.Option("--spend", help="Path to the spend CSV.")] = DEFAULT_SPEND,
    policy: Annotated[
        Path, typer.Option("--policy", help="Path to the budget policy YAML.")
    ] = DEFAULT_POLICY,
    output_format: Annotated[
        OutputFormat, typer.Option("--format", help="Output format.", case_sensitive=False)
    ] = OutputFormat.table,
    as_of: Annotated[
        str | None,
        typer.Option("--as-of", help="Evaluate as at this ISO date/datetime (UTC assumed)."),
    ] = None,
    export_litellm: Annotated[
        Path | None,
        typer.Option("--export-litellm", help="Also write policy-intent JSON here ('-' = stdout)."),
    ] = None,
    strict: Annotated[
        bool,
        typer.Option("--strict", help="Exit 1 if any enforcement action or violation is present."),
    ] = False,
) -> None:
    """Evaluate team spend against the tiered budget policy."""
    loaded_policy = _load(load_policy, policy, PolicyError)
    snapshot = _load(load_spend, spend, SpendLoadError)
    now = _resolve_now(as_of)

    result = evaluate(loaded_policy, snapshot, now=now)

    if output_format is OutputFormat.json:
        typer.echo(export_json(result, loaded_policy))
    else:
        typer.echo(_render_table(result))

    if export_litellm is not None:
        document = export_json(result, loaded_policy)
        if str(export_litellm) == "-":
            typer.echo(document)
        else:
            try:
                export_litellm.write_text(document + "\n", encoding="utf-8")
            except OSError as exc:
                typer.secho(f"error: cannot write {export_litellm}: {exc}", fg="red", err=True)
                raise typer.Exit(2) from exc
            typer.secho(f"wrote policy intent to {export_litellm}", fg="green", err=True)

    if strict and _has_findings(result):
        raise typer.Exit(1)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _load(loader, path, error_type):  # type: ignore[no-untyped-def]
    try:
        return loader(path)
    except error_type as exc:
        typer.secho(f"error: {exc}", fg="red", err=True)
        raise typer.Exit(2) from exc


def _resolve_now(as_of: str | None) -> datetime:
    if as_of is None:
        return datetime.now(tz=UTC)
    try:
        parsed = datetime.fromisoformat(as_of)
    except ValueError as exc:
        raise typer.BadParameter(f"--as-of must be an ISO date or datetime, got {as_of!r}") from exc
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _has_findings(result: EvaluationResult) -> bool:
    enforced = {BudgetStatus.OVER_BUDGET, BudgetStatus.QUARANTINED}
    return bool(result.governance_violations) or any(d.status in enforced for d in result.decisions)


def _render_table(result: EvaluationResult) -> str:
    lines: list[str] = []
    as_of = result.as_of.isoformat() if result.as_of else "n/a"
    lines.append(
        f"Budget evaluation - as of {as_of} "
        f"(snapshot {result.snapshot_age_hours:.0f}h old, "
        f"stale={'yes' if result.stale else 'no'})"
    )
    lines.append("")

    header = ("TEAM", "SPEND", "BUDGET", "USED", "STATUS", "ACTION", "NOTES")
    rows: list[tuple[str, ...]] = [header]
    for d in result.decisions:
        budget = f"${d.budget_usd:,.2f}" if d.budget_usd is not None else "-"
        used = f"{d.percent_used:.1f}%" if d.percent_used is not None else "-"
        notes = ", ".join(f.value for f in d.data_quality_flags)
        rows.append(
            (
                d.team,
                f"${d.spend_usd:,.2f}",
                budget,
                used,
                d.status.value,
                d.action.value,
                notes,
            )
        )
    widths = [max(len(r[i]) for r in rows) for i in range(len(header))]
    for row in rows:
        lines.append("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip())

    if result.governance_violations:
        lines.append("")
        lines.append(f"Governance violations ({len(result.governance_violations)}):")
        for v in result.governance_violations:
            where = v.team or v.api_key or "-"
            loc = f"line {v.line_number}" if v.line_number is not None else "-"
            lines.append(
                f"  {v.kind.value:<26} {loc:<9} {where:<22} "
                f"{(v.model or ''):<18} ${v.spend_usd:,.2f}  {v.detail}"
            )

    return "\n".join(lines)


if __name__ == "__main__":  # pragma: no cover
    app()

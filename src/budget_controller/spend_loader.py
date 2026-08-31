"""Parse the spend CSV into a :class:`SpendSnapshot`.

Design choices:

* **Tolerant rows, strict shape.** A missing file or a missing header column is
  a hard :class:`SpendLoadError` - the input is unusable. A bad *value* inside a
  row is not: it is coerced to a safe default and recorded as a
  :class:`DataQualityFlag` so the evaluator and the operator can see it.
* **Standard-library ``csv`` only** - the file is ~150 rows and auditability
  matters more than convenience.
* **No policy here.** The loader does not know team allow-lists, so it cannot
  judge "off-catalogue model" - that check lives in the evaluator, which has the
  policy. The loader only reports what the data says.

The expected columns are::

    date,api_key,team,model,request_count,prompt_tokens,completion_tokens,cost_usd
"""

from __future__ import annotations

import csv
from collections.abc import Iterable
from datetime import date, datetime
from pathlib import Path

from budget_controller.models import (
    UNOWNED_TEAM,
    DataQualityFlag,
    GroupSpend,
    SpendRow,
    SpendSnapshot,
)

REQUIRED_COLUMNS: tuple[str, ...] = (
    "date",
    "api_key",
    "team",
    "model",
    "request_count",
    "prompt_tokens",
    "completion_tokens",
    "cost_usd",
)


class SpendLoadError(Exception):
    """The spend file is missing, unreadable, or has the wrong columns."""


def load_spend(path: str | Path) -> SpendSnapshot:
    """Read *path* and return a parsed, aggregated :class:`SpendSnapshot`."""
    file_path = Path(path)
    try:
        text = file_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SpendLoadError(f"cannot read spend file '{file_path}': {exc}") from exc

    reader = csv.DictReader(text.splitlines(), restkey="_extra", restval=None)
    header = reader.fieldnames or []
    missing = [c for c in REQUIRED_COLUMNS if c not in header]
    if missing:
        raise SpendLoadError(f"spend file '{file_path}' is missing column(s): {', '.join(missing)}")

    rows = tuple(_parse_row(raw, i) for i, raw in enumerate(reader, start=2))
    return _aggregate(rows)


# --------------------------------------------------------------------------- #
# Row parsing
# --------------------------------------------------------------------------- #


def _parse_row(raw: dict[str, str | list[str] | None], line_number: int) -> SpendRow:
    flags: list[DataQualityFlag] = []

    # A short row leaves required keys at restval=None; a long row puts the
    # surplus under "_extra". Either way the row shape is wrong.
    has_extra = bool(raw.get("_extra"))
    is_short = any(raw.get(c) is None for c in REQUIRED_COLUMNS)
    if has_extra or is_short:
        flags.append(DataQualityFlag.UNPARSEABLE_ROW)

    # Normalise every required cell to a plain string for the parsers below.
    cell = {c: (v.strip() if isinstance(v := raw.get(c), str) else "") for c in REQUIRED_COLUMNS}

    parsed_date = _parse_date(cell["date"])
    if parsed_date is None and DataQualityFlag.UNPARSEABLE_ROW not in flags:
        flags.append(DataQualityFlag.UNPARSEABLE_ROW)

    api_key = cell["api_key"]
    model = cell["model"]

    team = cell["team"] or None
    if team is None:
        flags.append(DataQualityFlag.MISSING_TEAM)

    request_count, rc_ok = _parse_int(cell["request_count"])
    prompt_tokens, pt_ok = _parse_int(cell["prompt_tokens"])
    completion_tokens, ct_ok = _parse_int(cell["completion_tokens"])
    if not (rc_ok and pt_ok and ct_ok) and DataQualityFlag.UNPARSEABLE_ROW not in flags:
        flags.append(DataQualityFlag.UNPARSEABLE_ROW)

    cost = _parse_float(cell["cost_usd"])
    if cost is None:
        flags.append(DataQualityFlag.BLANK_COST)
        cost = 0.0
    elif cost < 0:
        flags.append(DataQualityFlag.NEGATIVE_COST)

    if request_count == 0 and cost > 0:
        flags.append(DataQualityFlag.ZERO_REQUESTS_NONZERO_COST)

    return SpendRow(
        line_number=line_number,
        date=parsed_date,
        api_key=api_key,
        team=team,
        model=model,
        request_count=request_count,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cost_usd=cost,
        flags=_dedupe(flags),
    )


def _parse_date(value: str | None) -> date | None:
    if value is None:
        return None
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def _parse_int(value: str | None) -> tuple[int, bool]:
    """Return ``(number, ok)``. On failure return ``(0, False)``."""
    if value is None or value.strip() == "":
        return 0, False
    try:
        return int(value.strip()), True
    except ValueError:
        return 0, False


def _parse_float(value: str | None) -> float | None:
    if value is None or value.strip() == "":
        return None
    try:
        return float(value.strip())
    except ValueError:
        return None


def _dedupe(flags: Iterable[DataQualityFlag]) -> tuple[DataQualityFlag, ...]:
    seen: dict[DataQualityFlag, None] = {}
    for f in flags:
        seen.setdefault(f, None)
    return tuple(seen)


# --------------------------------------------------------------------------- #
# Aggregation
# --------------------------------------------------------------------------- #


class _Accumulator:
    __slots__ = (
        "spend",
        "requests",
        "prompt_tokens",
        "completion_tokens",
        "rows",
        "models",
        "flags",
    )

    def __init__(self) -> None:
        self.spend = 0.0
        self.requests = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.rows = 0
        self.models: dict[str, None] = {}
        self.flags: dict[DataQualityFlag, None] = {}

    def add(self, row: SpendRow) -> None:
        self.spend += row.cost_usd
        self.requests += row.request_count
        self.prompt_tokens += row.prompt_tokens
        self.completion_tokens += row.completion_tokens
        self.rows += 1
        if row.model:
            self.models.setdefault(row.model, None)
        for f in row.flags:
            self.flags.setdefault(f, None)

    def freeze(self, label: str) -> GroupSpend:
        return GroupSpend(
            label=label,
            spend_usd=round(self.spend, 6),
            request_count=self.requests,
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
            row_count=self.rows,
            models=tuple(self.models),
            flags=tuple(self.flags),
        )


def _aggregate(rows: tuple[SpendRow, ...]) -> SpendSnapshot:
    by_team: dict[str, _Accumulator] = {}
    by_key: dict[str, _Accumulator] = {}
    by_model: dict[str, _Accumulator] = {}

    for row in rows:
        team_label = row.team if row.team is not None else UNOWNED_TEAM
        by_team.setdefault(team_label, _Accumulator()).add(row)
        by_key.setdefault(row.api_key, _Accumulator()).add(row)
        by_model.setdefault(row.model, _Accumulator()).add(row)

    dates = [r.date for r in rows if r.date is not None]
    total = round(sum(r.cost_usd for r in rows), 6)

    return SpendSnapshot(
        rows=rows,
        as_of=max(dates) if dates else None,
        first_date=min(dates) if dates else None,
        by_team={k: acc.freeze(k) for k, acc in by_team.items()},
        by_api_key={k: acc.freeze(k) for k, acc in by_key.items()},
        by_model={k: acc.freeze(k) for k, acc in by_model.items()},
        total_spend_usd=total,
    )

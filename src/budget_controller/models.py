"""Typed domain models for the budget controller.

Three groups live here:

* **Enums** — the closed vocabularies shared across the codebase
  (:class:`Action`, :class:`BudgetStatus`, :class:`EnforcementMode`,
  :class:`Tier`, :class:`DataQualityFlag`).
* **Policy models** — the validated shape of ``config/budget_policy.yaml``
  (:class:`BudgetPolicy` and its parts). Loading is done in ``policy.py``.
* **Spend models** — :class:`SpendRow`, :class:`GroupSpend`, :class:`SpendSnapshot`,
  the tabular structures produced by ``spend_loader.py`` (frozen dataclasses, not
  Pydantic: the loader parses tolerantly and records problems as flags).
* **Output model** — :class:`Decision`, one per team per evaluation run.

All models forbid unknown fields so a typo in the policy file fails loudly
rather than being silently ignored.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: ``by_team`` key used for spend on API keys that carry no team.
UNOWNED_TEAM = "__unowned__"

# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #


class Action(StrEnum):
    """What the controller wants the gateway (or the app) to do."""

    ALLOW = "allow"
    WARN = "warn"
    URGENT_WARN = "urgent_warn"
    DOWNGRADE = "downgrade"
    THROTTLE = "throttle"
    BLOCK = "block"
    REQUIRE_EXPLICIT_SIGNAL = "require_explicit_signal"
    QUARANTINE = "quarantine"


#: Actions that are valid as a team's ``at_100_percent`` enforcement choice.
ENFORCEMENT_ACTIONS: frozenset[Action] = frozenset(
    {
        Action.DOWNGRADE,
        Action.THROTTLE,
        Action.BLOCK,
        Action.REQUIRE_EXPLICIT_SIGNAL,
    }
)


class BudgetStatus(StrEnum):
    """Which threshold band a team's spend falls into."""

    OK = "ok"
    WARN = "warn"
    URGENT = "urgent"
    OVER_BUDGET = "over_budget"
    QUARANTINED = "quarantined"


class EnforcementMode(StrEnum):
    """Whether a team's decisions should actually be applied."""

    ENFORCE = "enforce"
    MONITOR = "monitor"


class Tier(StrEnum):
    """Relative cost/capability class of a model."""

    PREMIUM = "premium"
    STANDARD = "standard"
    ECONOMY = "economy"


class DataQualityFlag(StrEnum):
    """Governance / data-quality problems found while aggregating spend."""

    MISSING_TEAM = "missing_team"
    BLANK_COST = "blank_cost"
    NEGATIVE_COST = "negative_cost"
    ZERO_REQUESTS_NONZERO_COST = "zero_requests_nonzero_cost"
    OFF_CATALOGUE_MODEL = "off_catalogue_model"
    UNPARSEABLE_ROW = "unparseable_row"


# --------------------------------------------------------------------------- #
# Policy models
# --------------------------------------------------------------------------- #


class Thresholds(BaseModel):
    """Tier boundaries as a fraction of the monthly budget."""

    model_config = ConfigDict(extra="forbid")

    warn_at: float = Field(gt=0, le=1)
    urgent_warn_at: float = Field(gt=0, le=1)
    enforce_at: float = Field(gt=0)

    @model_validator(mode="after")
    def _ordered(self) -> Thresholds:
        if not self.warn_at < self.urgent_warn_at <= self.enforce_at:
            raise ValueError(
                "thresholds must satisfy warn_at < urgent_warn_at <= enforce_at "
                f"(got {self.warn_at}, {self.urgent_warn_at}, {self.enforce_at})"
            )
        return self


class Defaults(BaseModel):
    """Global settings that are not team-specific."""

    model_config = ConfigDict(extra="forbid")

    thresholds: Thresholds
    max_staleness_hours: float = Field(gt=0)
    currency: str = "USD"


class ModelPrice(BaseModel):
    """Directional price for one model, USD per 1M tokens."""

    model_config = ConfigDict(extra="forbid")

    tier: Tier
    input_per_1m: float = Field(ge=0)
    output_per_1m: float = Field(ge=0)


class TeamPolicy(BaseModel):
    """Budget policy for a single team."""

    model_config = ConfigDict(extra="forbid")

    monthly_budget_usd: float = Field(gt=0)
    default_model: str
    allowed_models: list[str] = Field(min_length=1)
    enforcement_mode: EnforcementMode
    silent_downgrade_allowed: bool
    at_100_percent: Action
    downgrade_model: str | None = None
    escalation: str
    notes: str = ""

    @model_validator(mode="after")
    def _check_enforcement_action(self) -> TeamPolicy:
        if self.at_100_percent not in ENFORCEMENT_ACTIONS:
            allowed = ", ".join(sorted(a.value for a in ENFORCEMENT_ACTIONS))
            raise ValueError(
                f"at_100_percent must be one of [{allowed}], got '{self.at_100_percent.value}'"
            )
        if self.at_100_percent is Action.DOWNGRADE and self.downgrade_model is None:
            raise ValueError("at_100_percent 'downgrade' requires a downgrade_model")
        if self.silent_downgrade_allowed and self.downgrade_model is None:
            raise ValueError("silent_downgrade_allowed is true but no downgrade_model is set")
        return self


class UnownedPolicy(BaseModel):
    """What to do with spend on a key that has no team."""

    model_config = ConfigDict(extra="forbid")

    action: Action
    escalation: str
    notes: str = ""

    @model_validator(mode="after")
    def _check_action(self) -> UnownedPolicy:
        if self.action not in (Action.QUARANTINE, Action.BLOCK):
            raise ValueError(
                f"unowned.action must be 'quarantine' or 'block', got '{self.action.value}'"
            )
        return self


class BudgetPolicy(BaseModel):
    """The whole ``config/budget_policy.yaml`` file, validated."""

    model_config = ConfigDict(extra="forbid")

    version: int
    defaults: Defaults
    models: dict[str, ModelPrice] = Field(min_length=1)
    teams: dict[str, TeamPolicy] = Field(min_length=1)
    unowned: UnownedPolicy

    @model_validator(mode="after")
    def _check_model_references(self) -> BudgetPolicy:
        known = set(self.models)
        for name, team in self.teams.items():
            referenced = [team.default_model, *team.allowed_models]
            if team.downgrade_model is not None:
                referenced.append(team.downgrade_model)
            unknown = sorted({m for m in referenced if m not in known})
            if unknown:
                raise ValueError(f"team '{name}' references model(s) not in models: {unknown}")
            if team.default_model not in team.allowed_models:
                raise ValueError(
                    f"team '{name}' default_model '{team.default_model}' "
                    "is not in its allowed_models"
                )
        return self


# --------------------------------------------------------------------------- #
# Output model
# --------------------------------------------------------------------------- #


class Decision(BaseModel):
    """One evaluation result for one team (or the unowned bucket).

    ``percent_used`` is a percentage (``92.3`` means 92.3% of budget), not a
    fraction. ``budget_usd`` and ``percent_used`` are ``None`` for the unowned
    bucket, which has no budget. ``snapshot_age_hours`` / ``snapshot_stale``
    record how old the spend data was at evaluation time.
    """

    model_config = ConfigDict(extra="forbid")

    team: str
    is_owned: bool = True
    budget_usd: float | None
    spend_usd: float
    percent_used: float | None
    status: BudgetStatus
    action: Action
    reason: str
    enforcement_mode: EnforcementMode | None = None
    downgrade_from: str | None = None
    downgrade_to: str | None = None
    as_of: date
    snapshot_age_hours: float
    snapshot_stale: bool
    data_quality_flags: list[DataQualityFlag] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Spend models (populated by spend_loader.py)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SpendRow:
    """One CSV row after tolerant parsing.

    Nothing here raises on bad input: unparseable or missing values are coerced
    to a safe default (``None`` date, ``0`` counts, ``0.0`` cost) and the
    problem is recorded in :attr:`flags` so it stays visible downstream.
    """

    line_number: int
    date: date | None
    api_key: str
    team: str | None
    model: str
    request_count: int
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    flags: tuple[DataQualityFlag, ...] = ()

    @property
    def is_owned(self) -> bool:
        return self.team is not None


@dataclass(frozen=True)
class GroupSpend:
    """Accumulated spend for one grouping key (a team, an API key, or a model)."""

    label: str
    spend_usd: float
    request_count: int
    prompt_tokens: int
    completion_tokens: int
    row_count: int
    models: tuple[str, ...] = ()
    flags: tuple[DataQualityFlag, ...] = ()


@dataclass(frozen=True)
class SpendSnapshot:
    """The whole spend file after parsing and aggregation."""

    rows: tuple[SpendRow, ...]
    as_of: date | None
    first_date: date | None
    by_team: dict[str, GroupSpend] = field(default_factory=dict)
    by_api_key: dict[str, GroupSpend] = field(default_factory=dict)
    by_model: dict[str, GroupSpend] = field(default_factory=dict)
    total_spend_usd: float = 0.0

    @property
    def flagged_rows(self) -> tuple[SpendRow, ...]:
        return tuple(r for r in self.rows if r.flags)

    @property
    def has_unowned_spend(self) -> bool:
        return UNOWNED_TEAM in self.by_team

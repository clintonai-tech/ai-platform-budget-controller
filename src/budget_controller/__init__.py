"""Budget controller package for tiered LLM spend enforcement.

Public API::

    from budget_controller import load_policy, load_spend, evaluate, build_export

    policy = load_policy("config/budget_policy.yaml")
    snapshot = load_spend("data/spend_30d.csv")
    result = evaluate(policy, snapshot, now=datetime.now(tz=UTC))
    document = build_export(result, policy)
"""

from budget_controller.evaluator import EvaluationResult, evaluate
from budget_controller.exporters import build_export, export_json
from budget_controller.policy import PolicyError, load_policy
from budget_controller.spend_loader import SpendLoadError, load_spend

__all__ = [
    "EvaluationResult",
    "PolicyError",
    "SpendLoadError",
    "__version__",
    "build_export",
    "evaluate",
    "export_json",
    "load_policy",
    "load_spend",
]

__version__ = "0.1.0"

"""Load and validate ``config/budget_policy.yaml`` into a :class:`BudgetPolicy`.

Everything that can go wrong with the file - missing, unreadable, not YAML,
duplicate keys, or failing schema validation - is surfaced as a single
:class:`PolicyError` with a message a teammate can act on.

``pyyaml`` silently keeps the last value for a duplicated mapping key, which
would let a copy-pasted team block hide another. :class:`_UniqueKeyLoader`
rejects duplicate keys instead, so ``teams:`` (and every other mapping) is
checked for accidental repeats.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from budget_controller.models import BudgetPolicy


class PolicyError(Exception):
    """The policy file is missing, unparseable, or fails validation."""


class _UniqueKeyLoader(yaml.SafeLoader):
    """SafeLoader that raises on duplicate mapping keys."""

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        seen: set[Any] = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in seen:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    f"found duplicate key {key!r}",
                    key_node.start_mark,
                )
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


def load_policy(path: str | Path) -> BudgetPolicy:
    """Read *path* and return a validated :class:`BudgetPolicy`.

    Raises :class:`PolicyError` on any failure.
    """
    file_path = Path(path)
    try:
        raw = file_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PolicyError(f"cannot read policy file '{file_path}': {exc}") from exc

    try:
        data = yaml.load(raw, Loader=_UniqueKeyLoader)  # noqa: S506 - _UniqueKeyLoader is safe
    except yaml.YAMLError as exc:
        raise PolicyError(f"policy file '{file_path}' is not valid YAML: {exc}") from exc

    if not isinstance(data, dict):
        raise PolicyError(f"policy file '{file_path}' must contain a mapping at the top level")

    try:
        return BudgetPolicy.model_validate(data)
    except ValidationError as exc:
        raise PolicyError(f"policy file '{file_path}' failed validation:\n{exc}") from exc

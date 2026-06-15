"""Minimal `UserSpec` dataclass used by the federation runner.

This is the only symbol the adapter (`skillfl/skillflow_adapter/`) needs from
`skillfl.harbor_runner`. Upstream SkillFL ships a larger module here with
Jinja2-templated YAML rendering and round-execution helpers; the federation
adapter does its own YAML composition (see `cli.py`) and never invokes those,
so we keep only the dataclass.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class UserSpec:
    """One federated 'user' = one (agent harness, model) pair plus a stable id."""
    id: str
    agent_name: str
    agent_import_path: str | None
    agent_model: str
    agent_kwargs: dict[str, Any] = field(default_factory=dict)

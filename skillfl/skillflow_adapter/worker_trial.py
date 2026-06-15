"""Runner-internal trial-result type.

The real harbor returns its own `TrialResult` class with a lot of fields we
don't need. Defining our own small struct here keeps the runner independent of
harbor's type surface and lets the dry-run bridge return identical-shaped data
without importing harbor at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class WorkerTrialResult:
    """One worker's one-trial outcome after harbor (or dry-run) runs a task."""

    worker_id: str
    task_name: str
    reward: float | None
    verifier_passed: bool
    # Absolute path to the harbor trial dir (has agent/, verifier/, result.json).
    # For dry-run this points to a fake dir we created with the expected layout.
    trial_dir: Path
    # Optional error info — None if trial ran to completion.
    exception_type: str | None = None
    exception_message: str | None = None
    # Free-form context the runner may want to log.
    extra: dict = field(default_factory=dict)

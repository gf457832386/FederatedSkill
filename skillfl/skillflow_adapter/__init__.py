"""Federated skill-evolution runner.

Design: strategy pattern over three orthogonal knobs so we can sweep settings
without rewriting the runner.

- TaskPartitioner: how N tasks are assigned to M workers (default: ReplicatePartitioner)
- SyncSchedule: when workers pool their patches (default: EveryTaskSync)
- PatchMerger: how conflicting patches from different workers are reconciled.
  Primary: CloudSkillMerge (per-worker LLM-agent merger). Fallback inside
  CloudSkillMerge: RewardWeightedFileMerge (deterministic, per-path
  highest-reward wins).
"""

from skillfl.skillflow_adapter.config import FedConfig
from skillfl.skillflow_adapter.merge import (
    CloudSkillMerge,
    MergedPatch,
    PatchMerger,
    RewardWeightedFileMerge,
    WorkerPatch,
)
from skillfl.skillflow_adapter.partitioning import (
    BlockPartitioner,
    RandomPartitioner,
    ReplicatePartitioner,
    RoundRobinPartitioner,
    TaskPartitioner,
)
from skillfl.skillflow_adapter.sync_schedule import (
    EveryKTaskSync,
    EveryTaskSync,
    OnceAtEndSync,
    SyncSchedule,
)

__all__ = [
    "FedConfig",
    # partitioning
    "TaskPartitioner",
    "RoundRobinPartitioner",
    "BlockPartitioner",
    "RandomPartitioner",
    "ReplicatePartitioner",
    # sync
    "SyncSchedule",
    "EveryTaskSync",
    "EveryKTaskSync",
    "OnceAtEndSync",
    # merge
    "PatchMerger",
    "RewardWeightedFileMerge",
    "CloudSkillMerge",
    "WorkerPatch",
    "MergedPatch",
]

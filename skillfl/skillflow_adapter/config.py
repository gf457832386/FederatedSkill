"""Run-time configuration for the federated skill-evolution runner.

This holds *choices* (strategies, worker specs, paths); YAML parsing +
Harbor/LLM credentials live in the runner. Strategies are injected as objects,
not strings, so the runner doesn't need a string-to-strategy dispatch — the
caller (CLI or test) instantiates the strategy and hands it in.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from skillfl.harbor_runner import UserSpec
from skillfl.skillflow_adapter.merge import PatchMerger, RewardWeightedFileMerge
from skillfl.skillflow_adapter.partitioning import RoundRobinPartitioner, TaskPartitioner
from skillfl.skillflow_adapter.sync_schedule import EveryTaskSync, SyncSchedule


@dataclass
class FedConfig:
    """All the knobs for one federated run on one family.

    The runner loops over families; each family gets its own FedConfig built
    from the top-level YAML + its own ranked task list.
    """

    # --- Required fields (no default) come first per @dataclass rules. ---

    # Identity / output.
    run_id: str
    runs_root: Path           # e.g. SkillFlow-benchmark/jobs/
    family_name: str          # e.g. "Compensation-Scenario-Modeling"
    task_dirs: list[Path]     # ordered per the family's ranking file

    # Workers: M UserSpec entries (homogeneous = list of M identical specs;
    # heterogeneous = distinct specs). Worker id defaults to UserSpec.id.
    workers: list[UserSpec]

    # Per-worker patcher LLM configs — keyed by worker_id, each carrying
    # {model_name, api_base, api_key, temperature?, max_tokens?, ...}. Each
    # worker's trajectory is distilled by THAT worker's own model. Required
    # in real mode (PatcherBridge); may be None for dry-run.
    patcher_worker_llm_configs: dict[str, dict] | None

    # --- Optional fields (with defaults) below. ---

    # Strategies (default = the combo we agreed on; all overridable).
    partitioner: TaskPartitioner = field(default_factory=RoundRobinPartitioner)
    sync_schedule: SyncSchedule = field(default_factory=EveryTaskSync)
    merger: PatchMerger = field(default_factory=RewardWeightedFileMerge)

    # Skill dir template (copied in at round 0 if shared_skills_dir is empty).
    project_template_dir: Path | None = None

    # Harbor knobs.
    harbor_concurrency: int | None = None   # default = len(workers) per sync round
    force_shared_env: bool = True
    docker_image_prefix: str = "harbor-prebuilt"

    # Per-worker isolated skill dirs during a round. When True the runner
    # gives each worker its own bind-mounted skill dir for the trial, so
    # concurrent writes from M workers on the same task don't trample each
    # other. After a sync, the merged master state is copied back to all
    # worker dirs so they restart aligned. Default False = original
    # shared-dir behavior (workers see each other's mid-task writes).
    isolated_worker_skills: bool = False

    # Merge mode:
    #   "shared"  — single shared library; merger sees all M patches and
    #               produces ONE MergedPatch applied to the master dir, then
    #               synced back to per-worker dirs.
    #   "unshared" — every worker keeps its own diverging library. Merger is
    #               invoked once per worker (CloudSkillMerge / any subclass
    #               that overrides merge_per_worker). The runner resets each
    #               worker dir to its pre-task snapshot, then applies that
    #               worker's MergedPatch on top. No master sync. Implies
    #               isolated_worker_skills=True.
    merger_mode: str = "shared"

    def validate(self) -> None:
        if not self.workers:
            raise ValueError("FedConfig.workers must be non-empty")
        if not self.task_dirs:
            raise ValueError("FedConfig.task_dirs must be non-empty")
        # Each worker id must be unique — merge logic uses it as the tie-break key.
        ids = [w.id for w in self.workers]
        if len(set(ids)) != len(ids):
            raise ValueError(f"Duplicate worker ids: {ids}")
        if self.merger_mode not in {"shared", "unshared"}:
            raise ValueError(
                f"merger_mode must be 'shared' or 'unshared', got {self.merger_mode!r}"
            )
        if self.merger_mode == "unshared" and not self.isolated_worker_skills:
            raise ValueError(
                "merger_mode='unshared' requires isolated_worker_skills=true "
                "(each worker needs its own dir to diverge in)"
            )

    @property
    def n_workers(self) -> int:
        return len(self.workers)

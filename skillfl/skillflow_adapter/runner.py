"""Federated skill-evolution runner for one benchmark family.

Main loop (from the research setting in docs/port-to-skillflow-benchmark.md):

    shards = partitioner.partition(family_tasks, n_workers)
    for round r in 0..max_shard_len-1:
        assignments = [(worker_i, shards[i][r]) for workers with a task this round]
        trials      = await asyncio.gather(*[harbor.launch_trial(w, t, skill_dir)
                                             for (w, t) in assignments])
        patches     = [patcher.generate(trial, skill_snapshot) for trial in trials]
        pending.extend(patches)
        if sync_schedule.should_sync(r, is_last_round):
            merged = merger.merge(pending)
            apply_to(skill_dir, merged)
            pending = []

The runner writes per-round and per-family artifacts, and computes a summary
at the end. It does NOT iterate over families — callers do that outside.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from skillfl.skillflow_adapter.config import FedConfig
from skillfl.skillflow_adapter.harbor_bridge import (
    DryRunHarborBridge,
    HarborBridge,
    HarborBridgeBase,
)
from skillfl.skillflow_adapter.merge import MergedPatch, WorkerPatch, safe_rel_path
from skillfl.skillflow_adapter.patcher_bridge import (
    DryRunPatcherBridge,
    PatcherBridge,
    PatcherBridgeBase,
)
from skillfl.skillflow_adapter.worker_trial import WorkerTrialResult


@dataclass
class RoundRecord:
    round_idx: int
    assignments: list[tuple[str, str]]  # [(worker_id, task_name), ...]
    rewards: dict[str, float | None]     # worker_id → reward
    exceptions: dict[str, str | None]    # worker_id → exception_type (or None)
    patches: list[WorkerPatch]
    merged: MergedPatch | None           # None if this round didn't sync
    elapsed_sec: float


@dataclass
class FamilyResult:
    family_name: str
    run_dir: Path
    shards: list[list[str]]              # per-worker ordered task names
    rounds: list[RoundRecord] = field(default_factory=list)
    elapsed_sec: float = 0.0
    # Where the final skill library lives. In shared mode this is one Path
    # (the master shared dir). In unshared mode it's a dict[worker_id → Path]
    # since each worker has its own diverging library.
    final_skill_dir: Path | dict[str, Path] | None = None


class FedRunner:
    def __init__(
        self,
        cfg: FedConfig,
        *,
        dry_run: bool = False,
        skillflow_root: Path | None = None,
        dry_run_seed: int = 0,
    ) -> None:
        cfg.validate()
        self.cfg = cfg
        self.dry_run = dry_run
        self.skillflow_root = skillflow_root

        # Per-family run dir: runs_root/<run_id>/<family>/
        self.run_dir = cfg.runs_root / cfg.run_id / cfg.family_name
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.shared_skills_dir = self.run_dir / "shared_skills" / cfg.family_name
        self.shared_skills_dir.mkdir(parents=True, exist_ok=True)

        # Seed shared skills dir from template if configured and dir is empty.
        self._maybe_seed_template()

        # Per-worker isolated skill dirs (only populated when isolated mode is
        # on). Each maps worker_id → runs_root/<run_id>/<family>/worker_skills/
        # <worker_id>/<family>/. Harbor's SharedSkillsDockerEnvironment binds
        # the *parent* of this path (i.e. .../worker_skills/<worker_id>/) and
        # auto-resolves the <family> subdir as the actual mount, matching the
        # master shared dir layout.
        self._worker_skill_dirs: dict[str, Path] = {}
        if cfg.isolated_worker_skills:
            for w in cfg.workers:
                wsd = (
                    self.run_dir / "worker_skills" / w.id / cfg.family_name
                )
                wsd.mkdir(parents=True, exist_ok=True)
                self._worker_skill_dirs[w.id] = wsd
            # Round-0 seed: each worker starts from whatever the master holds
            # right now (post `_maybe_seed_template`). Use shutil rather than
            # the merger path to keep this independent of merge logic.
            self._sync_master_to_workers()

        # Wire up bridges.
        self._harbor: HarborBridgeBase
        self._patcher: PatcherBridgeBase
        if dry_run:
            self._harbor = DryRunHarborBridge(seed=dry_run_seed)
            self._patcher = DryRunPatcherBridge(seed=dry_run_seed)
        else:
            if skillflow_root is None:
                raise ValueError("skillflow_root required in real mode")
            self._harbor = HarborBridge(
                family_name=cfg.family_name,
                skillflow_root=skillflow_root,
                project_template_dir=cfg.project_template_dir,
                force_shared_env=cfg.force_shared_env,
            )
            if not cfg.patcher_worker_llm_configs:
                raise ValueError(
                    "FedConfig.patcher_worker_llm_configs is required in real "
                    "mode (per-worker patcher routing — patcher is always the "
                    "worker's own model)"
                )
            self._patcher = PatcherBridge(
                skillflow_root=skillflow_root,
                worker_llm_configs=cfg.patcher_worker_llm_configs,
            )

    # -----------------------------------------------------------------------
    # Public entrypoint
    # -----------------------------------------------------------------------

    async def run(self) -> FamilyResult:
        t0 = time.time()
        shards = self.cfg.partitioner.partition(
            list(self.cfg.task_dirs), self.cfg.n_workers
        )
        assert len(shards) == self.cfg.n_workers
        n_rounds = max((len(s) for s in shards), default=0)

        result = FamilyResult(
            family_name=self.cfg.family_name,
            run_dir=self.run_dir,
            shards=[[p.name for p in s] for s in shards],
        )
        self._write_shard_manifest(shards)

        # Auto-resume: if rounds 0..K-1 already have round_summary.json on
        # disk (a prior bailed or interrupted run), skip them and continue
        # from round K. Worker skill dirs on disk already reflect the
        # post-merge state of round K-1 — we don't re-seed.
        start_round = self._discover_completed_rounds(n_rounds)
        if start_round > 0:
            # Rebuild RoundRecord stubs so family_summary covers all rounds.
            for r in range(start_round):
                rec = self._load_round_record(r)
                if rec is not None:
                    result.rounds.append(rec)
            # Clear any prior bail marker — we're attempting again.
            bail_marker = self.run_dir / "BAILED_OUT.txt"
            if bail_marker.exists():
                bail_marker.unlink()
            print(
                f"[runner] resuming {self.cfg.family_name} at round "
                f"{start_round}/{n_rounds} (rounds 0..{start_round - 1} loaded "
                f"from disk)"
            )

        pending: list[WorkerPatch] = []
        bailed = False
        for r in range(start_round, n_rounds):
            is_last = (r == n_rounds - 1)
            record = await self._run_one_round(r, shards, is_last, pending)
            result.rounds.append(record)
            if record.merged is not None:
                pending = []   # flushed at sync
            # else: `pending` keeps growing across rounds until next sync

            # Bail-out: if any worker's trial in this round was killed mid-run
            # (timeout / first-call 429 / no result line) we stop the family
            # here. The downstream redo loop in queue_experiments.sh will pick
            # the family up by scanning per-family norun and rerun the WHOLE
            # family from scratch with the corrected timeout/proxy config.
            # Continuing to spend rounds on a family that's already going to
            # be re-done is pure waste of compute.
            if self._round_has_killed_trial(record):
                bailed = True
                self.run_dir.joinpath("BAILED_OUT.txt").write_text(
                    f"Bailed out after round {r} due to killed trial(s).\n"
                    f"exceptions: {record.exceptions}\n"
                    f"rewards: {record.rewards}\n",
                    encoding="utf-8",
                )
                break

        # In unshared mode each worker keeps its own library; the shared dir
        # is never updated. Reflect that: final_skill_dir is a per-worker map.
        if self.cfg.merger_mode == "unshared":
            result.final_skill_dir = dict(self._worker_skill_dirs)
        else:
            result.final_skill_dir = self.shared_skills_dir
        result.elapsed_sec = time.time() - t0
        self._write_family_summary(result, bailed=bailed)
        return result

    def _discover_completed_rounds(self, n_rounds: int) -> int:
        """Return the index of the first round that has NOT been completed
        on disk. A round is "completed" iff round_NNN/round_summary.json
        exists. Resume picks up from this index.

        We stop at the first gap — if round_NNN is missing but round_NNN+1
        exists, we still resume at N (so the later partial round gets
        recomputed). This is conservative: never silently skip a hole.
        """
        for r in range(n_rounds):
            rs_path = self.run_dir / f"round_{r:03d}" / "round_summary.json"
            if not rs_path.is_file():
                return r
        return n_rounds  # all rounds already done

    def _load_round_record(self, round_idx: int) -> RoundRecord | None:
        """Reconstruct a RoundRecord from round_NNN/round_summary.json so
        the resumed run's final family_summary contains the prior rounds.

        The reconstructed record has empty patches/exceptions (we don't
        need them post-completion) and merged=None — sync flag derives
        from `synced` in the summary, but the only callers care about
        rewards/assignments/cost which we DO preserve.
        """
        rs_path = self.run_dir / f"round_{round_idx:03d}" / "round_summary.json"
        if not rs_path.is_file():
            return None
        try:
            data = json.loads(rs_path.read_text(encoding="utf-8"))
        except Exception:
            return None
        # MergedPatch placeholder so reward + cost survive into family_summary.
        merged_stub = None
        if data.get("synced"):
            merged_stub = MergedPatch(
                summary="[restored from prior run]",
                cost_usd=float(data.get("merger_cost_usd", 0.0)),
            )
        return RoundRecord(
            round_idx=data.get("round_idx", round_idx),
            assignments=[tuple(a) for a in data.get("assignments", [])],
            rewards={k: v for k, v in data.get("rewards", {}).items()},
            exceptions={k: None for k in data.get("rewards", {})},
            patches=[],
            merged=merged_stub,
            elapsed_sec=float(data.get("elapsed_sec", 0.0)),
        )

    def _round_has_killed_trial(self, record: RoundRecord) -> bool:
        """True iff any worker's trial in this round counts as a 'norun'.

        Norun = the agent didn't reach a clean stream-json final 'result' line
        in claude-code.txt. That covers:
          - first-call 429 death (claude-code's 10 retries eaten by upstream)
          - mid-conversation timeout (AgentTimeoutError after agent_timeout_sec)
          - container crash / OOM / process killed
          - any other case where the trial was cut off mid-flight
          - **upstream gateway quota exhausted** (kicked in late 2026; the
            CLI's 10 retries all return 4xx/5xx, then the CLI emits a fake
            'success' result containing the error text — reward looks numeric
            but library state is poisoned)
        Either way the data is bad and the family should be re-done from scratch
        with a fresh container — no point spending more rounds on it.
        """
        # Per-user policy ("kimi 死了就死了，继续 merge"): only bail if ALL
        # workers failed (round produced nothing usable). Single-worker
        # failures yield empty patches that the merger handles gracefully.
        # The cell for the failed worker will still be dropped at table
        # time by the per-worker rule in compute_fed.
        n_workers = len(record.rewards) if record.rewards else 0
        n_exc = sum(1 for ex in record.exceptions.values() if ex)
        n_disk_exc = 0
        if not self.dry_run:
            n_disk_exc = self._count_trial_exceptions(record)
        n_failed = max(n_exc, n_disk_exc)
        # Bail only if ALL workers failed -- otherwise let federation continue
        if n_workers > 0 and n_failed >= n_workers:
            return True
        # Gateway quota / auth check stays strict: silent quota corruption
        # poisons the library across all workers (workers look "ok" but emit
        # garbage patches), so any quota marker still bails.
        if not self.dry_run and self._round_has_quota_error(record):
            return True
        # If every worker produced a numeric reward, the trial reached the
        # verifier — definitely not killed. Skip the agent-log file scan
        # (which is claude-code-specific and false-positives for qwen-code /
        # kimi-cli whose stream-json filenames + success markers differ).
        if all(r is not None for r in record.rewards.values()):
            return False
        # File-based check is real-mode only; dry-run produces a different
        # trial dir layout (no claude-code.txt) and would false-positive.
        if self.dry_run:
            return False
        # Scan trial dirs for missing success line in the agent's stream-json
        # log. Recognised log filenames + success markers per agent CLI:
        _AGENT_LOG_MARKERS = (
            ("claude-code.txt", '"type":"result"'),
            ("kimi-cli.txt", '"id":"1","result":{"status":"finished"'),
            # qwen-code.txt has no reliable structured success marker (output
            # is free-form LLM text). If a worker uses qwen-code we rely on
            # the reward-presence fast-path above; a missing reward + missing
            # exception + qwen log present is treated as "log exists, assume
            # ran" — we don't second-guess.
            ("qwen-code.txt", None),
        )
        round_dir = self.run_dir / f"round_{record.round_idx:03d}"
        if not round_dir.exists():
            return False
        for w_dir in round_dir.iterdir():
            if not w_dir.is_dir() or not w_dir.name.startswith("worker_"):
                continue
            for trial_dir in w_dir.iterdir():
                if not trial_dir.is_dir() or "__" not in trial_dir.name:
                    continue
                agent_dir = trial_dir / "agent"
                # Locate whichever agent log file is present for this trial.
                log_file = None
                marker = None
                for fname, mark in _AGENT_LOG_MARKERS:
                    p = agent_dir / fname
                    if p.exists():
                        log_file = p
                        marker = mark
                        break
                if log_file is None:
                    return True   # no agent log at all = killed before start
                if marker is None:
                    continue  # qwen-code: trust file existence
                try:
                    text = log_file.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue
                if marker not in text:
                    return True
        return False

    # Upstream gateway quota / auth markers. When a CLI exhausts its 10
    # internal retries on these, it emits a fake-success result containing
    # the error string and reward becomes 0 — but the library state is
    # invalid because the agent never produced real work.
    _QUOTA_MARKERS = (
        "EXHAUSTED",                       # lkeap 403 "Plan packet is disabled: EXHAUSTED"
        "token plan quota exhausted",      # lkeap 500
        "quota exhausted",                 # generic
        "Plan packet is disabled",         # lkeap 403
        "401 Invalid API-key",             # token-plan / dashscope 401
        "401 Unauthorized",                # any gateway 401
        "authentication_failed",           # claude-code stream-json
        "insufficient_quota",              # OpenAI-protocol
        "insufficient balance",            # generic balance error
    )

    def _try_load_existing_trial(self, w_dir: Path, worker, task):
        """Per-worker resume: if w_dir holds a clean prior trial (result.json
        has a numeric reward and exception_info is null), reconstruct a
        WorkerTrialResult from disk so we can skip relaunching this worker.
        Returns None if disk state is missing, partial, or marked failed."""
        if not w_dir.exists():
            return None
        trial_dir = next(
            (p for p in w_dir.iterdir() if p.is_dir() and "__" in p.name),
            None,
        )
        if trial_dir is None:
            return None
        rj = trial_dir / "result.json"
        if not rj.exists():
            return None
        try:
            d = json.loads(rj.read_text())
        except Exception:
            return None
        if d.get("exception_info"):
            return None
        rwd = d.get("verifier_result", {}).get("rewards", {}).get("reward")
        if rwd is None:
            return None
        from skillfl.skillflow_adapter.harbor_bridge import WorkerTrialResult
        return WorkerTrialResult(
            worker_id=worker.id,
            task_name=task.name,
            reward=float(rwd),
            verifier_passed=(rwd >= 1.0),
            trial_dir=trial_dir,
            exception_type=None,
            exception_message=None,
            extra={"trial_name": trial_dir.name, "reused_from_disk": True},
        )

    def _count_trial_exceptions(self, record: RoundRecord) -> int:
        """Count workers in this round whose trial result.json carries an
        exception_info. Used by the bail policy: per-user, single-worker
        failures continue federation, only an all-worker failure bails."""
        round_dir = self.run_dir / f"round_{record.round_idx:03d}"
        if not round_dir.exists():
            return 0
        n = 0
        for w_dir in round_dir.iterdir():
            if not w_dir.is_dir() or not w_dir.name.startswith("worker_"):
                continue
            saw_exc = False
            for trial_dir in w_dir.iterdir():
                if not trial_dir.is_dir() or "__" not in trial_dir.name:
                    continue
                rj = trial_dir / "result.json"
                if not rj.exists():
                    continue
                try:
                    d = json.loads(rj.read_text())
                except Exception:
                    continue
                e = d.get("exception_info")
                if e and e.get("exception_type"):
                    saw_exc = True
                    break
            if saw_exc:
                n += 1
        return n

    def _round_has_trial_exception_info(self, record: RoundRecord) -> bool:
        """Defensive: walk this round's trial result.json files and return
        True if any has exception_info set. Catches the case where harbor
        returns exception_type=None at the subprocess boundary even though
        the per-trial result.json itself records the exception (observed for
        AgentTimeoutError on kimi-cli)."""
        round_dir = self.run_dir / f"round_{record.round_idx:03d}"
        if not round_dir.exists():
            return False
        for w_dir in round_dir.iterdir():
            if not w_dir.is_dir() or not w_dir.name.startswith("worker_"):
                continue
            for trial_dir in w_dir.iterdir():
                if not trial_dir.is_dir() or "__" not in trial_dir.name:
                    continue
                rj = trial_dir / "result.json"
                if not rj.exists():
                    continue
                try:
                    d = json.loads(rj.read_text())
                except Exception:
                    continue
                e = d.get("exception_info")
                if e and e.get("exception_type"):
                    return True
        return False

    def _round_has_quota_error(self, record: RoundRecord) -> bool:
        """Scan every worker's agent log + the merger sandbox log for upstream
        quota / auth errors. If any are found, the round is poisoned even
        when rewards look numeric — bail-on-norun should fire."""
        round_dir = self.run_dir / f"round_{record.round_idx:03d}"
        if not round_dir.exists():
            return False
        # Worker agent logs
        for w_dir in round_dir.iterdir():
            if not w_dir.is_dir() or not w_dir.name.startswith("worker_"):
                continue
            for trial_dir in w_dir.iterdir():
                if not trial_dir.is_dir() or "__" not in trial_dir.name:
                    continue
                agent_dir = trial_dir / "agent"
                for fname in ("claude-code.txt", "kimi-cli.txt", "qwen-code.txt", "trajectory.json"):
                    p = agent_dir / fname
                    if not p.exists():
                        continue
                    try:
                        text = p.read_text(encoding="utf-8", errors="replace")
                    except Exception:
                        continue
                    if any(m in text for m in self._QUOTA_MARKERS):
                        return True
        # Merger sandbox claude-code.txt files (one per worker target +
        # optional .task_update). Same markers apply.
        merge_root = round_dir / "cloud_skill_merge_sandboxes"
        if merge_root.exists():
            for sandbox_log in merge_root.rglob("claude-code.txt"):
                try:
                    text = sandbox_log.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue
                if any(m in text for m in self._QUOTA_MARKERS):
                    return True
        return False

    # -----------------------------------------------------------------------
    # Per-round
    # -----------------------------------------------------------------------

    async def _run_one_round(
        self,
        round_idx: int,
        shards: list[list[Path]],
        is_last_round: bool,
        pending_patches: list[WorkerPatch],
    ) -> RoundRecord:
        t0 = time.time()
        round_dir = self.run_dir / f"round_{round_idx:03d}"
        round_dir.mkdir(parents=True, exist_ok=True)

        # Build (worker, task) assignments for this round. Workers whose shard
        # is shorter than round_idx+1 sit this round out.
        assignments: list[tuple[int, Path]] = [
            (i, shards[i][round_idx])
            for i in range(self.cfg.n_workers)
            if round_idx < len(shards[i])
        ]

        # Snapshot skill dir BEFORE launching trials. We keep TWO parallel
        # snapshots:
        #   - dict[rel → content] (text-only, for patcher and merger)
        #   - on-disk copytree at <round_dir>/.snapshots/<wid>/ (preserves
        #     binaries; runner-side reset uses this so assets/data.bin etc.
        #     survive sync rounds)
        # In isolated mode each worker gets its own snapshot pair; otherwise
        # all workers share one snapshot of the master dir.
        snap_root = round_dir / ".snapshots"
        snap_root.mkdir(parents=True, exist_ok=True)
        pre_snapshot_dirs: dict[int, Path] = {}
        if self.cfg.isolated_worker_skills:
            pre_snapshots: dict[int, dict[str, str]] = {}
            for (i, _t) in assignments:
                wid = self.cfg.workers[i].id
                src = self._worker_skill_dirs[wid]
                pre_snapshots[i] = self._snapshot_dir(src)
                pre_snapshot_dirs[i] = self._copy_dir_to_snapshot(src, snap_root / wid)
        else:
            shared_src = self.shared_skills_dir
            shared_snapshot = self._snapshot_dir(shared_src)
            shared_snap_dir = self._copy_dir_to_snapshot(shared_src, snap_root / "_shared")
            pre_snapshots = {i: shared_snapshot for (i, _t) in assignments}
            pre_snapshot_dirs = {i: shared_snap_dir for (i, _t) in assignments}

        # Launch M trials concurrently. In isolated mode each trial bind-mounts
        # the worker's private dir; otherwise all share the master dir.
        def _trial_skill_dir(i: int) -> Path:
            if self.cfg.isolated_worker_skills:
                return self._worker_skill_dirs[self.cfg.workers[i].id]
            return self.shared_skills_dir

        # Per-worker resume: if a worker's trial dir on disk already has a
        # clean result.json (numeric reward, no exception_info), reuse that
        # trial instead of re-launching. Saves API on partial-round redos
        # where only some workers (e.g., kimi-cli timeout) need to retry.
        reused: dict[int, "WorkerTrialResult"] = {}
        to_launch: list[tuple[int, Path]] = []
        for (i, task) in assignments:
            existing = self._try_load_existing_trial(
                round_dir / f"worker_{i}", self.cfg.workers[i], task,
            )
            if existing is not None:
                reused[i] = existing
            else:
                to_launch.append((i, task))
        if reused:
            print(f"[runner] round {round_idx}: reusing {len(reused)} workers' "
                  f"on-disk trials, re-launching {len(to_launch)}")
        # Harbor refuses to init a Job with an existing job_dir + different
        # config (FileExistsError). When re-launching a worker because its
        # prior trial was killed/incomplete, move the stale worker_<i> dir
        # aside so harbor creates a fresh one.
        for (i, _task) in to_launch:
            w_dir = round_dir / f"worker_{i}"
            if w_dir.exists():
                backup = w_dir.with_name(f".{w_dir.name}.prev-{int(time.time())}")
                try:
                    w_dir.rename(backup)
                except Exception:
                    shutil.rmtree(w_dir, ignore_errors=True)
        new_trials = await asyncio.gather(*[
            self._harbor.launch_trial(
                worker=self.cfg.workers[i],
                task_dir=task,
                shared_skills_dir=_trial_skill_dir(i),
                job_dir=round_dir / f"worker_{i}",
            )
            for (i, task) in to_launch
        ])
        new_trial_map = {i: t for (i, _), t in zip(to_launch, new_trials)}
        trials = [reused.get(i) or new_trial_map[i] for (i, _) in assignments]

        # Run patcher per-worker (sync; each is an independent LLM call).
        # If the patcher's LLM call fails (RateLimit / timeout / JSON parse)
        # we record the failure on `patcher_failures` so it propagates into
        # `record.exceptions` below — without that, an empty-patch silent fail
        # would let the run look successful while the library stops growing.
        this_round_patches: list[WorkerPatch] = []
        patcher_failures: dict[str, str] = {}
        for (i, _task), trial in zip(assignments, trials):
            worker = self.cfg.workers[i]
            try:
                wp = self._patcher.generate(
                    trial=trial,
                    skill_snapshot=pre_snapshots[i],
                )
            except Exception as e:
                patcher_failures[worker.id] = f"{type(e).__name__}: {e}"
                wp = WorkerPatch(
                    worker_id=worker.id,
                    reward=trial.reward or 0.0,
                    summary=f"[patcher failed: {type(e).__name__}: {e}]",
                )
            this_round_patches.append(wp)

            # Per-worker artifacts.
            w_dir = round_dir / f"worker_{i}"
            (w_dir / "patch.json").write_text(
                json.dumps({
                    "worker_id": wp.worker_id,
                    "reward": wp.reward,
                    "summary": wp.summary,
                    "upsert_paths": sorted(wp.upsert_files.keys()),
                    "delete_paths": wp.delete_paths,
                }, indent=2),
                encoding="utf-8",
            )

        pending_patches.extend(this_round_patches)

        # Sync?
        merged: MergedPatch | None = None
        if self.cfg.sync_schedule.should_sync(round_idx, is_last_round=is_last_round):
            if self.cfg.merger_mode == "unshared":
                # Personalized FL: cloud merger called once PER WORKER, for
                # all N workers — not just the active ones this round. With
                # round_robin / once_at_end and uneven shards some workers
                # may be inactive on the sync round; their libraries must
                # still receive merged peer patches that accumulated in
                # pending_patches, otherwise federation drops the contribution.
                #
                # Active-worker pre_snapshots are stale for inactive workers
                # (they didn't run this round), so we re-snapshot all worker
                # dirs from disk — that gives the inactive worker's library
                # in its current post-last-sync state.
                # Re-snapshot all workers at sync time. dict for merger
                # (text-only); disk copy for reset (preserves binaries).
                sync_snap_root = round_dir / ".sync_snapshots"
                sync_snap_root.mkdir(parents=True, exist_ok=True)
                all_libraries: dict[str, dict[str, str]] = {}
                all_library_dirs: dict[str, Path] = {}
                for w in self.cfg.workers:
                    src = self._worker_skill_dirs[w.id]
                    all_libraries[w.id] = self._snapshot_dir(src)
                    all_library_dirs[w.id] = self._copy_dir_to_snapshot(
                        src, sync_snap_root / w.id
                    )
                merged_per_worker = self.cfg.merger.merge_per_worker(
                    pending_patches,
                    libraries=all_libraries,
                    round_idx=round_idx,
                    round_dir=round_dir,
                )
                # Reset every worker's dir to its sync snapshot (preserves
                # binaries via copytree), then apply each worker's MergedPatch.
                self._reset_workers_to_disk_snapshots(all_library_dirs)
                self._apply_merged_per_worker(merged_per_worker)
                # Audit: per-worker merged_patch.json + per-round summary.
                merged_index: dict[str, dict] = {}
                for wid, mp in merged_per_worker.items():
                    w_dir = round_dir / f"merged_for_{wid}"
                    w_dir.mkdir(parents=True, exist_ok=True)
                    (w_dir / "merged_patch.json").write_text(
                        json.dumps({
                            "worker_id": wid,
                            "summary": mp.summary,
                            "upsert_paths": sorted(mp.upsert_files.keys()),
                            "delete_paths": mp.delete_paths,
                            "provenance": {k: list(v) for k, v in mp.provenance.items()},
                            "conflicts": mp.conflicts,
                            "cost_usd": mp.cost_usd,
                        }, indent=2),
                        encoding="utf-8",
                    )
                    merged_index[wid] = {
                        "n_upsert": len(mp.upsert_files),
                        "n_delete": len(mp.delete_paths),
                        "n_conflicts": len(mp.conflicts),
                        "cost_usd": mp.cost_usd,
                    }
                round_merger_cost = sum(mp.cost_usd for mp in merged_per_worker.values())
                (round_dir / "merged_patch.json").write_text(
                    json.dumps({
                        "merger_mode": "unshared",
                        "n_pending_patches_merged": len(pending_patches),
                        "per_worker": merged_index,
                        "merger_cost_usd": round_merger_cost,
                    }, indent=2),
                    encoding="utf-8",
                )
                # Synthetic union for round-level summary fields.
                from skillfl.skillflow_adapter.merge import MergedPatch as _MP
                merged = _MP(
                    upsert_files={p: "" for mp in merged_per_worker.values()
                                  for p in mp.upsert_files},
                    delete_paths=sorted({p for mp in merged_per_worker.values()
                                         for p in mp.delete_paths}),
                    summary="(unshared) " + "; ".join(
                        f"{wid}:{mp.summary[:80]}" for wid, mp in merged_per_worker.items()
                    ),
                    cost_usd=round_merger_cost,
                )
            else:
                # Shared mode: snapshot the current shared library so the
                # merger can spot cross-round duplicates.
                existing_library = self._snapshot_skill_dir()
                merged = self.cfg.merger.merge(
                    pending_patches, existing_library=existing_library
                )
                self._apply_merged(merged)
                (round_dir / "merged_patch.json").write_text(
                    json.dumps({
                        "summary": merged.summary,
                        "upsert_paths": sorted(merged.upsert_files.keys()),
                        "delete_paths": merged.delete_paths,
                        "provenance": {k: list(v) for k, v in merged.provenance.items()},
                        "conflicts": merged.conflicts,
                        "n_pending_patches_merged": len(pending_patches),
                        "merger_cost_usd": merged.cost_usd,
                    }, indent=2),
                    encoding="utf-8",
                )

        # Round summary. Exceptions are the union of (a) trial-level exceptions
        # surfaced by harbor and (b) patcher-LLM failures — both should mark the
        # worker as failed for bail-on-norun purposes (silent empty patches were
        # masking dashscope rate-limit cascades in prior runs).
        round_exceptions = {self.cfg.workers[i].id: t.exception_type
                            for (i, _), t in zip(assignments, trials)}
        for wid, err in patcher_failures.items():
            if not round_exceptions.get(wid):
                round_exceptions[wid] = err
        record = RoundRecord(
            round_idx=round_idx,
            assignments=[(self.cfg.workers[i].id, shards[i][round_idx].name)
                         for (i, _t) in assignments],
            rewards={self.cfg.workers[i].id: t.reward for (i, _), t in zip(assignments, trials)},
            exceptions=round_exceptions,
            patches=this_round_patches,
            merged=merged,
            elapsed_sec=time.time() - t0,
        )
        (round_dir / "round_summary.json").write_text(
            json.dumps({
                "round_idx": record.round_idx,
                "assignments": record.assignments,
                "rewards": record.rewards,
                "exceptions": record.exceptions,
                "synced": merged is not None,
                "n_pending_after_round": (
                    0 if merged is not None else len(pending_patches)
                ),
                "elapsed_sec": record.elapsed_sec,
            }, indent=2),
            encoding="utf-8",
        )
        return record

    # -----------------------------------------------------------------------
    # Skill dir plumbing
    # -----------------------------------------------------------------------

    def _maybe_seed_template(self) -> None:
        """Copy template into shared_skills_dir iff it's empty."""
        if self.cfg.project_template_dir is None:
            return
        if any(self.shared_skills_dir.iterdir()):
            return
        tpl = self.cfg.project_template_dir
        if not tpl.exists():
            return
        for item in tpl.iterdir():
            dst = self.shared_skills_dir / item.name
            if item.is_dir():
                shutil.copytree(item, dst)
            else:
                shutil.copy2(item, dst)

    def _snapshot_skill_dir(self) -> dict[str, str]:
        return self._snapshot_dir(self.shared_skills_dir)

    @staticmethod
    def _snapshot_dir(base: Path) -> dict[str, str]:
        """Return {rel_path: content_str, '__skill_dir__': absolute_path}."""
        out: dict[str, str] = {}
        for p in base.rglob("*"):
            if p.is_file():
                try:
                    out[str(p.relative_to(base))] = p.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    # Binary files: skip content, just record existence.
                    out[str(p.relative_to(base))] = "<binary>"
        # Meta key so PatcherBridge can re-snapshot from disk if needed.
        out["__skill_dir__"] = str(base)
        return out

    @staticmethod
    def _copy_dir_to_snapshot(src: Path, dst: Path) -> Path:
        """Copytree `src` to `dst`, preserving everything (incl. binaries).
        Existing `dst` is wiped first. Returns dst for chaining."""
        if dst.exists():
            shutil.rmtree(dst)
        if src.exists():
            shutil.copytree(src, dst)
        else:
            dst.mkdir(parents=True, exist_ok=True)
        return dst

    def _reset_workers_to_disk_snapshots(
        self,
        snap_dirs: dict[str, Path],
    ) -> None:
        """Wipe each worker's dir and copytree-restore from a real on-disk
        snapshot. Preserves binary files (assets, etc.) that the dict-based
        snapshot can't represent."""
        for wid, snap_dir in snap_dirs.items():
            base = self._worker_skill_dirs[wid]
            if base.exists():
                shutil.rmtree(base)
            base.mkdir(parents=True, exist_ok=True)
            if snap_dir.exists():
                # copytree refuses to merge into an existing dir; copy contents.
                for item in snap_dir.iterdir():
                    target = base / item.name
                    if item.is_dir():
                        shutil.copytree(item, target)
                    else:
                        shutil.copy2(item, target)

    def _apply_merged_per_worker(
        self, merged_per_worker: dict[str, MergedPatch]
    ) -> None:
        """Apply each per-worker MergedPatch to that worker's private dir.

        No shared dir update, no cross-worker sync — each worker's library
        evolves independently from this round forward.

        All paths run through safe_rel_path before touching disk; an LLM
        producing a `../../etc/passwd` upsert can't escape the worker dir.
        """
        for wid, merged in merged_per_worker.items():
            base = self._worker_skill_dirs[wid]
            for rel in merged.delete_paths:
                safe = safe_rel_path(rel)
                if safe is None:
                    merged.conflicts.append(f"__unsafe_delete_path__:{rel}")
                    continue
                target = base / safe
                if target.exists():
                    if target.is_dir():
                        shutil.rmtree(target)
                    else:
                        target.unlink()
            for rel, content in merged.upsert_files.items():
                if content == "<binary>":
                    merged.conflicts.append(
                        f"__llm_returned_binary_placeholder__:{rel}"
                    )
                    continue
                safe = safe_rel_path(rel)
                if safe is None:
                    merged.conflicts.append(f"__unsafe_upsert_path__:{rel}")
                    continue
                target = base / safe
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")

    def _apply_merged(self, merged: MergedPatch) -> None:
        """Write upserts, remove deletes. Deletes processed first so a deleted
        path that's simultaneously upserted (shouldn't happen post-merge, but
        defensive) ends up as upserted. In isolated mode also re-syncs each
        worker's private dir to the merged master state so every worker
        starts the next task aligned.

        Paths sanitized via safe_rel_path before touching disk.
        """
        base = self.shared_skills_dir
        for rel in merged.delete_paths:
            safe = safe_rel_path(rel)
            if safe is None:
                merged.conflicts.append(f"__unsafe_delete_path__:{rel}")
                continue
            target = base / safe
            if target.exists():
                if target.is_dir():
                    shutil.rmtree(target)
                else:
                    target.unlink()
        for rel, content in merged.upsert_files.items():
            # Defensive: snapshots represent non-utf8 files as the literal
            # string "<binary>". If an LLM merger echoes that back into an
            # upsert, we must NOT write it (it would clobber the real binary).
            if content == "<binary>":
                merged.conflicts.append(f"__llm_returned_binary_placeholder__:{rel}")
                continue
            safe = safe_rel_path(rel)
            if safe is None:
                merged.conflicts.append(f"__unsafe_upsert_path__:{rel}")
                continue
            target = base / safe
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        if self.cfg.isolated_worker_skills:
            self._sync_master_to_workers()

    def _sync_master_to_workers(self) -> None:
        """Mirror the master shared_skills dir into every worker's private dir.

        Used after merges (and once at init for the round-0 seed). The
        worker dirs are wiped and re-populated from master so they reflect
        exactly the merged state with no stale artifacts from a worker's
        previous trial.
        """
        for worker_dir in self._worker_skill_dirs.values():
            # Wipe + recreate so we don't carry over stale files that the
            # merger explicitly deleted (a merger delete won't show up as a
            # file to copy).
            if worker_dir.exists():
                shutil.rmtree(worker_dir)
            worker_dir.mkdir(parents=True, exist_ok=True)
            for item in self.shared_skills_dir.iterdir():
                dst = worker_dir / item.name
                if item.is_dir():
                    shutil.copytree(item, dst)
                else:
                    shutil.copy2(item, dst)

    # -----------------------------------------------------------------------
    # Bookkeeping
    # -----------------------------------------------------------------------

    def _write_shard_manifest(self, shards: list[list[Path]]) -> None:
        (self.run_dir / "shard_manifest.json").write_text(
            json.dumps({
                "run_id": self.cfg.run_id,
                "family_name": self.cfg.family_name,
                "n_workers": self.cfg.n_workers,
                "partitioner": repr(self.cfg.partitioner),
                "sync_schedule": repr(self.cfg.sync_schedule),
                "merger": repr(self.cfg.merger),
                "workers": [
                    {
                        "id": w.id,
                        "agent_name": w.agent_name,
                        "agent_import_path": w.agent_import_path,
                        "agent_model": w.agent_model,
                    }
                    for w in self.cfg.workers
                ],
                "shards": {
                    self.cfg.workers[i].id: [p.name for p in s]
                    for i, s in enumerate(shards)
                },
            }, indent=2),
            encoding="utf-8",
        )

    def _write_family_summary(self, result: FamilyResult, *, bailed: bool = False) -> None:
        per_round = [
            {
                "round_idx": r.round_idx,
                "assignments": r.assignments,
                "rewards": r.rewards,
                "synced": r.merged is not None,
                "n_conflicts": len(r.merged.conflicts) if r.merged else 0,
                "n_ops_in_merged": (
                    (len(r.merged.upsert_files) + len(r.merged.delete_paths))
                    if r.merged else 0
                ),
                "merger_cost_usd": r.merged.cost_usd if r.merged else 0.0,
                "elapsed_sec": r.elapsed_sec,
            }
            for r in result.rounds
        ]
        all_rewards = [v for r in result.rounds for v in r.rewards.values() if v is not None]
        total_merger_cost = sum(
            (r.merged.cost_usd if r.merged else 0.0) for r in result.rounds
        )
        (self.run_dir / "family_summary.json").write_text(
            json.dumps({
                "family_name": result.family_name,
                "n_workers": self.cfg.n_workers,
                "n_rounds": len(result.rounds),
                "elapsed_sec": result.elapsed_sec,
                "bailed_out_on_timeout": bailed,
                "reward_stats": {
                    "n": len(all_rewards),
                    "mean": sum(all_rewards) / len(all_rewards) if all_rewards else None,
                    "max": max(all_rewards) if all_rewards else None,
                    "min": min(all_rewards) if all_rewards else None,
                    "n_passed": sum(1 for r in all_rewards if r >= 1.0),
                },
                "merger_cost_total_usd": total_merger_cost,
                "rounds": per_round,
                "final_skill_dir": (
                    {wid: str(p) for wid, p in result.final_skill_dir.items()}
                    if isinstance(result.final_skill_dir, dict)
                    else str(result.final_skill_dir) if result.final_skill_dir else None
                ),
            }, indent=2),
            encoding="utf-8",
        )

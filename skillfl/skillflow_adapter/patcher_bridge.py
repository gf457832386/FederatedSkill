"""Generate one worker's patch from its trial.

Two implementations:
- `PatcherBridge`: wraps SkillFlow's `SkillPatchEvolver` — same LLM call /
  prompt / output format as the baseline iterative runner, so our patches
  are drawn from the same distribution as baseline ones. This is essential
  for a clean comparison.
- `DryRunPatcherBridge`: deterministic-fake. Produces a seeded upsert patch
  per (worker, task) so the runner + merge can be exercised without API.
"""

from __future__ import annotations

import json
import random
import re
import sys
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from skillfl.skillflow_adapter.merge import WorkerPatch
from skillfl.skillflow_adapter.worker_trial import WorkerTrialResult


# Inner-pytest progress-bar regex (e.g. ".FFFFFF [100%]" → 1 pass / 6 fail).
# Same pattern used by scripts/extract_paper_metrics.py.
_INNER_DOT_RE = re.compile(r"([.F]{2,})\s*\[100%\]")


def _compute_soft_reward(trial_dir: Path, hard_reward: float | None) -> float:
    """Return the trial's soft reward (sub-test pass rate, 0.0-1.0).

    If the verifier's outer test passed, soft = 1.0. Otherwise we parse the
    inner pytest progress bar from `verifier/test-stdout.txt` and return the
    fraction of inner sub-tests that passed. If the inner stdout is missing
    or unparseable, we fall back to the hard reward (so soft=0.0 == hard=0.0
    in the worst case — never *more* informative than hard, never less).
    """
    if hard_reward is not None and hard_reward >= 1.0:
        return 1.0
    stdout_path = trial_dir / "verifier" / "test-stdout.txt"
    if not stdout_path.exists():
        return float(hard_reward or 0.0)
    try:
        text = stdout_path.read_text(errors="replace")
    except OSError:
        return float(hard_reward or 0.0)
    m = _INNER_DOT_RE.search(text)
    if not m:
        return float(hard_reward or 0.0)
    run = m.group(1)
    p = run.count(".")
    f = run.count("F")
    if p + f == 0:
        return float(hard_reward or 0.0)
    return p / (p + f)


def _resolve_patch_temperature(model_name: str, requested: float) -> float:
    """Match upstream's `resolve_patch_temperature`: Moonshot/Kimi rejects
    sampling temperatures < 1.0, so bump to 1.0 for that provider regardless
    of what the config requested. Substring match (not prefix) because the
    config-side name may be plain `kimi-k2.5` or `moonshot/kimi-k2.5`."""
    lname = (model_name or "").strip().lower()
    if "moonshot" in lname or "kimi" in lname:
        return 1.0
    return requested


def _normalize_for_litellm(model_name: str, api_base: str | None) -> str:
    """LiteLLM needs `<provider>/<model>`. Infer provider from api_base."""
    name = (model_name or "").strip()
    if not name or "/" in name:
        return name
    base = (api_base or "").rstrip("/").lower()
    lname = name.lower()
    if "/anthropic" in base or lname.startswith(("claude", "vertex.claude")):
        return f"anthropic/{name}"
    if "/google/" in base or lname.startswith("gemini"):
        return f"gemini/{name}"
    if "/openai" in base:
        return f"openai/{name}"
    # Default to openai-compatible; tune if another provider lands here.
    return f"openai/{name}"


class PatcherLLMError(RuntimeError):
    """The patcher's LLM call failed (RateLimit / timeout / JSON parse / no LiteLLM).

    Raised when SkillFlow's patcher returns a SkillPatchResult whose `summary`
    starts with a known LLM-failure marker. Previously these were swallowed —
    runner wrote an empty patch and continued, which silently degraded the
    library while making the run look successful. Surface them explicitly so
    the runner can record the exception (and bail-on-norun if configured).
    """


class PatcherBridgeBase(ABC):
    """Turn one WorkerTrialResult + current skill snapshot into a WorkerPatch."""

    @abstractmethod
    def generate(
        self,
        *,
        trial: WorkerTrialResult,
        skill_snapshot: dict[str, str],
    ) -> WorkerPatch:
        ...


# ---------------------------------------------------------------------------
# Real mode
# ---------------------------------------------------------------------------


class PatcherBridge(PatcherBridgeBase):
    """Uses SkillFlow's patcher (same prompt/LLM as the baseline).

    Each worker's trajectory is distilled by **that worker's own model**.
    Upstream SkillFlow is single-agent and hard-codes `first_agent.model_name`
    as the patcher; in our heterogeneous M-worker setup we route per-worker
    so the patch reflects the actual worker model's style. Cross-model
    distillation drift (e.g. Claude summarizing GLM's trajectory) hurts the
    merged library, so this routing is mandatory — there is no shared-patcher
    fallback.

    Per-worker config dict shape:
      {worker_id: {model_name, api_base, api_key, temperature?, max_tokens?,
                   extra_headers?}, ...}
    """

    def __init__(
        self,
        *,
        skillflow_root: Path,
        worker_llm_configs: dict[str, dict[str, Any]],
        extra_headers: dict[str, str] | None = None,
        # Defaults match upstream `iterative_shared_skills_runner.py` JobConfig.
        max_steps: int = 20,
        max_obs_chars: int = 3000,
    ) -> None:
        if not worker_llm_configs:
            raise ValueError(
                "PatcherBridge requires worker_llm_configs (per-worker routing "
                "is mandatory — patcher is always the worker's own model)"
            )
        self.skillflow_root = skillflow_root
        if str(skillflow_root) not in sys.path:
            sys.path.insert(0, str(skillflow_root))

        from libs.skill_evolution.patcher import (
            CompactionConfig,
            SkillPatchEvolver,
            TrajectoryCompactor,
        )
        self._SkillPatchEvolver = SkillPatchEvolver

        self._compactor = TrajectoryCompactor(
            CompactionConfig(max_steps=max_steps, max_obs_chars=max_obs_chars)
        )
        self._extra_headers = dict(extra_headers or {})

        self._evolvers: dict[str, Any] = {}
        for wid, cfg in worker_llm_configs.items():
            mname = cfg.get("model_name")
            if not mname:
                raise ValueError(
                    f"worker_llm_configs[{wid}] missing model_name"
                )
            api_b = cfg.get("api_base")
            api_k = cfg.get("api_key")
            hdrs = {**self._extra_headers, **(cfg.get("extra_headers") or {})}
            norm = _normalize_for_litellm(mname, api_b)
            requested_temp = cfg.get("temperature", 0.2)
            temp = _resolve_patch_temperature(norm, requested_temp)
            self._evolvers[wid] = SkillPatchEvolver(
                model_name=norm,
                api_base=api_b,
                api_key=api_k,
                temperature=temp,
                max_tokens=cfg.get("max_tokens", 8192),
                extra_headers=hdrs,
            )

    def _evolver_for(self, worker_id: str):
        evo = self._evolvers.get(worker_id)
        if evo is None:
            raise KeyError(
                f"no patcher LLM configured for worker_id={worker_id!r}; "
                f"known: {sorted(self._evolvers)}"
            )
        return evo

    def generate(
        self,
        *,
        trial: WorkerTrialResult,
        skill_snapshot: dict[str, str],
    ) -> WorkerPatch:
        from libs.skill_evolution.patcher import (
            SkillSnapshotter,
            ensure_standard_trajectory,
        )

        trial_dir = trial.trial_dir
        trajectory_path = ensure_standard_trajectory(trial_dir)
        if trajectory_path is None:
            # Agent produced no usable trajectory — worker contributes nothing.
            return WorkerPatch(
                worker_id=trial.worker_id,
                reward=trial.reward or 0.0,
                upsert_files={},
                delete_paths=[],
                summary="[skipped: trajectory not found]",
            )

        result_json_path = trial_dir / "result.json"
        trial_result_dict: dict[str, Any] | None = None
        if result_json_path.exists():
            trial_result_dict = json.loads(result_json_path.read_text(encoding="utf-8"))
        if trial_result_dict is not None and trial.reward is not None:
            trial_result_dict["reward"] = trial.reward

        verifier_ctr = None
        ctrf_path = trial_dir / "verifier" / "ctrf.json"
        if ctrf_path.exists():
            try:
                verifier_ctr = json.loads(ctrf_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                verifier_ctr = None

        outcome = self._compactor.extract_trial_outcome(
            trajectory_path=trajectory_path,
            trial_name=trial.extra.get("trial_name", trial_dir.name),
            task_name=trial.task_name,
            task_source="",
            trial_result=trial_result_dict,
            verifier_ctr=verifier_ctr,
        )
        outcome.reward = trial.reward
        outcome.verifier_passed = trial.verifier_passed

        # Snapshot passed from caller — but SkillFlow's API wants a
        # SkillSnapshotter-shaped object, not our raw dict. Re-snapshot from
        # disk (the caller has already ensured shared_skills_dir is at the
        # right state; SkillSnapshotter just reads from disk).
        snap = SkillSnapshotter.snapshot(self._snapshot_source_dir(skill_snapshot))
        evolver = self._evolver_for(trial.worker_id)
        sk_patch = evolver.generate_patch(snap, outcome)

        # SkillFlow's patcher swallows LLM call failures (RateLimit / timeout /
        # JSON parse) and returns a SkillPatchResult whose `summary` field
        # starts with "LLM call failed:" or "JSON parse failed:" — `upsert_files`
        # is empty. Silently returning that empty patch makes the run look
        # successful while the library quietly stops accumulating. Raise
        # explicitly so the runner records it as an exception (→ bail-on-norun).
        #
        # Exception: `max_tokens limit` failures are treated as structural
        # truncation, NOT infrastructure failures. patch_max_tokens=8192 is a
        # fixed baseline parameter across all experiments; if a particular
        # trajectory exceeds it, the library just doesn't grow that round —
        # comparable across baselines, so we tolerate the empty patch instead
        # of bailing the whole family.
        summary_str = sk_patch.summary or ""
        is_llm_fail = (
            summary_str.startswith("LLM call failed")
            or summary_str.startswith("JSON parse failed")
            or summary_str.startswith("LiteLLM not available")
        )
        is_max_tokens_trunc = "max_tokens limit" in summary_str or "max_tokens=" in summary_str
        if is_llm_fail and not is_max_tokens_trunc:
            raise PatcherLLMError(summary_str)

        # The merger sees `reward` as the worker's signal of "how well this
        # trial went". We pass the soft (sub-test pass rate) version when the
        # verifier output makes it computable — gives the merger a more
        # informative tiebreaker than 0/1, without telling it which specific
        # sub-tests failed.
        soft = _compute_soft_reward(trial_dir, trial.reward)
        return WorkerPatch(
            worker_id=trial.worker_id,
            reward=soft,
            upsert_files=dict(sk_patch.upsert_files or {}),
            delete_paths=list(sk_patch.delete_paths or []),
            summary=summary_str,
        )

    def _snapshot_source_dir(self, skill_snapshot: dict[str, str]) -> Path:
        # The runner snapshotted the skill dir for us, but SkillSnapshotter
        # re-reads from disk. The runner guarantees it hasn't modified the
        # shared dir between snapshot and this call — so it's safe to use the
        # live dir path embedded in the snapshot's meta key.
        src = skill_snapshot.get("__skill_dir__")
        if not src:
            raise RuntimeError(
                "PatcherBridge expected '__skill_dir__' meta key in snapshot. "
                "Runner's _snapshot_skill_dir must populate it."
            )
        return Path(src)


# ---------------------------------------------------------------------------
# Dry-run mode
# ---------------------------------------------------------------------------


class DryRunPatcherBridge(PatcherBridgeBase):
    """Produces a seeded fake patch per (worker, task).

    Behavior:
    - With probability 0.3, proposes an upsert at `skills/auto/<task>.md`
      containing a short marker string. This occasionally conflicts across
      workers (same path touched by multiple) so the merge logic gets
      exercised.
    - With probability 0.1, proposes a delete of a previously-created path.
    - Otherwise empty patch.
    """

    def __init__(self, seed: int = 0) -> None:
        self.seed = seed

    def generate(
        self,
        *,
        trial: WorkerTrialResult,
        skill_snapshot: dict[str, str],
    ) -> WorkerPatch:
        rng = random.Random(f"{self.seed}:{trial.worker_id}:{trial.task_name}")
        upsert: dict[str, str] = {}
        deletes: list[str] = []
        summary = f"[dry-run] {trial.worker_id} on {trial.task_name}"

        r = rng.random()
        if r < 0.3:
            # Occasionally all workers upsert the same path to trigger merge
            # conflicts. 20% of the time write to a "hot" shared path.
            if rng.random() < 0.2:
                path = "skills/auto/shared.md"
            else:
                path = f"skills/auto/{trial.task_name}.md"
            upsert[path] = (
                f"# auto skill from {trial.worker_id} on {trial.task_name}\n"
                f"reward={trial.reward}\n"
            )
        elif r < 0.4:
            # Delete any existing path in the snapshot (except meta).
            candidates = [k for k in skill_snapshot.keys() if not k.startswith("__")]
            if candidates:
                deletes.append(rng.choice(candidates))

        return WorkerPatch(
            worker_id=trial.worker_id,
            reward=trial.reward if trial.reward is not None else 0.0,
            upsert_files=upsert,
            delete_paths=deletes,
            summary=summary,
        )

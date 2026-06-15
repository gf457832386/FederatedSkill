"""Run one (worker, task) trial via harbor.

Two implementations:
- `HarborBridge`: real mode. Imports harbor + SkillFlow's SharedSkillsDockerEnvironment.
- `DryRunHarborBridge`: deterministic-fake. No docker, no harbor, no LLM.

Both expose the same async `launch_trial(...)` interface so the runner can
swap between them via one branch at construction time.

Design: one harbor `Job` per trial. The M workers per round run as M
concurrent Job instances (asyncio.gather at the runner level); harbor's
per-Job concurrency stays 1. This avoids having to teach harbor about
per-(worker, task) pairing, at the cost of minor per-Job metadata overhead.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import random
import sys
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from skillfl.harbor_runner import UserSpec
from skillfl.skillflow_adapter.worker_trial import WorkerTrialResult


class HarborBridgeBase(ABC):
    """Runs one (worker, task) trial, writes artifacts to trial_dir, returns result."""

    @abstractmethod
    async def launch_trial(
        self,
        *,
        worker: UserSpec,
        task_dir: Path,
        shared_skills_dir: Path,
        job_dir: Path,
    ) -> WorkerTrialResult:
        ...


# ---------------------------------------------------------------------------
# Real mode
# ---------------------------------------------------------------------------


class HarborBridge(HarborBridgeBase):
    """Wraps harbor's Python `Job` + SkillFlow's SharedSkillsDockerEnvironment.

    Harbor / SkillFlow paths are imported lazily so `dry-run` callers don't
    need those deps installed.
    """

    def __init__(
        self,
        *,
        family_name: str,
        skillflow_root: Path,
        project_template_dir: Path | None,
        force_shared_env: bool = True,
    ) -> None:
        self.family_name = family_name
        self.skillflow_root = skillflow_root
        self.project_template_dir = project_template_dir
        self.force_shared_env = force_shared_env

        # Make SkillFlow-benchmark importable.
        if str(skillflow_root) not in sys.path:
            sys.path.insert(0, str(skillflow_root))

    async def launch_trial(
        self,
        *,
        worker: UserSpec,
        task_dir: Path,
        shared_skills_dir: Path,
        job_dir: Path,
    ) -> WorkerTrialResult:
        """Run ONE harbor trial in a dedicated subprocess, retrying the whole
        trial when claude-code's own retry budget gets eaten by 429s.

        Rationale: two harbor.Job instances sharing one asyncio event loop
        wedge each other's agent-setup phase (reliably hits
        AgentSetupTimeoutError:360s). Isolating each trial in its own OS
        process gives it a fresh event loop + fresh harbor state, which is
        the only arrangement we've confirmed works.

        429-exhaustion retry: claude-code (the agent inside the container)
        caps its own LLM retries at 10 (~3 min budget). When dashscope's
        per-key concurrency quota stays exhausted longer than that, the
        trial finishes with `is_error: true, api_error_status: 429` and 0
        turns. We detect that pattern from the trial's claude-code.txt and
        re-run the WHOLE trial (fresh container, fresh agent conversation).
        Tunable via env:
          SKILLFL_AGENT_429_RETRIES (default 20)
          SKILLFL_AGENT_429_BASE_SLEEP (default 30)
          SKILLFL_AGENT_429_MAX_SLEEP (default 600)
        """
        job_dir.mkdir(parents=True, exist_ok=True)
        job_name = job_dir.name

        config_dict = self._build_job_config_dict(
            job_name=job_name,
            job_dir=job_dir,
            worker=worker,
            task_dir=task_dir,
            shared_skills_dir=shared_skills_dir,
        )
        cfg_path = job_dir / ".single_trial_config.json"
        result_path = job_dir / ".single_trial_result.json"
        cfg_path.write_text(json.dumps(config_dict), encoding="utf-8")

        max_429_retries = int(os.environ.get("SKILLFL_AGENT_429_RETRIES", "20"))
        base_sleep = int(os.environ.get("SKILLFL_AGENT_429_BASE_SLEEP", "30"))
        max_sleep = int(os.environ.get("SKILLFL_AGENT_429_MAX_SLEEP", "600"))

        attempt = 0
        proc = None
        stdout = stderr = b""
        while True:
            # cwd must be skillflow_root so `import libs.harbor_noinstall_agents`
            # resolves. PYTHONPATH inherits from parent.
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "skillfl.skillflow_adapter._single_trial",
                str(cfg_path), str(result_path),
                cwd=str(self.skillflow_root),
                env=os.environ.copy(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()

            if attempt >= max_429_retries:
                break  # give up on retry, fall through with whatever we have
            if not _trial_was_429_exhausted(result_path):
                break  # success or non-429 failure: don't retry
            attempt += 1
            sleep_s = min(base_sleep * (2 ** min(attempt - 1, 5)), max_sleep)
            print(
                f"[harbor_bridge] trial {worker.id}/{task_dir.name} hit "
                f"claude-code 429-exhaustion; retrying whole trial "
                f"(attempt {attempt}/{max_429_retries}, sleep {sleep_s}s)",
                file=sys.stderr, flush=True,
            )
            await asyncio.sleep(sleep_s)

        # Parse result written by the subprocess.
        if not result_path.exists():
            err_tail = (stderr or b"").decode("utf-8", errors="replace")[-800:]
            return WorkerTrialResult(
                worker_id=worker.id,
                task_name=task_dir.name,
                reward=None,
                verifier_passed=False,
                trial_dir=job_dir,
                exception_type="subprocess_died_no_result",
                exception_message=(
                    f"exit={proc.returncode}; stderr_tail={err_tail}"
                ),
                extra={"trial_name": None, "subprocess_exit": proc.returncode},
            )
        try:
            parsed = json.loads(result_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            return WorkerTrialResult(
                worker_id=worker.id,
                task_name=task_dir.name,
                reward=None,
                verifier_passed=False,
                trial_dir=job_dir,
                exception_type="subprocess_bad_result_json",
                exception_message=str(e),
                extra={"trial_name": None, "subprocess_exit": proc.returncode},
            )

        if parsed.get("status") != "ok":
            return WorkerTrialResult(
                worker_id=worker.id,
                task_name=task_dir.name,
                reward=parsed.get("reward"),
                verifier_passed=False,
                trial_dir=Path(parsed.get("trial_dir") or job_dir),
                exception_type=parsed.get("exception_type") or parsed.get("status"),
                exception_message=parsed.get("exception_message"),
                extra={"trial_name": parsed.get("trial_name"),
                       "subprocess_exit": proc.returncode,
                       "subprocess_status": parsed.get("status")},
            )

        reward = parsed.get("reward")
        trial_dir_s = parsed.get("trial_dir")
        return WorkerTrialResult(
            worker_id=worker.id,
            task_name=task_dir.name,
            reward=reward,
            verifier_passed=(reward is not None and reward >= 1.0),
            trial_dir=Path(trial_dir_s) if trial_dir_s else job_dir,
            exception_type=parsed.get("exception_type"),
            exception_message=parsed.get("exception_message"),
            extra={"trial_name": parsed.get("trial_name"),
                   "subprocess_exit": proc.returncode,
                   "subprocess_status": "ok"},
        )

    def _build_job_config_dict(
        self,
        *,
        job_name: str,
        job_dir: Path,
        worker: UserSpec,
        task_dir: Path,
        shared_skills_dir: Path,
    ) -> dict[str, Any]:
        """Build the harbor JobConfig dict for a 1-agent, 1-task job."""
        env_block: dict[str, Any] = {
            "allow_internet": True,
            # Build each task's container image from its environment/Dockerfile
            # on first use instead of pulling a pre-built tag from a registry.
            # The per-task images we used during paper experiments live only on
            # the original training machine's local registry (localhost/harbor-
            # prebuilt:task-*) — they are NOT public. With force_build=True
            # harbor uses docker-compose-build.yaml which has `pull_policy: build`
            # and points at ${CONTEXT_DIR}=test_tasks/<family>/<task>/environment/
            # for the per-task Dockerfile. Docker's build cache makes the second
            # and subsequent uses of the same task image effectively free.
            "force_build": True,
            "delete": False,
        }
        if self.force_shared_env:
            env_block["import_path"] = (
                "libs.terminus_env.environments.shared_skills_env:"
                "SharedSkillsDockerEnvironment"
            )
            env_block["kwargs"] = {
                "project_template_dir": (
                    str(self.project_template_dir)
                    if self.project_template_dir else None
                ),
                "copy_task_skills": False,
                # Point the env at OUR shared skills dir via shared_skills_root.
                # The env resolves <jobs_dir>/shared_skills/<group>/ by default;
                # we override shared_skills_root to bind the specific dir in.
                "shared_skills_root": str(shared_skills_dir.parent),
            }

        # SkillFlow's yaml puts `env` as a sibling of `kwargs` under an agent
        # (not nested). Our CLI stashed it into worker.agent_kwargs["env"]
        # for transport; extract it back to the top level here, and strip it
        # out of kwargs so harbor's dataclass validator doesn't see an
        # unexpected field.
        raw_kwargs = dict(worker.agent_kwargs or {})
        agent_env = dict(raw_kwargs.pop("env", {}) or {})
        # If both `name` and `import_path` are set, harbor's agent factory
        # goes through its registered-agents lookup (matching on name), which
        # gives you the stock ClaudeCode (full install via apt-get), not the
        # NoInstall mixin we want. Forcing name=None when import_path is set
        # locks harbor to import_path-based loading. This matches what
        # SkillFlow's iter runner produces (name=null in the JobConfig).
        agent_block = {
            "name": None if worker.agent_import_path else (worker.agent_name or None),
            "import_path": worker.agent_import_path,
            "model_name": worker.agent_model,
            "kwargs": raw_kwargs,
            "env": agent_env,
        }

        # `datasets:` with n_tasks=0 registers a dataset name for harbor's
        # per-dataset metric aggregation without having harbor enumerate tasks
        # from the directory (we pass the explicit single task via `tasks:`).
        # Omitting datasets makes harbor's trial END hook crash on an empty
        # metrics list ("IndexError: list index out of range" in
        # `_update_metric_display`). Same pattern SkillFlow's iter runner uses.
        return {
            "job_name": job_name,
            "jobs_dir": str(job_dir.parent),
            "agents": [agent_block],
            "environment": env_block,
            "orchestrator": {"type": "local", "n_concurrent_trials": 1, "quiet": True},
            "tasks": [{"path": str(task_dir), "source": self.family_name}],
            "datasets": [{"path": str(task_dir.parent), "n_tasks": 0}],
            # Bumped to 4.0x = 7200s (120 min / 2 h). 90 min still potentially
            # too tight on kimi-cli's heavy families. Give it 2 hours.
            "agent_timeout_multiplier": 4.0,
        }


def _trial_was_429_exhausted(result_path: Path) -> bool:
    """True iff the just-finished trial died because claude-code's own
    retry budget was eaten by dashscope 429s.

    Signature: the LAST `{"type":"result"}` line in the trial's claude-code.txt
    has `is_error: true` and either `api_error_status: 429` or a result text
    mentioning the dashscope quota phrase. We don't retry on non-429 errors
    (real bugs in the prompt / tool calls) — those won't recover from
    container restart.
    """
    if not result_path.exists():
        return False
    try:
        parsed = json.loads(result_path.read_text(encoding="utf-8"))
    except Exception:
        return False
    trial_dir_s = parsed.get("trial_dir")
    if not trial_dir_s:
        return False
    cc_txt = Path(trial_dir_s) / "agent" / "claude-code.txt"
    if not cc_txt.exists():
        return False
    # Read the file backwards looking for the final "type":"result" line.
    try:
        content = cc_txt.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return False
    last_result_line = None
    for line in reversed(content.splitlines()):
        if '"type":"result"' in line:
            last_result_line = line
            break
    if not last_result_line:
        # No result line at all — could be subprocess died mid-run; let
        # higher-level error handling decide. Don't claim 429-exhaustion.
        return False
    try:
        rj = json.loads(last_result_line)
    except Exception:
        return False
    if not rj.get("is_error"):
        return False
    if rj.get("api_error_status") == 429:
        return True
    result_text = rj.get("result") or ""
    if isinstance(result_text, str):
        rt = result_text.lower()
        for phrase in (
            "rate limit", "rate_limit", "429",
            "concurrency", "quota exceeded", "too many requests",
        ):
            if phrase in rt:
                return True
    return False


def _resolve_trial_dir(trial_uri: str) -> Path:
    from urllib.parse import unquote, urlparse

    if trial_uri.startswith("file://"):
        parsed = urlparse(trial_uri)
        return Path(unquote(parsed.path))
    return Path(trial_uri)


# ---------------------------------------------------------------------------
# Dry-run mode
# ---------------------------------------------------------------------------


class DryRunHarborBridge(HarborBridgeBase):
    """No docker, no harbor. Fabricates a trial_dir layout and a seeded reward.

    Intent: exercise the runner's round loop, partitioning, and merge logic
    end-to-end without spinning up Docker or calling any LLM. Reward is
    deterministic given (worker_id, task_name, seed).
    """

    def __init__(self, seed: int = 0, per_trial_sleep_sec: float = 0.01) -> None:
        self.seed = seed
        self.per_trial_sleep_sec = per_trial_sleep_sec

    async def launch_trial(
        self,
        *,
        worker: UserSpec,
        task_dir: Path,
        shared_skills_dir: Path,
        job_dir: Path,
    ) -> WorkerTrialResult:
        # Materialize the "trial dir" on disk so downstream code that inspects
        # trial_dir/agent/trajectory.json etc. sees a consistent layout.
        trial_dir = job_dir / f"{task_dir.name}__dryrun"
        trial_dir.mkdir(parents=True, exist_ok=True)
        (trial_dir / "agent").mkdir(exist_ok=True)
        (trial_dir / "verifier").mkdir(exist_ok=True)

        reward = self._fake_reward(worker.id, task_dir.name)
        # Fake trajectory: 1 step saying "I solved it" or "I got stuck".
        fake_traj = [
            {
                "role": "assistant",
                "content": f"[dry-run] worker={worker.id} task={task_dir.name} reward={reward}",
            }
        ]
        (trial_dir / "agent" / "trajectory.json").write_text(
            json.dumps(fake_traj, indent=2), encoding="utf-8"
        )
        (trial_dir / "verifier" / "reward.txt").write_text(
            f"{reward}\n", encoding="utf-8"
        )
        (trial_dir / "result.json").write_text(
            json.dumps({
                "id": _stable_id(f"{worker.id}:{task_dir.name}"),
                "task_name": task_dir.name,
                "trial_name": trial_dir.name,
                "started_at": time.time(),
                "finished_at": time.time() + self.per_trial_sleep_sec,
                "config": {
                    "agent": {
                        "name": worker.agent_name,
                        "import_path": worker.agent_import_path,
                        "model_name": worker.agent_model,
                    }
                },
                "reward": reward,
            }, indent=2),
            encoding="utf-8",
        )

        await asyncio.sleep(self.per_trial_sleep_sec)
        return WorkerTrialResult(
            worker_id=worker.id,
            task_name=task_dir.name,
            reward=reward,
            verifier_passed=reward >= 1.0,
            trial_dir=trial_dir,
            exception_type=None,
            exception_message=None,
            extra={"mode": "dry-run"},
        )

    def _fake_reward(self, worker_id: str, task_name: str) -> float:
        # Seeded pseudo-random reward so tests / dry-runs are reproducible.
        rng = random.Random(f"{self.seed}:{worker_id}:{task_name}")
        # Small chance of reward=1.0, rest uniform in [0, 0.6].
        if rng.random() < 0.2:
            return 1.0
        return round(rng.uniform(0.0, 0.6), 3)


def _stable_id(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:12]

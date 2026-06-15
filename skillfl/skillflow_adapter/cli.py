"""Driver CLI for the federated skill-evolution adapter.

Mirrors SkillFlow's `iterative_shared_skills_runner.py` structure (outer loop
over families, optional multi-process parallelism across families) but swaps
the inner per-family loop for `FedRunner`, which partitions each family's
tasks across M workers and merges their patches via `CloudSkillMerge`.

Usage:
  cd SkillFlow-benchmark   # cwd must be SkillFlow-benchmark/ so task paths
                           # in the yaml resolve as relative paths
  python -m skillfl.skillflow_adapter.cli \\
      --config SkillFlow-benchmark/configs/fed.llmmerge.local.yaml \\
      --max-parallel-groups 1

Output layout matches SkillFlow's so scripts/extract_paper_metrics.py can
point at `--jobs <outdir>` the same way.
"""
from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# Make the repo root importable regardless of cwd.
_THIS = Path(__file__).resolve()
_REPO_ROOT = _THIS.parents[2]    # .../SkillFL/
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from skillfl.harbor_runner import UserSpec
from skillfl.skillflow_adapter.config import FedConfig
from skillfl.skillflow_adapter.llm_client import make_llm_call
from skillfl.skillflow_adapter.merge import RewardWeightedFileMerge
from skillfl.skillflow_adapter.partitioning import (
    BlockPartitioner,
    RandomPartitioner,
    ReplicatePartitioner,
    RoundRobinPartitioner,
)
from skillfl.skillflow_adapter.runner import FedRunner
from skillfl.skillflow_adapter.sync_schedule import (
    EveryKTaskSync,
    EveryTaskSync,
    OnceAtEndSync,
)


# --------------------------------------------------------------------------
# YAML parsing
# --------------------------------------------------------------------------


@dataclass
class FedJobConfig:
    job_name: str
    n_workers: int
    agent: dict[str, Any]                     # single agent spec (homogeneous)
    workers_override: list[dict] | None = None # heterogeneous path
    partitioner_name: str = "round_robin"
    partitioner_seed: int = 0
    sync_schedule_name: str = "every_task"
    sync_k: int = 1
    merger_name: str = "cloud_skill_merge"       # or "reward_weighted_file"
    patcher: dict[str, Any] = field(default_factory=dict)
    merger_llm: dict[str, Any] = field(default_factory=dict)
    # Cloud-skill merger knobs (used iff merger_name == "cloud_skill_merge").
    merger_skill: dict[str, Any] = field(default_factory=dict)
    datasets: list[str] = field(default_factory=list)   # list of test_tasks/<family> paths
    # Paths default to cwd — the CLI expects to be launched from within
    # `SkillFlow-benchmark/` so that `test_tasks/...` in the yaml and harbor's
    # `import_path: libs....` agent spec both resolve correctly.
    skillflow_root: Path = Path(".")
    project_template_dir: Path | None = None
    force_shared_env: bool = True
    isolated_worker_skills: bool = False
    merger_mode: str = "shared"
    runs_root: Path = Path("jobs")


def load_job_config(path: Path) -> FedJobConfig:
    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    if not isinstance(raw, dict):
        raise ValueError(f"{path} top level must be a mapping")

    workers_field = raw.get("workers")
    n_workers = int(raw.get("n_workers", 0))
    if workers_field is None and n_workers <= 0:
        raise ValueError("config must set either `workers:` list or `n_workers: N`")

    return FedJobConfig(
        job_name=str(raw["job_name"]),
        n_workers=n_workers or (len(workers_field) if workers_field else 0),
        agent=raw.get("agent") or {},
        workers_override=workers_field,
        partitioner_name=str(raw.get("partitioner", "round_robin")),
        partitioner_seed=int(raw.get("partitioner_seed", 0)),
        sync_schedule_name=str(raw.get("sync_schedule", "every_task")),
        sync_k=int(raw.get("sync_k", 1)),
        merger_name=str(raw.get("merger", "cloud_skill_merge")),
        patcher=raw.get("patcher") or {},
        merger_llm=raw.get("merger_llm") or {},
        merger_skill=raw.get("merger_skill") or {},
        datasets=[d["path"] if isinstance(d, dict) else d for d in (raw.get("datasets") or [])],
        skillflow_root=Path(raw.get("skillflow_root", ".")).resolve(),
        project_template_dir=(
            Path(raw["project_template_dir"]).resolve()
            if raw.get("project_template_dir") else None
        ),
        force_shared_env=bool(raw.get("force_shared_env", True)),
        isolated_worker_skills=bool(raw.get("isolated_worker_skills", False)),
        merger_mode=str(raw.get("merger_mode", "shared")),
        runs_root=Path(raw.get("runs_root", "jobs")).resolve(),
    )


# --------------------------------------------------------------------------
# Strategy constructors (from string → instance)
# --------------------------------------------------------------------------


def build_partitioner(name: str, seed: int = 0):
    if name == "round_robin":
        return RoundRobinPartitioner()
    if name == "block":
        return BlockPartitioner()
    if name == "random":
        return RandomPartitioner(seed=seed)
    if name == "replicate":
        return ReplicatePartitioner()
    raise ValueError(f"unknown partitioner: {name}")


def build_sync_schedule(name: str, k: int = 1):
    if name == "every_task":
        return EveryTaskSync()
    if name == "every_k_task":
        return EveryKTaskSync(k=k)
    if name == "once_at_end":
        return OnceAtEndSync()
    raise ValueError(f"unknown sync_schedule: {name}")


def build_merger(
    name: str,
    merger_llm_cfg: dict[str, Any],
    workers: list | None = None,
    *,
    skill_cfg: dict[str, Any] | None = None,
    runs_root: Path | None = None,
    job_name: str | None = None,
    family_name: str | None = None,
):
    if name == "reward_weighted_file":
        return RewardWeightedFileMerge()
    if name == "cloud_skill_merge":
        # Fixed cloud merger model + claude-code skill agent. Called once per
        # worker against that worker's pre-task library + all M patches.
        if not workers:
            raise ValueError("cloud_skill_merge requires the workers list")
        from skillfl.skillflow_adapter.merge import (
            CloudSkillMerge,
            make_claude_code_subprocess_runner,
            make_podman_claude_runner,
            make_podman_codex_runner,
        )
        skill_cfg = dict(skill_cfg or {})
        skill_dir = Path(
            skill_cfg.get("skill_dir")
            or (Path(__file__).parent / "merge_skill")
        )
        # Optional step-1 task-update skill. Default: bundled task_update_skill/.
        # Set merger_skill.task_update_skill_dir to "" or "none" to disable
        # (back-compat with v4.x runs that don't have a task-update step).
        tu_cfg = skill_cfg.get("task_update_skill_dir")
        if tu_cfg in ("", "none", None):
            tu_default = Path(__file__).parent / "task_update_skill"
            task_update_skill_dir = tu_default if tu_cfg is None else None
        else:
            task_update_skill_dir = Path(tu_cfg)
        task_update_max_turns = int(
            skill_cfg.get("task_update_max_turns", 10)
        )
        task_update_wall_clock_sec = int(
            skill_cfg.get("task_update_wall_clock_sec", 300)
        )
        merger_model = str(
            skill_cfg.get("merger_model")
            or merger_llm_cfg.get("model_name", "")
        )
        if not merger_model:
            raise ValueError(
                "cloud_skill_merge requires merger_skill.merger_model "
                "(or merger_llm.model_name as fallback)"
            )
        # Cloud merger creds: skill_cfg.merger_env wins, else inherit from the
        # first worker's env (typical: same gateway, same key).
        merger_env_raw = dict(skill_cfg.get("merger_env") or {})
        if not merger_env_raw and workers:
            first_env = (workers[0].agent_kwargs or {}).get("env") or {}
            merger_env_raw = dict(first_env)
        merger_env = {
            k: (_expand_env(v) if isinstance(v, str) else v)
            for k, v in merger_env_raw.items()
        }
        worker_models = {w.id: w.agent_model for w in workers}
        # Map each worker to a canonical CLI name so the merger can adapt
        # SKILL.md style per (model, CLI). Derived from agent_import_path; e.g.
        # NoInstallClaudeCode → "claude-code", NoInstallQwenCode → "qwen-code",
        # NoInstallKimiCli → "kimi-cli". Falls back to agent_name when import_path
        # is absent. Unknown classes pass through as the lower-cased class name.
        worker_clis = {w.id: _cli_name_from_user_spec(w) for w in workers}
        max_turns = int(skill_cfg.get("max_turns", 30))
        wall_clock_sec = int(skill_cfg.get("wall_clock_sec", 600))
        runner_kind = str(skill_cfg.get("agent_runner", "host"))
        if runner_kind in ("podman", "podman_codex"):
            image = skill_cfg.get("podman_image")
            if not image:
                raise ValueError(
                    f"cloud_skill_merge with agent_runner={runner_kind!r} requires "
                    "merger_skill.podman_image"
                )
            if runner_kind == "podman_codex":
                # Codex CLI variant — talks to OpenAI Responses API natively
                # (no Anthropic-protocol bridging). merger_env should carry
                # OPENAI_API_KEY; the runner does `codex login --with-api-key`
                # inline per call.
                agent_runner = make_podman_codex_runner(
                    image=str(image),
                    podman_bin=str(skill_cfg.get("podman_bin", "podman")),
                )
            else:
                agent_runner = make_podman_claude_runner(
                    image=str(image),
                    podman_bin=str(skill_cfg.get("podman_bin", "podman")),
                )
        elif runner_kind == "host":
            claude_bin = str(skill_cfg.get("claude_bin", "claude"))
            agent_runner = make_claude_code_subprocess_runner(claude_bin=claude_bin)
        else:
            raise ValueError(
                f"unknown merger_skill.agent_runner: {runner_kind!r} "
                f"(want 'host', 'podman', or 'podman_codex')"
            )
        if runs_root is not None and job_name and family_name:
            default_sandbox_root = (
                runs_root / job_name / family_name / "cloud_skill_merge_sandboxes"
            )
            memory_root = (
                runs_root / job_name / family_name / ".merger_memory"
            )
        else:
            default_sandbox_root = Path("./cloud_skill_merge_sandboxes")
            memory_root = Path("./.merger_memory")
        # Fallback when the cloud-agent merge fails (per-worker): the
        # deterministic reward-weighted file merge. Same fallback for every
        # worker — guaranteed to produce something without another LLM call.
        return CloudSkillMerge(
            agent_runner=agent_runner,
            merger_model=merger_model,
            merger_env=merger_env,
            worker_models=worker_models,
            worker_clis=worker_clis,
            skill_dir=skill_dir,
            default_sandbox_root=default_sandbox_root,
            family=family_name or "",
            fallback=RewardWeightedFileMerge(),
            memory_root=memory_root,
            max_turns=max_turns,
            wall_clock_sec=wall_clock_sec,
            task_update_skill_dir=task_update_skill_dir,
            task_update_max_turns=task_update_max_turns,
            task_update_wall_clock_sec=task_update_wall_clock_sec,
        )
    raise ValueError(
        f"unknown merger: {name!r} "
        f"(supported: 'reward_weighted_file', 'cloud_skill_merge')"
    )


def _expand_env(val: str | None) -> str | None:
    """Expand `$ENV_VAR` or `${ENV_VAR}` references (useful to avoid baking
    keys into yaml). Other shell-style features (`${VAR:-default}` etc.) are
    NOT supported — keep the expansion small and predictable."""
    if not val:
        return val
    if val.startswith("${") and val.endswith("}"):
        name = val[2:-1]
        return os.environ.get(name, val)
    if val.startswith("$"):
        return os.environ.get(val[1:], val)
    return val


# Known agent class → canonical CLI name. Add entries when new harnesses ship.
_CLI_NAME_BY_CLASS = {
    "NoInstallClaudeCode": "claude-code",
    "ClaudeCode":          "claude-code",
    "NoInstallQwenCode":   "qwen-code",
    "QwenCode":            "qwen-code",
    "NoInstallKimiCli":    "kimi-cli",
    "KimiCli":             "kimi-cli",
}


def _cli_name_from_user_spec(spec: UserSpec) -> str:
    """Derive a canonical CLI name for a worker.

    Priority:
      1. Class name parsed from `agent_import_path` (matches `_CLI_NAME_BY_CLASS`).
      2. Lower-cased class name as-is (for new harnesses not in the table).
      3. `agent_name` from the config block.
      4. "unknown".
    """
    ip = spec.agent_import_path or ""
    if ":" in ip:
        cls = ip.rsplit(":", 1)[-1]
        if cls in _CLI_NAME_BY_CLASS:
            return _CLI_NAME_BY_CLASS[cls]
        if cls:
            return cls.lower()
    return (spec.agent_name or "unknown").lower()


# --------------------------------------------------------------------------
# UserSpec construction
# --------------------------------------------------------------------------


def _spec_from_agent_block(agent_block: dict, worker_id: str) -> UserSpec:
    kwargs = dict(agent_block.get("kwargs") or {})
    env = dict(agent_block.get("env") or {})
    # Expand $ENV_VAR references inside env values.
    env = {k: (_expand_env(v) if isinstance(v, str) else v) for k, v in env.items()}
    # Harbor's agent config takes env under kwargs (via trial config flow) and
    # also via the agent record itself. We stash it in agent_kwargs["env"] so
    # harbor_bridge can extract it when building the JobConfig.
    merged_kwargs = dict(kwargs)
    if env:
        merged_kwargs["env"] = env
    return UserSpec(
        id=worker_id,
        agent_name=agent_block.get("name") or "claude-code",
        agent_import_path=agent_block.get("import_path"),
        agent_model=agent_block.get("model_name") or "",
        agent_kwargs=merged_kwargs,
    )


def build_workers(cfg: FedJobConfig) -> list[UserSpec]:
    if cfg.workers_override:
        out = []
        for i, spec in enumerate(cfg.workers_override):
            wid = spec.get("id") or f"u{i}"
            agent = spec.get("agent") or spec  # support both nested and flat
            out.append(_spec_from_agent_block(agent, wid))
        # Sanity: unique ids enforced downstream by FedConfig.validate().
        return out
    return [
        _spec_from_agent_block(cfg.agent, f"u{i}") for i in range(cfg.n_workers)
    ]


# --------------------------------------------------------------------------
# Per-family run
# --------------------------------------------------------------------------


def resolve_family_tasks(family_path: Path, skillflow_root: Path) -> tuple[str, list[Path]]:
    """Return (family_name, ordered_task_dirs) for a test_tasks/<family> path."""
    if not family_path.is_absolute():
        family_path = (skillflow_root / family_path).resolve()
    if not family_path.exists():
        raise FileNotFoundError(f"family path not found: {family_path}")
    family_name = family_path.name

    ranking_file = family_path / "ALL_TASK_DIFFICULTY_RANKING.json"
    present = sorted([d for d in family_path.iterdir() if d.is_dir() and (d / "task.toml").exists()])
    if not ranking_file.exists():
        return family_name, present

    ranking = json.loads(ranking_file.read_text())
    by_name = {d.name: d for d in present}
    ordered: list[Path] = []
    seen: set[str] = set()
    for name in ranking:
        if name in by_name and name not in seen:
            ordered.append(by_name[name])
            seen.add(name)
    # Append unranked extras at the end (matches SkillFlow's behavior).
    for d in present:
        if d.name not in seen:
            ordered.append(d)
    return family_name, ordered


async def _run_one_family(
    cfg: FedJobConfig,
    family_path_str: str,
) -> dict[str, Any]:
    t0 = time.time()
    family_path = Path(family_path_str)
    family_name, task_dirs = resolve_family_tasks(family_path, cfg.skillflow_root)
    workers = build_workers(cfg)

    # Patcher creds (if missing in config, inherit from first worker's env).
    patcher_key = _expand_env(cfg.patcher.get("api_key"))
    patcher_base = cfg.patcher.get("api_base")
    if not patcher_key or not patcher_base:
        # Pull from first worker's env.
        first_env = workers[0].agent_kwargs.get("env") or {}
        patcher_key = patcher_key or first_env.get("ANTHROPIC_API_KEY")
        patcher_base = patcher_base or first_env.get("ANTHROPIC_BASE_URL")

    # Per-worker patcher: each worker's trajectory distilled by its own model.
    # Upstream SkillFlow is single-agent and uses first_agent.model_name; in
    # our heterogeneous M-worker setup we route per-worker so the patch
    # reflects the worker's own style. patcher.* yaml block only carries
    # shared knobs (temperature/max_tokens) + fallback creds.
    patcher_worker_llm_configs: dict[str, dict] = {}
    for w in workers:
        w_env = (w.agent_kwargs.get("env") or {}) if w.agent_kwargs else {}
        # Prefer ANTHROPIC_* env (claude-code workers), fall back to OPENAI_*
        # (qwen-code / kimi-cli workers run native OpenAI-protocol and only
        # set OPENAI_API_KEY/OPENAI_BASE_URL). Final fallback: global
        # patcher.* yaml block. Without the OPENAI_* fallback, OpenAI-protocol
        # workers had patcher pointed at the global Anthropic endpoint with no
        # matching key, producing silent 404 NotFoundError on every patch.
        w_key = (
            _expand_env(w_env.get("ANTHROPIC_API_KEY"))
            or _expand_env(w_env.get("OPENAI_API_KEY"))
            or patcher_key
        )
        w_base = (
            w_env.get("ANTHROPIC_BASE_URL")
            or w_env.get("OPENAI_BASE_URL")
            or patcher_base
        )
        patcher_worker_llm_configs[w.id] = {
            "model_name": w.agent_model,
            "api_base": w_base,
            "api_key": w_key,
            "temperature": float(cfg.patcher.get("temperature", 0.2)),
            "max_tokens": int(cfg.patcher.get("max_tokens", 8192)),
        }

    fed_cfg = FedConfig(
        run_id=cfg.job_name,
        runs_root=cfg.runs_root,
        family_name=family_name,
        task_dirs=task_dirs,
        workers=workers,
        patcher_worker_llm_configs=patcher_worker_llm_configs,
        partitioner=build_partitioner(cfg.partitioner_name, cfg.partitioner_seed),
        sync_schedule=build_sync_schedule(cfg.sync_schedule_name, cfg.sync_k),
        merger=build_merger(
            cfg.merger_name,
            cfg.merger_llm,
            workers=workers,
            skill_cfg=cfg.merger_skill,
            runs_root=cfg.runs_root,
            job_name=cfg.job_name,
            family_name=family_name,
        ),
        project_template_dir=cfg.project_template_dir,
        force_shared_env=cfg.force_shared_env,
        isolated_worker_skills=cfg.isolated_worker_skills,
        merger_mode=cfg.merger_mode,
    )

    runner = FedRunner(fed_cfg, dry_run=False, skillflow_root=cfg.skillflow_root)
    try:
        await runner.run()
        status = "ok"
        err = ""
    except Exception as e:
        status = "failed"
        err = f"{type(e).__name__}: {e}"
    return {
        "family": family_name,
        "status": status,
        "elapsed_sec": round(time.time() - t0, 1),
        "error": err,
        "run_dir": str(fed_cfg.runs_root / cfg.job_name / family_name),
    }


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def _run_family_in_subprocess(
    config_path: str, family_name: str, cwd: str,
) -> dict[str, Any]:
    """Invoke ourselves with --only-family in a subprocess.

    Each subprocess is an isolated Python process with its own event loop.
    Multiple harbor.Job instances sharing an event loop can wedge on agent
    setup (we hit AgentSetupTimeoutError=360s that way). Per-family subprocess
    isolation matches how SkillFlow's iterative runner gets family-parallelism.
    """
    t0 = time.time()
    args = [
        sys.executable, "-m", "skillfl.skillflow_adapter.cli",
        "--config", config_path,
        "--only-family", family_name,
        "--max-parallel-groups", "1",
    ]
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    try:
        proc = subprocess.run(args, cwd=cwd, env=env,
                              capture_output=True, text=True)
    except Exception as e:
        return {
            "family": family_name,
            "status": "failed",
            "elapsed_sec": round(time.time() - t0, 1),
            "error": f"subprocess.run raised: {type(e).__name__}: {e}",
            "run_dir": "",
        }

    status = "ok" if proc.returncode == 0 else "failed"
    err = "" if status == "ok" else (
        f"exit={proc.returncode}; "
        f"stderr_tail={(proc.stderr or '').strip().splitlines()[-1:] if proc.stderr else ''}"
    )
    return {
        "family": family_name,
        "status": status,
        "elapsed_sec": round(time.time() - t0, 1),
        "error": err,
        "run_dir": "",
        "subprocess_stdout_tail": (proc.stdout or "")[-2000:],
    }


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--config", type=Path, required=True)
    p.add_argument("--max-parallel-groups", type=int, default=10,
                   help="Families to run concurrently. When >1, each family "
                        "is spawned in its own subprocess (process-per-family, "
                        "matching SkillFlow's isolation pattern). When 1, runs "
                        "sequentially in this process. Default 10.")
    p.add_argument("--only-family", type=str, default=None,
                   help="Run just this one family by name. Forces single-"
                        "family in-process execution regardless of "
                        "--max-parallel-groups.")
    args = p.parse_args()

    cfg = load_job_config(args.config)
    cfg.runs_root.mkdir(parents=True, exist_ok=True)

    datasets = cfg.datasets
    if args.only_family:
        datasets = [d for d in datasets if d.endswith(args.only_family) or Path(d).name == args.only_family]
        if not datasets:
            print(f"No family matched --only-family {args.only_family!r}", file=sys.stderr)
            sys.exit(1)

    print(f"Run output directory: {cfg.runs_root / cfg.job_name}")
    print(f"Families to run: {len(datasets)}  max parallel: {args.max_parallel_groups}")

    # Single-family OR single-parallel → in-process.
    if args.only_family or args.max_parallel_groups == 1:
        async def _sequential():
            out = []
            for ds in datasets:
                print(f"\n>>> family: {Path(ds).name}")
                r = await _run_one_family(cfg, ds)
                print(f"<<< family: {r['family']} status={r['status']} elapsed={r['elapsed_sec']}s "
                      + (r['error'] or ''))
                out.append(r)
            return out
        results = asyncio.run(_sequential())
    else:
        # Multi-family → subprocess-per-family for isolation.
        config_path_str = str(args.config.resolve())
        cwd_str = os.getcwd()
        family_names = [Path(ds).name for ds in datasets]

        results = []
        print(f"Dispatching {len(family_names)} families across "
              f"{args.max_parallel_groups} parallel subprocesses...")
        with concurrent.futures.ProcessPoolExecutor(
            max_workers=args.max_parallel_groups
        ) as ex:
            future_to_name = {
                ex.submit(_run_family_in_subprocess, config_path_str, name, cwd_str): name
                for name in family_names
            }
            for fut in concurrent.futures.as_completed(future_to_name):
                name = future_to_name[fut]
                try:
                    r = fut.result()
                except Exception as e:
                    r = {"family": name, "status": "failed", "elapsed_sec": 0,
                         "error": f"future exception: {type(e).__name__}: {e}",
                         "run_dir": ""}
                marker = "[OK]" if r["status"] == "ok" else "[FAIL]"
                print(f"{marker} {r['family']:<45} elapsed={r['elapsed_sec']:>6}s"
                      + (f"  {r['error']}" if r["error"] else ""))
                results.append(r)

    # Print compact summary.
    ok = [r for r in results if r["status"] == "ok"]
    print()
    for r in results:
        marker = "[OK]" if r["status"] == "ok" else "[FAIL]"
        print(f"{marker} {r['family']:<45} elapsed={r['elapsed_sec']:>6}s"
              + (f"  {r['error']}" if r["error"] else ""))
    print(f"\nCompleted: {len(ok)}/{len(results)} families succeeded.")

    # Persist summary for post-hoc analysis.
    summary_path = cfg.runs_root / cfg.job_name / "cli_summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"Wrote {summary_path}")


if __name__ == "__main__":
    main()

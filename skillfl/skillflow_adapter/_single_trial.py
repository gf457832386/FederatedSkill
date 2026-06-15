"""Standalone runner: one harbor Job (1 agent, 1 task), one Python process.

Why this exists: two or more `harbor.Job` instances sharing a single Python
event loop wedge each other's agent-setup (observed: AgentSetupTimeoutError
after 360s on every trial when M=2 workers ran via `asyncio.gather`). Putting
each trial in its own OS process gives each harbor Job its own event loop,
its own docker client, its own logger, and any module-level singletons get
a fresh initialization.

Interface:

    python -m skillfl.skillflow_adapter._single_trial <cfg.json> <result.json>

`cfg.json` is a harbor `JobConfig.model_dump()` (dict of primitives).
`result.json` is written on exit and has a `status` field plus either
`{trial_name, trial_uri, trial_dir, reward, exception_*}` or an error block.
Exit code is 0 on `status=ok`, non-zero otherwise.
"""
from __future__ import annotations

import asyncio
import json
import sys
import traceback
from pathlib import Path
from urllib.parse import unquote, urlparse


def _resolve_trial_dir(trial_uri: str | None) -> str | None:
    if not trial_uri:
        return None
    if trial_uri.startswith("file://"):
        return unquote(urlparse(trial_uri).path)
    return trial_uri


def _write(out_path: Path, payload: dict) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: _single_trial.py <cfg.json> <result.json>", file=sys.stderr)
        return 2
    cfg_path = Path(sys.argv[1])
    out_path = Path(sys.argv[2])

    try:
        cfg_dict = json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception as e:
        _write(out_path, {"status": "failed_to_load_cfg",
                          "exception_type": type(e).__name__,
                          "exception_message": str(e)})
        return 2

    try:
        from harbor import Job
        from harbor.models.job.config import JobConfig
        job_config = JobConfig.model_validate(cfg_dict)
    except Exception as e:
        _write(out_path, {"status": "failed_to_build_jobconfig",
                          "exception_type": type(e).__name__,
                          "exception_message": str(e),
                          "traceback": traceback.format_exc()[-2000:]})
        return 3

    async def _run():
        job = Job(config=job_config)
        return await job.run()

    try:
        result = asyncio.run(_run())
    except Exception as e:
        _write(out_path, {"status": "job_run_raised",
                          "exception_type": type(e).__name__,
                          "exception_message": str(e)[:1000],
                          "traceback": traceback.format_exc()[-2000:]})
        return 4

    if not result.trial_results:
        _write(out_path, {"status": "no_trial_results",
                          "exception_type": "NoTrialResult",
                          "exception_message": "Job returned 0 trial_results"})
        return 5

    tr = result.trial_results[0]
    reward = None
    if tr.verifier_result and tr.verifier_result.rewards:
        r0 = next(iter(tr.verifier_result.rewards.values()), None)
        reward = float(r0) if r0 is not None else None

    _write(out_path, {
        "status": "ok",
        "trial_name": tr.trial_name,
        "trial_uri": tr.trial_uri,
        "trial_dir": _resolve_trial_dir(tr.trial_uri),
        "reward": reward,
        "exception_type": getattr(tr, "exception_type", None),
        "exception_message": getattr(tr, "exception_message", None),
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())

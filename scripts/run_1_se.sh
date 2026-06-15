#!/usr/bin/env bash
# Setting 1: Self-Evolve baseline (single worker, no federation).
#
# Usage:
#   scripts/run_1_se.sh                            # sweep all 20 families
#   FAMILY=Production-Capacity-Planning scripts/run_1_se.sh
#   CONFIG=configs/my_se.yaml scripts/run_1_se.sh  # custom config
#   MAX_PARALLEL_GROUPS=4 scripts/run_1_se.sh      # parallelize families
#
# The SE pipeline uses SkillFlow's `iterative_shared_skills_runner.py`
# (lives at external/SkillFlow/ — installed by setup.sh) — single agent,
# patch after every trial, no merger.

set -euo pipefail
# shellcheck source=scripts/_common.sh
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

CONFIG="${CONFIG:-configs/1_se_qwen.local.yaml}"
MAX_PARALLEL_GROUPS="${MAX_PARALLEL_GROUPS:-1}"
LOG_TAG="${LOG_TAG:-se}"

ARGS=(--config "$CONFIG" --max-parallel-groups "$MAX_PARALLEL_GROUPS")
if [ -n "${FAMILY:-}" ]; then
    # iter runner doesn't take --only-family; substitute by writing a one-off
    # config that points at just that family. The SkillFlow yaml format keeps
    # the datasets list at the top level.
    TMP_CFG="$(mktemp -t se_${FAMILY//\//_}.XXXXXX.yaml)"
    python - "$CONFIG" "$FAMILY" "$TMP_CFG" <<'PY'
import sys, yaml, pathlib
src, fam, dst = sys.argv[1:]
cfg = yaml.safe_load(open(src))
cfg['datasets'] = [{'path': f'test_tasks/{fam}'}]
cfg['job_name'] = cfg.get('job_name', 'se-run') + f"--{fam}"
pathlib.Path(dst).write_text(yaml.safe_dump(cfg, sort_keys=False))
print(f"[se] wrote one-family config: {dst}", file=sys.stderr)
PY
    ARGS=(--config "$TMP_CFG" --max-parallel-groups "$MAX_PARALLEL_GROUPS")
    LOG_NAME="${LOG_TAG}_${FAMILY}.log"
    trap 'rm -f "$TMP_CFG"' EXIT
else
    LOG_NAME="${LOG_TAG}_all.log"
fi

ITER_RUNNER="${ITER_RUNNER:-external/SkillFlow/iterative_shared_skills_runner.py}"
if [ ! -f "$ITER_RUNNER" ]; then
    echo "[run] ERROR: iterative_shared_skills_runner.py not found at $ITER_RUNNER" >&2
    echo "[run]        Did you run setup.sh? (clones SkillFlow into external/SkillFlow)" >&2
    exit 1
fi
run_with_log "$LOG_NAME" python "$ITER_RUNNER" "${ARGS[@]}" "$@"

#!/usr/bin/env bash
# Setting 2: Homogeneous federation — three GLM-5 workers, all on claude-code.
#
# Usage:
#   scripts/run_2_fed_3glm_cc.sh                                # all 20 families
#   FAMILY=Production-Capacity-Planning scripts/run_2_fed_3glm_cc.sh
#   MAX_PARALLEL_GROUPS=4 scripts/run_2_fed_3glm_cc.sh           # parallel families
#   CONFIG=configs/my_homog.yaml scripts/run_2_fed_3glm_cc.sh    # custom config

set -euo pipefail
# shellcheck source=scripts/_common.sh
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

CONFIG="${CONFIG:-configs/2_fed_3glm_cc.local.yaml}"
MAX_PARALLEL_GROUPS="${MAX_PARALLEL_GROUPS:-1}"
LOG_TAG="${LOG_TAG:-fed_3glm_cc}"

ARGS=(--config "$CONFIG" --max-parallel-groups "$MAX_PARALLEL_GROUPS")
if [ -n "${FAMILY:-}" ]; then
    ARGS+=(--only-family "$FAMILY")
    LOG_NAME="${LOG_TAG}_${FAMILY}.log"
else
    LOG_NAME="${LOG_TAG}_all.log"
fi

run_with_log "$LOG_NAME" python -m skillfl.skillflow_adapter.cli "${ARGS[@]}" "$@"

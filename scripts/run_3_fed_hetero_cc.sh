#!/usr/bin/env bash
# Setting 3: Heterogeneous federation, single CLI — qwen + glm + kimi all on claude-code.
# Table 1 (CC-CLI) setting from the paper.
#
# Usage:
#   scripts/run_3_fed_hetero_cc.sh
#   FAMILY=Production-Capacity-Planning scripts/run_3_fed_hetero_cc.sh
#   MAX_PARALLEL_GROUPS=4 scripts/run_3_fed_hetero_cc.sh
#   CONFIG=configs/my_hetero_cc.yaml scripts/run_3_fed_hetero_cc.sh

set -euo pipefail
# shellcheck source=scripts/_common.sh
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

CONFIG="${CONFIG:-configs/3_fed_hetero_cc.local.yaml}"
MAX_PARALLEL_GROUPS="${MAX_PARALLEL_GROUPS:-1}"
LOG_TAG="${LOG_TAG:-fed_hetero_cc}"

ARGS=(--config "$CONFIG" --max-parallel-groups "$MAX_PARALLEL_GROUPS")
if [ -n "${FAMILY:-}" ]; then
    ARGS+=(--only-family "$FAMILY")
    LOG_NAME="${LOG_TAG}_${FAMILY}.log"
else
    LOG_NAME="${LOG_TAG}_all.log"
fi

run_with_log "$LOG_NAME" python -m skillfl.skillflow_adapter.cli "${ARGS[@]}" "$@"

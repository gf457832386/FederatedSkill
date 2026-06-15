#!/usr/bin/env bash
# Setting 4: Heterogeneous federation, native CLIs — qwen-code + claude-code + kimi-cli.
# Table 2 (mixed-CLI) setting from the paper.
#
# Usage:
#   scripts/run_4_fed_hetero_mixed_cli.sh
#   FAMILY=Production-Capacity-Planning scripts/run_4_fed_hetero_mixed_cli.sh
#   MAX_PARALLEL_GROUPS=4 scripts/run_4_fed_hetero_mixed_cli.sh
#   CONFIG=configs/my_mixed.yaml scripts/run_4_fed_hetero_mixed_cli.sh

set -euo pipefail
# shellcheck source=scripts/_common.sh
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

CONFIG="${CONFIG:-configs/4_fed_hetero_mixed_cli.local.yaml}"
MAX_PARALLEL_GROUPS="${MAX_PARALLEL_GROUPS:-1}"
LOG_TAG="${LOG_TAG:-fed_hetero_mixed_cli}"

ARGS=(--config "$CONFIG" --max-parallel-groups "$MAX_PARALLEL_GROUPS")
if [ -n "${FAMILY:-}" ]; then
    ARGS+=(--only-family "$FAMILY")
    LOG_NAME="${LOG_TAG}_${FAMILY}.log"
else
    LOG_NAME="${LOG_TAG}_all.log"
fi

run_with_log "$LOG_NAME" python -m skillfl.skillflow_adapter.cli "${ARGS[@]}" "$@"

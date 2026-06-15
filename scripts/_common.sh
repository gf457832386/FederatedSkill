# Shared helpers for run_*.sh — sourced, never executed directly.
#
# - Discovers the repo root regardless of where the script is launched from.
# - Activates .venv if one exists and the caller hasn't already.
# - Loads .env so the YAML configs can reference $DASHSCOPE_KEY / $MOONSHOT_KEY.
# - Lays out the log directory.

if [ -n "${_FS_COMMON_SOURCED:-}" ]; then return; fi
_FS_COMMON_SOURCED=1

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# Auto-activate venv if not already inside one
if [ -z "${VIRTUAL_ENV:-}" ] && [ -f .venv/bin/activate ]; then
    # shellcheck source=/dev/null
    source .venv/bin/activate
fi

# Load API keys
if [ -f .env ]; then
    set -a
    # shellcheck source=/dev/null
    source .env
    set +a
fi

LOG_DIR="${LOG_DIR:-logs}"
mkdir -p "$LOG_DIR"

# Honor DOCKER_HOST so podman socket users don't have to export it inline
export DOCKER_HOST="${DOCKER_HOST:-unix:///run/user/$(id -u)/podman/podman.sock}"

# Helper that runs a CLI entry point and tees to a log file. Takes:
#   $1: log filename (under $LOG_DIR/)
#   $@ (rest): the command to run, args included
run_with_log() {
    local logname="$1"; shift
    local logpath="$LOG_DIR/$logname"
    echo "[run] cmd: $*"
    echo "[run] log: $logpath"
    "$@" 2>&1 | tee "$logpath"
}

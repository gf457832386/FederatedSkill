#!/usr/bin/env bash
# Emergency revert: restore library/ to its pre-task baseline state.
#
# The runner copies the pre-task library into both `library/` (read-write
# working copy) and `.baseline_library/` (immutable snapshot) at sandbox
# startup. If you realize you've made the library worse than baseline, run
# this to wipe library/ and re-copy from .baseline_library/.
#
# Use sparingly — prefer narrow Edit reverts over full revert. After running
# this, you've effectively rejected ALL peer patches for this round.
#
# Usage:  bash scripts/revert_to_baseline.sh

set -uo pipefail
cd "$(dirname "$0")/.."   # cd to sandbox root

if [[ ! -d ".baseline_library" ]]; then
  echo "ERROR: .baseline_library/ not found in sandbox root."
  echo "       The runner should have created this — if it didn't, the"
  echo "       sandbox is malformed and revert is not possible."
  exit 1
fi

if [[ -d library ]]; then
  rm -rf library
fi
cp -r .baseline_library library

n_files=$(find library -type f 2>/dev/null | wc -l | tr -d ' ')
echo "Reverted library/ to baseline ($n_files file(s))."
echo "All this round's peer absorptions and your own modifications are now"
echo "discarded for THIS LIBRARY. Decide carefully what to do next — most"
echo "likely you want to write DONE.txt with a note explaining the revert."

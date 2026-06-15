#!/usr/bin/env bash
# Combined post-merge validation. Replaces 4 separate calls (validate_skill_md,
# find_near_dup, py_compile, grep). Run ONCE before writing DONE.txt.
#
# Usage:  bash scripts/validate_library.sh
#
# Outputs are clearly delimited so the agent can grep "FAIL" or "OK".
# Exit code 0 always — the agent reads stdout and decides whether to fix.

set -uo pipefail
shopt -s nullglob
cd "$(dirname "$0")/.."

echo "=== validate_skill_md ==="
python3 scripts/validate_skill_md.py library/ || true
echo

echo "=== find_near_dup_skills ==="
python3 scripts/find_near_dup_skills.py library/ || true
echo

echo "=== script syntax (py_compile) ==="
n_ok=0; n_fail=0
for py in library/*/scripts/*.py library/*/scripts/**/*.py; do
  [[ -f "$py" ]] || continue
  if python3 -m py_compile "$py" 2>/tmp/_pyerr; then
    n_ok=$((n_ok+1))
  else
    n_fail=$((n_fail+1))
    echo "FAIL: $py"
    cat /tmp/_pyerr
  fi
done
echo "scripts checked: $((n_ok+n_fail)), ok=$n_ok, fail=$n_fail"
echo

echo "=== script overfit / network grep ==="
# Flag absolute paths, network calls, and known task-specific filenames.
# Agent should review hits and decide whether to sanitize or reject.
hits=0
for py in library/*/scripts/*.py library/*/scripts/**/*.py; do
  [[ -f "$py" ]] || continue
  out=$(grep -nE '/(root|home|tmp)/|http://|https://|urllib|requests|\.xlsx["'"'"']|\.csv["'"'"']' "$py" 2>/dev/null)
  if [[ -n "$out" ]]; then
    echo "HITS in $py:"
    echo "$out" | head -10
    hits=$((hits+1))
  fi
done
echo "scripts with overfit/network hits: $hits"
echo

echo "=== junk file scan ==="
junk=$(find library/ \( -name __pycache__ -o -name '*.pyc' -o -name '.DS_Store' \) -print 2>/dev/null)
if [[ -n "$junk" ]]; then
  echo "JUNK files in library/ (should be removed before DONE):"
  echo "$junk"
fi

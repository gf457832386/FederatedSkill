#!/usr/bin/env bash
# Side-by-side dump of `description:` frontmatter fields from every SKILL.md
# in patches/ + peer_libraries/ + library/. Helps the agent decide
# "same workflow vs different sub-tasks" BEFORE consolidating.
#
# Usage:  bash scripts/compare_skill_descriptions.sh
#
# Output groups by skill path (all paths seen across all sources):
#   <path>
#     library/   → description (or [missing])
#     patches/u0 → description (or [no patch] / [no description])
#     patches/u1 → ...
#     peer_libraries/u1 → ...

set -uo pipefail
shopt -s nullglob
cd "$(dirname "$0")/.."

extract_desc() {
  local f="$1"
  [[ -f "$f" ]] || { echo "[missing]"; return; }
  awk '/^---/{f++; next} f==1 && /^description:/{sub(/^description:[[:space:]]*/,""); print; exit}' "$f" 2>/dev/null \
    | sed 's/^["'\'']//; s/["'\'']$//' \
    | head -c 200 \
    | tr '\n' ' '
  echo ""
}

# Collect all distinct skill paths (relative to library/).
declare -A seen
collect_paths() {
  local base="$1"
  [[ -d "$base" ]] || return
  while IFS= read -r f; do
    rel="${f#$base/}"
    rel="${rel%/SKILL.md}"
    seen["$rel"]=1
  done < <(find "$base" -name SKILL.md 2>/dev/null)
}

collect_paths library
for d in patches/*/files; do collect_paths "$d"; done
collect_paths peer_libraries
# Also include nested: peer_libraries/<peer>/<path>/SKILL.md
for pdir in peer_libraries/*/; do
  collect_paths "$pdir"
done

if [[ ${#seen[@]} -eq 0 ]]; then
  echo "(no SKILL.md files found anywhere)"
  exit 0
fi

# Sort paths alphabetically for stable output.
mapfile -t paths < <(printf '%s\n' "${!seen[@]}" | sort)

echo "=== description: fields across sources ==="
echo "(use this to decide same-workflow vs different-sub-task before consolidating)"
echo

for p in "${paths[@]}"; do
  echo "--- $p ---"
  # library/
  echo "  library/             → $(extract_desc "library/$p/SKILL.md")"
  # patches/<wid>/files/
  for pdir in patches/*/; do
    wid=$(basename "$pdir")
    pf="$pdir/files/$p/SKILL.md"
    if [[ -f "$pf" ]]; then
      echo "  patches/$wid         → $(extract_desc "$pf")"
    fi
  done
  # peer_libraries/<peer>/
  for pdir in peer_libraries/*/; do
    peer=$(basename "$pdir")
    pl="$pdir/$p/SKILL.md"
    if [[ -f "$pl" ]]; then
      echo "  peer_libraries/$peer → $(extract_desc "$pl")"
    fi
  done
  echo
done

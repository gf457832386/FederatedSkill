#!/usr/bin/env bash
# Convenience: copy a worker's entire patch (upserts + apply deletes) into library/.
# Use this when the merger agent decides to fully accept one worker's contribution
# without further local editing.
#
# Usage:  bash scripts/apply_full_patch.sh patches/u1
#
# Equivalent to:
#   cp -r patches/u1/files/* library/      (for upserts)
#   for p in $(jq -r '.delete_paths[]' patches/u1/meta.json); do rm -rf library/$p; done
#
# This script does NOT touch other workers' patches — it's a per-patch helper.
# To synthesize across multiple patches, the agent should Edit/Write directly.

set -uo pipefail
cd "$(dirname "$0")/.."   # cd to sandbox root

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 <patches/worker_id>"
  exit 2
fi

pdir="$1"
if [[ ! -d "$pdir" ]]; then
  echo "ERROR: $pdir not a directory"
  exit 1
fi

meta="$pdir/meta.json"
if [[ ! -f "$meta" ]]; then
  echo "ERROR: $meta not found"
  exit 1
fi

# Apply upserts: copy each file in patches/<wid>/files/<rel> to library/<rel>.
files_dir="$pdir/files"
n_upsert=0
if [[ -d "$files_dir" ]]; then
  while IFS= read -r src; do
    rel="${src#$files_dir/}"
    dst="library/$rel"
    mkdir -p "$(dirname "$dst")"
    cp "$src" "$dst"
    n_upsert=$((n_upsert + 1))
  done < <(find "$files_dir" -type f 2>/dev/null)
fi

# Apply deletes from meta.json's delete_paths.
n_delete=0
deletes=$(python3 -c "import json; print('\n'.join(json.load(open('$meta')).get('delete_paths',[])))" 2>/dev/null)
if [[ -n "$deletes" ]]; then
  while IFS= read -r p; do
    [[ -z "$p" ]] && continue
    if [[ -e "library/$p" ]]; then
      rm -rf "library/$p"
      n_delete=$((n_delete + 1))
    fi
  done <<< "$deletes"
fi

echo "Applied $pdir: $n_upsert upsert(s), $n_delete delete(s)."

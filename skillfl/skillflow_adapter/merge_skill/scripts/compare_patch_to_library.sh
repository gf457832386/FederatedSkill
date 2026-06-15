#!/usr/bin/env bash
# Show unified diffs for every file a worker proposes vs the current library.
# Use this to inspect ALL of one worker's proposed changes in one Bash turn,
# instead of diffing file-by-file.
#
# Usage:  bash scripts/compare_patch_to_library.sh patches/u1
#
# For each file under patches/u1/files/<rel>, prints `diff -u library/<rel> <patch>`.
# Files missing in library are treated as new (`/dev/null`); files in library
# but not in patch are NOT shown — use peer_consensus.py / summarize_patches.sh
# to see the broader picture.
#
# Also lists meta.json.delete_paths with their current library content (head),
# so the agent can judge whether to honor the delete.

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
files_dir="$pdir/files"

echo "=== Diffs for $pdir vs library/ ==="
echo

if [[ -d "$files_dir" ]]; then
  any=0
  while IFS= read -r src; do
    rel="${src#$files_dir/}"
    lib="library/$rel"
    if [[ -f "$lib" ]]; then
      echo "--- diff: $rel (modified) ---"
      diff -u "$lib" "$src" || true
    else
      echo "--- diff: $rel (NEW — not in library) ---"
      diff -u /dev/null "$src" || true
    fi
    echo
    any=1
  done < <(find "$files_dir" -type f 2>/dev/null | sort)
  [[ "$any" -eq 0 ]] && echo "(no files in patch's files/ subdir)"
else
  echo "(no files/ subdir)"
fi

# Show deletes with a peek at what would be removed.
if [[ -f "$meta" ]]; then
  deletes=$(python3 -c "import json; print('\n'.join(json.load(open('$meta')).get('delete_paths',[])))" 2>/dev/null || true)
  if [[ -n "$deletes" ]]; then
    echo "=== Proposed deletes (showing first 20 lines of each library file) ==="
    while IFS= read -r p; do
      [[ -z "$p" ]] && continue
      echo "--- delete: $p ---"
      if [[ -f "library/$p" ]]; then
        head -20 "library/$p"
      elif [[ -d "library/$p" ]]; then
        echo "(directory; would be removed recursively)"
        ls "library/$p" 2>/dev/null | head -10 | sed 's/^/  /'
      else
        echo "(not in your library — delete is a no-op for you)"
      fi
      echo
    done <<< "$deletes"
  fi
fi

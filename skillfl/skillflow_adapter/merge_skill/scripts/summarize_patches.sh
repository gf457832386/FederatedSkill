#!/usr/bin/env bash
# Compact one-shot overview of all peer patches in the current sandbox.
# Run from the sandbox root (where patches/ lives). Outputs a human-readable
# summary suitable for the merger agent to read in a single Bash turn.
#
# Format per patch:
#   --- <worker_id> (reward=X.XXX) ---
#   Summary: <one-line summary>
#   Upserts (N files):
#     <path>  (M lines, +K new vs library)
#     ...
#   Deletes (N files):
#     <path>
#
# `+K new vs library` shows how big the proposed change is (line delta vs
# the library's current version, if any). Helps the agent prioritize which
# diffs to dig into. Errors are silenced — non-existent files just show as
# "(new)".

set -uo pipefail
shopt -s nullglob          # empty patches/ should yield empty loop, not literal "*"
cd "$(dirname "$0")/.."    # cd to sandbox root (script lives in scripts/)

if [[ ! -d patches ]]; then
  echo "ERROR: no patches/ in $(pwd)"
  exit 1
fi

for pdir in patches/*/; do
  wid=$(basename "$pdir")
  meta="$pdir/meta.json"
  if [[ ! -f "$meta" ]]; then
    echo "--- $wid (no meta.json) ---"
    continue
  fi

  reward=$(python3 -c "import json,sys; d=json.load(open('$meta')); print(f\"{d.get('reward',0):.3f}\")" 2>/dev/null || echo "?")
  summary=$(python3 -c "import json,sys; d=json.load(open('$meta')); print(d.get('summary','')[:200])" 2>/dev/null || echo "")

  echo "--- $wid (reward=$reward) ---"
  echo "Summary: $summary"

  if [[ -d "$pdir/files" ]]; then
    upserts=$(find "$pdir/files" -type f 2>/dev/null | sort)
    n_upserts=$(echo -n "$upserts" | grep -c . || true)
    if [[ "$n_upserts" -gt 0 ]]; then
      echo "Upserts ($n_upserts files):"
      echo "$upserts" | while IFS= read -r f; do
        rel="${f#$pdir/files/}"
        proposed_lines=$(wc -l < "$f" 2>/dev/null | tr -d ' ')
        # If this is a SKILL.md, surface its description: frontmatter line —
        # critical for deciding "same workflow vs different sub-workflows".
        desc=""
        if [[ "$(basename "$f")" == "SKILL.md" ]]; then
          desc=$(awk '/^---/{f++; next} f==1 && /^description:/{sub(/^description:[[:space:]]*/,""); print; exit}' "$f" 2>/dev/null | head -c 200)
        fi
        if [[ -f "library/$rel" ]]; then
          lib_lines=$(wc -l < "library/$rel" 2>/dev/null | tr -d ' ')
          delta=$((proposed_lines - lib_lines))
          if [[ $delta -ge 0 ]]; then
            echo "  $rel ($proposed_lines lines, +$delta vs library)"
          else
            echo "  $rel ($proposed_lines lines, $delta vs library)"
          fi
        else
          echo "  $rel ($proposed_lines lines, new)"
        fi
        if [[ -n "$desc" ]]; then
          echo "    description: $desc"
        fi
      done
    else
      echo "Upserts: (none)"
    fi
  else
    echo "Upserts: (no files/ subdir)"
  fi

  deletes=$(python3 -c "import json; print('\n'.join(json.load(open('$meta')).get('delete_paths',[])))" 2>/dev/null)
  n_del=$(echo -n "$deletes" | grep -c . || true)
  if [[ "$n_del" -gt 0 ]]; then
    echo "Deletes ($n_del files):"
    echo "$deletes" | sed 's/^/  /'
  else
    echo "Deletes: (none)"
  fi
  echo
done

# Brief library overview at the end.
echo "=== Target library/ (the worker you're merging FOR) ==="
if [[ -d library ]]; then
  n_files=$(find library -type f 2>/dev/null | wc -l | tr -d ' ')
  n_skills=$(find library -name SKILL.md 2>/dev/null | wc -l | tr -d ' ')
  echo "$n_skills skill(s), $n_files total file(s)"
  find library -name SKILL.md 2>/dev/null | sort | sed 's|^library/||; s|/SKILL.md$||; s/^/  /'
else
  echo "(no library/ dir)"
fi

# Federation overview: which skills do peer libraries already have?
# Helps spot consensus (skill present in multiple peers) vs target-only skills.
if [[ -d peer_libraries ]]; then
  echo
  echo "=== Federation view: skills across peer_libraries/ ==="
  # For each peer dir, list its skills.
  for pdir in peer_libraries/*/; do
    [[ -d "$pdir" ]] || continue
    pname=$(basename "$pdir")
    pcount=$(find "$pdir" -name SKILL.md 2>/dev/null | wc -l | tr -d ' ')
    echo "--- $pname ($pcount skill(s)) ---"
    find "$pdir" -name SKILL.md 2>/dev/null | sort | sed "s|^$pdir||; s|/SKILL.md$||; s/^/  /"
  done
  # Cross-peer consensus: which skill paths appear in >1 library (target + peers)?
  echo
  echo "--- consensus skills (present in target + peers) ---"
  python3 -c "
from pathlib import Path
from collections import defaultdict
counts = defaultdict(set)
# Include target as 'target' label.
for path in Path('library').rglob('SKILL.md') if Path('library').is_dir() else []:
    rel = str(path.parent.relative_to('library'))
    counts[rel].add('target')
for pdir in Path('peer_libraries').iterdir() if Path('peer_libraries').is_dir() else []:
    if not pdir.is_dir(): continue
    for path in pdir.rglob('SKILL.md'):
        rel = str(path.parent.relative_to(pdir))
        counts[rel].add(pdir.name)
for skill, holders in sorted(counts.items()):
    if len(holders) > 1:
        print(f'  {skill}  [{\",\".join(sorted(holders))}]')
" 2>/dev/null || echo "  (could not compute consensus)"
fi

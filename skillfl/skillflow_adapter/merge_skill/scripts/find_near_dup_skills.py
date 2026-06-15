#!/usr/bin/env python3
"""Pre-scan a skill library for near-duplicate skill directory names.

Use this BEFORE deciding whether to absorb peer patches that may introduce
their own naming variants — and BEFORE writing DONE.txt as a final
sanity check on the merged result.

Usage:
    python3 find_near_dup_skills.py <library_dir>

Output: pairs of skill dirs whose names look like the same skill under
different spellings (hyphen / underscore / order / minor word variants).
"""

from __future__ import annotations

import re
import sys
from difflib import SequenceMatcher
from pathlib import Path


def normalize(name: str) -> str:
    """Lowercase, collapse separators, drop trailing version digits."""
    s = name.lower()
    s = re.sub(r"[-_./]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    # Drop trailing numeric suffixes ("skill-foo-2" → "skill-foo")
    s = re.sub(r"\s+\d+$", "", s)
    # Drop "v\d+" suffix.
    s = re.sub(r"\s+v\d+$", "", s)
    return s


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def read_description(skill_md: Path) -> str:
    """Pull the `description:` line out of YAML frontmatter, return ''."""
    try:
        text = skill_md.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    in_fm = False
    for line in text.splitlines():
        if line.strip() == "---":
            if not in_fm:
                in_fm = True
                continue
            else:
                break
        if in_fm and line.lower().startswith("description:"):
            return line.split(":", 1)[1].strip().strip('"').strip("'")[:200]
    return ""


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    root = Path(sys.argv[1])
    if not root.is_dir():
        print(f"ERROR: {root} is not a directory", file=sys.stderr)
        return 1

    # A "skill" = any directory containing SKILL.md.
    skills: list[tuple[str, str, str]] = []  # (rel_path, normalized_name, description)
    for p in root.rglob("SKILL.md"):
        skill_dir = p.parent
        rel = skill_dir.relative_to(root)
        skills.append((str(rel), normalize(skill_dir.name), read_description(p)))

    if len(skills) < 2:
        print(f"Only {len(skills)} skill(s) — no duplicates possible.")
        return 0

    threshold_high = 0.85   # very likely duplicates by name
    threshold_med = 0.70    # possibly duplicates by name
    near_dups: list[tuple[str, str, float, str, str]] = []

    for i in range(len(skills)):
        for j in range(i + 1, len(skills)):
            rel_i, norm_i, desc_i = skills[i]
            rel_j, norm_j, desc_j = skills[j]
            sim = similarity(norm_i, norm_j)
            if sim >= threshold_med:
                near_dups.append((rel_i, rel_j, sim, desc_i, desc_j))

    if not near_dups:
        print(f"OK — {len(skills)} skill(s), no near-duplicates above threshold.")
        return 0

    near_dups.sort(key=lambda t: -t[2])
    print(f"Found {len(near_dups)} name-similar pair(s) (across {len(skills)} skills):\n")
    print(
        "NOTE: name similarity does NOT mean semantic duplication. Read the\n"
        "      `description:` fields below — if they describe DIFFERENT\n"
        "      sub-workflows (e.g. 'OCR for product labels' vs 'OCR for\n"
        "      shipping orders'), KEEP THEM SEPARATE. Only consolidate\n"
        "      when descriptions agree on the same end-to-end workflow.\n"
    )
    for a, b, sim, da, db in near_dups:
        flag = "NAME-SIM HIGH" if sim >= threshold_high else "NAME-SIM MED "
        print(f"  [{flag}, name-sim={sim:.2f}]")
        print(f"      {a}")
        print(f"        description: {da or '(none)'}")
        print(f"      {b}")
        print(f"        description: {db or '(none)'}")
    print()
    print("Action: read both descriptions; consolidate ONLY if same workflow.")
    print("        If descriptions describe different sub-tasks, leave both.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

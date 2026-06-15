#!/usr/bin/env python3
"""Validate every SKILL.md in a library directory before declaring DONE.

Checks each SKILL.md for:
  1. File parses (UTF-8 readable).
  2. If frontmatter present (--- ... ---), it's valid YAML.
  3. Frontmatter has `name` (required) and `description` (recommended).
  4. Skill directory name matches frontmatter.name (case- and separator-
     insensitive: `vaxcrate-dispatch` matches `name: Vaxcrate Dispatch`).
  5. File length ≤ 500 lines (SkillFlow soft cap).

Run before writing DONE.txt to catch your own edits going wrong.

Usage:
    python3 validate_skill_md.py <library_dir>
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

try:
    import yaml  # type: ignore
    HAS_YAML = True
except ImportError:
    HAS_YAML = False


MAX_LINES = 500
FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def normalize(s: str) -> str:
    """Lowercase + strip non-alnum so 'Vaxcrate Dispatch' == 'vaxcrate-dispatch'."""
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def parse_frontmatter(text: str) -> tuple[dict | None, str | None]:
    """Returns (parsed_dict, error_or_none). dict is None if no frontmatter."""
    m = FRONTMATTER_RE.match(text)
    if not m:
        return None, None  # No frontmatter at all (allowed but discouraged).
    body = m.group(1)
    if not HAS_YAML:
        # Best-effort line-by-line parse if PyYAML missing.
        out: dict = {}
        for line in body.splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                out[k.strip()] = v.strip().strip('"').strip("'")
        return out, None
    try:
        parsed = yaml.safe_load(body)
        if not isinstance(parsed, dict):
            return None, "frontmatter is not a mapping"
        return parsed, None
    except yaml.YAMLError as e:
        return None, f"YAML parse error: {e}"


def validate_file(skill_md: Path, library_root: Path) -> list[str]:
    issues: list[str] = []
    rel = skill_md.relative_to(library_root)
    skill_dir_name = skill_md.parent.name
    try:
        text = skill_md.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        issues.append(f"{rel}: cannot read ({type(e).__name__}: {e})")
        return issues

    n_lines = text.count("\n")
    if n_lines > MAX_LINES:
        issues.append(f"{rel}: {n_lines} lines exceeds soft cap of {MAX_LINES}")

    fm, fm_err = parse_frontmatter(text)
    if fm_err:
        issues.append(f"{rel}: {fm_err}")
        return issues

    if fm is None:
        issues.append(f"{rel}: no YAML frontmatter (expected `---\\nname: ...\\n---`)")
        return issues

    if "name" not in fm or not fm.get("name"):
        issues.append(f"{rel}: frontmatter missing `name`")
    else:
        if normalize(str(fm["name"])) != normalize(skill_dir_name):
            issues.append(
                f"{rel}: frontmatter name='{fm['name']}' doesn't match dir '{skill_dir_name}'"
            )

    if "description" not in fm or not fm.get("description"):
        issues.append(f"{rel}: frontmatter missing `description` (recommended)")

    return issues


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    root = Path(sys.argv[1])
    if not root.is_dir():
        print(f"ERROR: {root} not a directory")
        return 1

    skill_files = sorted(root.rglob("SKILL.md"))
    if not skill_files:
        print(f"OK — no SKILL.md files in {root} (empty library).")
        return 0

    all_issues: list[str] = []
    for f in skill_files:
        all_issues.extend(validate_file(f, root))

    if not all_issues:
        print(f"OK — {len(skill_files)} SKILL.md file(s) all valid.")
        return 0

    print(f"Found {len(all_issues)} issue(s) across {len(skill_files)} SKILL.md file(s):")
    for it in all_issues:
        print(f"  - {it}")
    print()
    print("Fix these before writing DONE.txt — invalid SKILL.md files won't")
    print("be discoverable as skills by future agents.")
    return 1  # nonzero so agent sees there are issues to fix


if __name__ == "__main__":
    sys.exit(main())

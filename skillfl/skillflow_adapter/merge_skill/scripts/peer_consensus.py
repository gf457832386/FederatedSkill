#!/usr/bin/env python3
"""Identify which paths multiple peers want to touch (conflict hotspots) vs
which paths only one peer touches (independent contributions).

Use this BEFORE inspecting individual diffs — knowing where workers overlap
helps you focus your token budget on real conflicts and quickly absorb the
independent contributions.

Usage:
    python3 peer_consensus.py [patches_dir]   # default: patches/

Reads each `<patches_dir>/<wid>/meta.json` and `<wid>/files/` tree to compute
which paths each worker touches. Output groups paths by the count of workers
touching them.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path


def main() -> int:
    patches_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "patches")
    if not patches_dir.is_dir():
        print(f"ERROR: {patches_dir} not a directory")
        return 1

    # path → {worker_id → "upsert" | "delete"}
    touched: dict[str, dict[str, str]] = defaultdict(dict)

    for wdir in sorted(patches_dir.iterdir()):
        if not wdir.is_dir():
            continue
        wid = wdir.name
        # Upserts
        files_dir = wdir / "files"
        if files_dir.is_dir():
            for f in files_dir.rglob("*"):
                if f.is_file():
                    rel = str(f.relative_to(files_dir))
                    touched[rel][wid] = "upsert"
        # Deletes
        meta = wdir / "meta.json"
        if meta.is_file():
            try:
                d = json.loads(meta.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            for p in d.get("delete_paths") or []:
                if isinstance(p, str) and p:
                    touched[p][wid] = "delete"

    if not touched:
        print("No paths touched by any patch. (All workers' patches are empty?)")
        return 0

    # Group by number of workers touching the path.
    by_count: dict[int, list[tuple[str, dict[str, str]]]] = defaultdict(list)
    for path, ws in touched.items():
        by_count[len(ws)].append((path, ws))

    print(f"Total touched paths: {len(touched)}")
    print()

    # Hot first: highest worker count → lowest.
    for count in sorted(by_count.keys(), reverse=True):
        items = sorted(by_count[count])
        if count >= 2:
            label = f"CONFLICT HOTSPOT — {count} workers touch this path"
        else:
            label = f"Independent contributions — only {count} worker"
        print(f"=== {label} ({len(items)} path(s)) ===")
        for path, ws in items:
            actions = ", ".join(f"{wid}→{act}" for wid, act in sorted(ws.items()))
            print(f"  {path}")
            print(f"    {actions}")
        print()

    # One-line takeaway.
    n_conflicts = sum(len(v) for k, v in by_count.items() if k >= 2)
    n_indep = sum(len(v) for k, v in by_count.items() if k == 1)
    print(f"Summary: {n_conflicts} conflict hotspot(s), {n_indep} independent contribution(s).")
    if n_conflicts > 0:
        print("Recommendation: resolve hotspots first (read each peer's version), then")
        print("                absorb independent contributions in bulk.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

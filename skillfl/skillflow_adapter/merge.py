"""Merging M workers' patches into one effective patch.

The patch representation here is SkillFlow's file-level upsert/delete form
(see SkillFlow-benchmark/libs/skill_evolution/patcher.py::SkillPatchResult).
An extension path to SkillFL's 6-op section-level patches would live behind the
same `PatchMerger` ABC — it would just operate on a different `WorkerPatch`
payload shape.

Merge semantics (default RewardWeightedFileMerge):
- A "path" is `upsert_files.key` or an entry in `delete_paths`.
- For each path, look at every worker's proposed action (upsert / delete /
  absent):
    * If one worker upserts and all others are absent → take the upsert
    * If all non-absent workers delete → delete
    * Otherwise (mixed upserts, or upsert-vs-delete conflict) → the proposing
      worker with the highest reward wins, lex tie-break by worker_id
- Empty upsert content is treated as non-proposal (skip).

This mirrors SkillFL's deterministic reward-weighted merge but at the file
level instead of the op level. Aside from the tie-break rule it is purely
deterministic given the same inputs.
"""

from __future__ import annotations

import json
import shutil
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal


def safe_rel_path(rel: str | None) -> str | None:
    """Validate `rel` as a safe relative path inside a sandbox dir.

    Rejects absolute paths (POSIX `/...`, Windows `C:\\...`, drive-letter
    `C:foo`), empty strings, and paths whose components contain `..`, `.`,
    or empty pieces. Normalizes backslashes to forward slashes, strips a
    single leading `./`. Returns the cleaned path, or None if unsafe.

    Used at every trust boundary where path strings cross from untrusted
    sources (LLM output, sandbox filesystem traversal, peer patches) into
    runner-side filesystem writes.
    """
    if not isinstance(rel, str):
        return None
    s = rel.strip()
    if not s:
        return None
    # Reject leading separators (POSIX absolute) up front, before any
    # normalization, so `/etc/passwd` can't sneak through as `etc/passwd`.
    if s.startswith(("/", "\\")):
        return None
    # Normalize backslashes to forward slashes (so callers can pass
    # mixed-separator paths from Windows-y sources).
    s = s.replace("\\", "/")
    # Strip a single leading "./" — no more, no less.
    if s.startswith("./"):
        s = s[2:]
    if not s:
        return None
    p = Path(s)
    if p.is_absolute():
        return None
    parts = p.parts
    if not parts:
        return None
    for part in parts:
        if part in ("", "..", "."):
            return None
        # Windows drive prefix like "C:" or "C:foo".
        if len(part) >= 2 and part[1] == ":":
            return None
    return str(p)


@dataclass
class WorkerPatch:
    """One worker's proposed change set for a single sync round."""
    worker_id: str
    reward: float
    # File-level upsert/delete form (SkillFlow's SkillPatchResult shape).
    upsert_files: dict[str, str] = field(default_factory=dict)
    delete_paths: list[str] = field(default_factory=list)
    # Opaque context the merger may log but not act on.
    summary: str = ""


@dataclass
class MergedPatch:
    """Result of merging M WorkerPatches."""
    upsert_files: dict[str, str] = field(default_factory=dict)
    delete_paths: list[str] = field(default_factory=list)
    # Per-path provenance: path → (action, winning_worker_id, winning_reward).
    provenance: dict[str, tuple[Literal["upsert", "delete"], str, float]] = field(default_factory=dict)
    # Paths where workers disagreed; kept for logging/debugging.
    conflicts: list[str] = field(default_factory=list)
    # One-sentence description of the merge decision; for CloudSkillMerge this
    # is the LLM's own summary, for deterministic mergers it stays empty.
    summary: str = ""
    # USD cost of the merge LLM/agent calls that produced this patch.
    # 0.0 for deterministic mergers (no LLM); populated by CloudSkillMerge (parsed from
    # claude-code stream-json `total_cost_usd`) and PerWorkerSkillMerge (parsed from
    # claude-code stream-json `total_cost_usd`). The runner writes this into
    # `merged_patch.json` so post-hoc analysis can sum merger spend per round
    # / per family without re-instrumenting.
    cost_usd: float = 0.0


class PatchMerger(ABC):
    """Combine M worker patches into one effective patch.

    `existing_library` is an optional snapshot of the shared skill library
    BEFORE the workers ran (a {rel_path: content} dict, like the runner's
    `_snapshot_skill_dir()` output). Mergers that can use it (e.g.
    CloudSkillMerge) will use it to spot cross-round semantic duplicates;
    mergers that don't need it just ignore it.
    """

    @abstractmethod
    def merge(
        self,
        patches: list[WorkerPatch],
        *,
        existing_library: dict[str, str] | None = None,
        self_worker_id: str | None = None,
    ) -> MergedPatch:
        ...

    def merge_per_worker(
        self,
        patches: list[WorkerPatch],
        *,
        libraries: dict[str, dict[str, str]],
        round_idx: int | None = None,
        round_dir: Path | None = None,
    ) -> dict[str, MergedPatch]:
        """Unshared-library variant: produce one MergedPatch per worker.

        The merger is invoked once per worker. Each call sees:
          - that worker's pre-task library snapshot (in `libraries[wid]`),
            and
          - ALL M worker patches (own + peers') — so the merger can decide,
            from a global view of patches, what worker `wid`'s next-round
            library should look like.

        `round_idx` / `round_dir` are round-level metadata for mergers that
        materialize per-round artifacts (e.g. `CloudSkillMerge` builds
        sandboxes under round_dir/cloud_skill_merge_sandboxes). Mergers
        without on-disk artifacts ignore both.

        Default implementation: invoke `merge()` once per worker on the full
        patch list. Subclasses (e.g. CloudSkillMerge) override when they
        need access to round_idx / round_dir.
        """
        del round_idx, round_dir
        out: dict[str, MergedPatch] = {}
        for wid, lib in libraries.items():
            out[wid] = self.merge(patches, existing_library=lib, self_worker_id=wid)
        return out

    def __repr__(self) -> str:
        return f"{type(self).__name__}()"


class RewardWeightedFileMerge(PatchMerger):
    """Default merger: per-path, highest-reward proposer wins.

    Tie-breaking rule for equal rewards:
    - 'upsert' beats 'delete' (we prefer keeping content)
    - Then lex-smaller worker_id wins
    """

    def merge(
        self,
        patches: list[WorkerPatch],
        *,
        existing_library: dict[str, str] | None = None,  # ignored
        self_worker_id: str | None = None,  # ignored
    ) -> MergedPatch:
        # Build a per-path proposal map: path → list[(action, worker_id, reward, content)]
        proposals: dict[str, list[tuple[str, str, float, str]]] = {}
        for p in patches:
            for path, content in p.upsert_files.items():
                if content is None or content == "":
                    # Empty upsert == no proposal.
                    continue
                proposals.setdefault(path, []).append(
                    ("upsert", p.worker_id, p.reward, content)
                )
            for path in p.delete_paths:
                proposals.setdefault(path, []).append(
                    ("delete", p.worker_id, p.reward, "")
                )

        merged = MergedPatch()
        for path, ps in proposals.items():
            # Check if there's any disagreement on action for this path.
            actions = {entry[0] for entry in ps}
            if len(actions) > 1 or len({entry[1] for entry in ps}) > 1:
                # "Conflict" means >1 worker touched this path, even if agreeing
                # — we log it. True action-level disagreement is a subset.
                merged.conflicts.append(path)

            # Winner selection. Sort key:
            #   (-reward, action_rank, worker_id)
            # where action_rank = 0 for upsert, 1 for delete (so upsert wins at equal reward).
            def sort_key(entry: tuple[str, str, float, str]) -> tuple:
                action, wid, reward, _ = entry
                return (-reward, 0 if action == "upsert" else 1, wid)

            winner = min(ps, key=sort_key)
            action, wid, reward, content = winner
            merged.provenance[path] = (action, wid, reward)  # type: ignore[assignment]
            if action == "upsert":
                merged.upsert_files[path] = content
            else:
                merged.delete_paths.append(path)

        # Stable-sort delete_paths for determinism.
        merged.delete_paths.sort()
        return merged




# ---------------------------------------------------------------------------
# Agent-runner contracts and host/container runner factories
# ---------------------------------------------------------------------------

# Contract for a function that runs an agent in a sandbox dir until it writes
# `DONE.txt` (or its budget runs out). Should raise on agent crash / timeout
# *that left no DONE.txt*; returning normally without DONE.txt also counts as
# failure (the merger itself checks for DONE.txt presence).
#
#   sandbox_dir:    workspace; agent's cwd.
#   prompt:         initial user message to seed the agent.
#   model_name:     model the agent should use (passed through to claude-code).
#   max_turns:      cap on agent turns (advisory; CLI doesn't enforce).
#   wall_clock_sec: hard wall-clock cap.
#   env:            optional dict of env vars for the subprocess (model creds).
AgentRunner = Callable[..., None]


def make_claude_code_subprocess_runner(
    *,
    claude_bin: str = "claude",
    extra_args: list[str] | None = None,
) -> AgentRunner:
    """Host-mode `AgentRunner` that spawns the `claude` CLI directly as a
    subprocess rooted in `sandbox_dir`. Requires `claude` on PATH.

    Use `make_podman_claude_runner` when claude isn't installed on the host
    but a harbor-prebuilt image with claude is available.

    Note: claude has no `--max-turns` flag; the soft turn cap is enforced
    by the agent following SKILL.md, the hard cap is `wall_clock_sec` via
    subprocess.timeout.
    """
    import os
    import subprocess

    extra = list(extra_args or [])

    def _run(
        *,
        sandbox_dir: Path,
        prompt: str,
        model_name: str,
        max_turns: int,
        wall_clock_sec: int,
        env: dict[str, str] | None = None,
    ) -> None:
        del max_turns  # claude CLI does not expose a turn cap; SKILL.md soft-caps.
        merged_env = os.environ.copy()
        if env:
            merged_env.update({k: str(v) for k, v in env.items() if v is not None})
        cmd = [
            claude_bin,
            "--print",
            "--model", model_name,
            "--dangerously-skip-permissions",
            *extra,
        ]
        try:
            subprocess.run(
                cmd,
                cwd=str(sandbox_dir),
                env=merged_env,
                input=prompt,
                text=True,
                capture_output=True,
                timeout=wall_clock_sec,
                check=True,
            )
        except FileNotFoundError as e:
            raise RuntimeError(
                f"claude binary not found ({claude_bin!r}); "
                f"install claude-code or use make_podman_claude_runner"
            ) from e
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(
                f"claude subprocess timed out after {wall_clock_sec}s"
            ) from e
        except subprocess.CalledProcessError as e:
            tail = (e.stderr or "")[-400:] if isinstance(e.stderr, str) else ""
            raise RuntimeError(
                f"claude subprocess exited {e.returncode}: {tail}"
            ) from e

    return _run


def make_podman_claude_runner(
    *,
    image: str,
    podman_bin: str = "podman",
    extra_run_args: list[str] | None = None,
) -> AgentRunner:
    """Container-mode `AgentRunner` that spawns the harbor-prebuilt image
    and runs `claude --print` against the sandbox bind-mounted at /workspace.

    Why this exists: when claude isn't on the host, we reuse the same image
    the worker trials run in (which already has claude + node preinstalled
    at /root/.local/bin/claude). Sandbox dir is mounted into the container,
    claude edits library/ in /workspace, podman rootless mapping makes the
    output files appear back on host as the host user.

    `image` is the OCI image tag (e.g. "localhost/harbor-prebuilt:task-XXXX").
    Any harbor-prebuilt tag works — they all carry the same claude install.

    `IS_SANDBOX=1` is set automatically because `--dangerously-skip-permissions`
    refuses to run as root unless this env tells it the runtime is sandboxed.
    Inside a fresh disposable container that's accurate.

    Stream-json output is captured to `<sandbox>/claude-code.txt`; the merger
    later greps the final result line for `total_cost_usd` so we can attribute
    spend to each merge call (matches how worker trials log cost).
    """
    import subprocess

    extra = list(extra_run_args or [])

    def _run(
        *,
        sandbox_dir: Path,
        prompt: str,
        model_name: str,
        max_turns: int,
        wall_clock_sec: int,
        env: dict[str, str] | None = None,
    ) -> None:
        del max_turns  # see make_claude_code_subprocess_runner.
        cmd = [
            podman_bin, "run", "--rm", "-i",
            "--network", "host",
            "-v", f"{sandbox_dir}:/workspace:rw",
            "-w", "/workspace",
            "-e", "IS_SANDBOX=1",
        ]
        if env:
            for k, v in env.items():
                if v is None:
                    continue
                cmd.extend(["-e", f"{k}={v}"])
        cmd.extend(extra)
        cmd.append(image)
        cmd.extend([
            "/root/.local/bin/claude",
            "--print",
            "--model", model_name,
            "--dangerously-skip-permissions",
            "--verbose",                 # required by stream-json + --print
            "--output-format", "stream-json",
        ])
        # Stream the agent's stream-json output directly to a log file at the
        # sandbox root so we can post-hoc parse cost / turn counts. Stderr stays
        # in memory for error reporting; it's small.
        log_path = sandbox_dir / "claude-code.txt"
        try:
            with log_path.open("w", encoding="utf-8") as out_f:
                subprocess.run(
                    cmd,
                    input=prompt,
                    text=True,
                    stdout=out_f,
                    stderr=subprocess.PIPE,
                    timeout=wall_clock_sec,
                    check=True,
                )
        except FileNotFoundError as e:
            raise RuntimeError(
                f"{podman_bin!r} not found; install podman or pass podman_bin=docker"
            ) from e
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(
                f"claude container timed out after {wall_clock_sec}s"
            ) from e
        except subprocess.CalledProcessError as e:
            tail = (e.stderr or "")[-400:] if isinstance(e.stderr, str) else ""
            raise RuntimeError(
                f"claude container exited {e.returncode}: {tail}"
            ) from e

    return _run


def make_podman_codex_runner(
    *,
    image: str,
    podman_bin: str = "podman",
    extra_run_args: list[str] | None = None,
) -> AgentRunner:
    """Container-mode `AgentRunner` that spawns the harbor-prebuilt image
    and runs `codex exec` against the sandbox bind-mounted at /workspace.

    Parallels `make_podman_claude_runner` but uses OpenAI's Codex CLI instead
    of claude-code. Codex talks to OpenAI's Responses API natively, so no
    Anthropic-protocol bridging / litellm shim is needed.

    Auth: Codex doesn't read `OPENAI_API_KEY` directly — it expects an auth
    file at `~/.codex/auth.json`, written by `codex login --with-api-key`
    (key piped from stdin). Since each merger call spawns a fresh disposable
    container, we run login + exec inline in the same `sh -c '...'` step.
    The container's `$HOME` is /root, so /root/.codex/auth.json is written
    each time and discarded when the container exits.

    The prompt is piped to codex via stdin (codex exec reads stdin when the
    PROMPT positional is omitted or `-`). JSONL output goes to stdout, which
    we capture to `<sandbox>/codex-cli.txt` for post-hoc inspection.

    The `env` dict is expected to contain `OPENAI_API_KEY` (or whatever the
    cli.py caller sets for codex's `merger_env`); other keys are passed
    through unchanged. Unlike the claude runner, there's no ANTHROPIC_*
    rewriting / localhost retry-proxy — codex retries internally.
    """
    import subprocess

    extra = list(extra_run_args or [])

    def _run(
        *,
        sandbox_dir: Path,
        prompt: str,
        model_name: str,
        max_turns: int,
        wall_clock_sec: int,
        env: dict[str, str] | None = None,
    ) -> None:
        del max_turns  # codex exec has no per-call turn cap; rely on wall_clock_sec.
        cmd = [
            podman_bin, "run", "--rm", "-i",
            "--network", "host",
            "-v", f"{sandbox_dir}:/workspace:rw",
            "-w", "/workspace",
        ]
        if env:
            for k, v in env.items():
                if v is None:
                    continue
                cmd.extend(["-e", f"{k}={v}"])
        cmd.extend(extra)
        cmd.append(image)
        # sh -c so we can do `codex login` then `codex exec` in one container
        # run. PROMPT is piped via stdin to login (just key) then exec (prompt).
        # We split using a sentinel between them: a small wrapper script
        # written to /tmp inline.
        cmd.extend([
            "sh", "-c",
            "set -e; "
            "printf '%s' \"$OPENAI_API_KEY\" | /root/.local/bin/codex login --with-api-key >/dev/null && "
            "cat /tmp/codex_prompt.txt | /root/.local/bin/codex exec "
            "  --skip-git-repo-check "
            "  --dangerously-bypass-approvals-and-sandbox "
            "  -s danger-full-access "
            f"  -m {model_name} "
            "  -C /workspace "
            "  --json ",
        ])
        # Codex needs the prompt at /tmp/codex_prompt.txt inside the container;
        # easiest is to drop it on the sandbox and let the sh -c read from it.
        # But sandbox is mounted at /workspace, and dropping in /workspace
        # would pollute the merge area. Instead: bake the prompt write into
        # the sh -c via a heredoc piped through stdin, or write it next to
        # the cmd. Simplest: pipe prompt as the container's stdin to a
        # tee-then-feed-codex chain.
        # Implementation: rewrite cmd to use stdin pipeline.
        cmd[-1] = (
            "set -e; "
            "cat > /tmp/codex_prompt.txt; "
            "printf '%s' \"$OPENAI_API_KEY\" | /root/.local/bin/codex login --with-api-key >/dev/null && "
            "/root/.local/bin/codex exec "
            "  --skip-git-repo-check "
            "  --dangerously-bypass-approvals-and-sandbox "
            "  -s danger-full-access "
            f"  -m {model_name} "
            "  -C /workspace "
            "  --json "
            "  - < /tmp/codex_prompt.txt"
        )
        log_path = sandbox_dir / "codex-cli.txt"
        try:
            with log_path.open("w", encoding="utf-8") as out_f:
                subprocess.run(
                    cmd,
                    input=prompt,
                    text=True,
                    stdout=out_f,
                    stderr=subprocess.PIPE,
                    timeout=wall_clock_sec,
                    check=True,
                )
        except FileNotFoundError as e:
            raise RuntimeError(
                f"{podman_bin!r} not found; install podman or pass podman_bin=docker"
            ) from e
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(
                f"codex container timed out after {wall_clock_sec}s"
            ) from e
        except subprocess.CalledProcessError as e:
            tail = (e.stderr or "")[-400:] if isinstance(e.stderr, str) else ""
            raise RuntimeError(
                f"codex container exited {e.returncode}: {tail}"
            ) from e

    return _run


def parse_claude_code_cost(log_path: Path) -> float:
    """Extract `total_cost_usd` from a claude-code stream-json log.

    The agent emits exactly one `{"type":"result", ...}` line at the end of a
    successful run; that line has `total_cost_usd: <number>`. Returns 0.0 if
    the log is missing, malformed, or the cost field isn't present (which we
    treat as "no signal" rather than crashing).
    """
    if not log_path.is_file():
        return 0.0
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return 0.0
    last_cost = 0.0
    for line in text.splitlines():
        if '"type":"result"' not in line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        v = obj.get("total_cost_usd")
        if isinstance(v, (int, float)):
            last_cost = float(v)
    return last_cost


# ---------------------------------------------------------------------------
# Cloud-side skill-based merge
# ---------------------------------------------------------------------------


class CloudSkillMerge(PatchMerger):
    """Cloud-side merger that runs ONE fixed model as a claude-code skill agent
    on behalf of each worker.

    Setting (personalized FL, unshared libraries):
      - Each worker `u_i` has its own private library (no sharing across
        workers; libraries diverge over rounds).
      - On sync, the cloud invokes a single fixed merger model N times — once
        per worker. Each invocation sees that worker's pre-task library +
        ALL M patches (own + peers'), and decides what the worker's
        next-round library should be.

    The merger model is shared across workers (cloud is one entity), in
    contrast to the deleted PerWorkerSkillMerge where each worker self-merged
    with its own model. The framing in SKILL.md is "you are the cloud
    administrator merging on behalf of worker X".

    Sandbox layout per worker (built at <sandbox_root>/<wid>/):
        meta.json              {target_worker, target_model, target_cli, family, round, peers, all_workers}
        patches/
            u0/{meta.json, files/}
            u1/{...}
            u2/{...}
        library/               ← target worker's library, editable
        .baseline_library/     ← read-only snapshot
        scripts/               ← helper scripts copied from skill_dir
        MERGE_TASK.md          ← copy of SKILL.md
    Agent writes DONE.txt at completion. Stream-json output is captured to
    claude-code.txt at the sandbox root for cost / debugging.

    Three-tier fallback: skill agent → `fallback.merge_per_worker` (typically
    CloudSkillMerge) → fallback's own fallback (deterministic).
    """

    def __init__(
        self,
        *,
        agent_runner: AgentRunner,
        merger_model: str,
        skill_dir: Path,
        default_sandbox_root: Path,
        family: str,
        fallback: PatchMerger,
        merger_env: dict[str, str] | None = None,
        worker_models: dict[str, str] | None = None,
        worker_clis: dict[str, str] | None = None,
        memory_root: Path | None = None,
        max_turns: int = 30,
        wall_clock_sec: int = 600,
        task_update_skill_dir: Path | None = None,
        task_update_max_turns: int = 10,
        task_update_wall_clock_sec: int = 300,
    ) -> None:
        if not merger_model:
            raise ValueError("CloudSkillMerge requires a non-empty merger_model")
        skill_md = Path(skill_dir) / "SKILL.md"
        if not skill_md.is_file():
            raise ValueError(f"skill_dir missing SKILL.md: {skill_md}")
        self.agent_runner = agent_runner
        self.merger_model = merger_model
        self.merger_env = dict(merger_env or {})
        # Optional: tells the merger which model each worker is using, so the
        # cloud agent can personalize content to that model's strengths. Not
        # required — without it the agent gets worker IDs only.
        self.worker_models = dict(worker_models or {})
        # Optional companion to worker_models: which CLI agent each worker
        # drives (claude-code / qwen-code / kimi-cli / ...). The merger uses
        # this to tailor SKILL.md style to the CLI's tool-use scaffold —
        # e.g., scripting explicit verification for CLIs that don't auto-verify.
        self.worker_clis = dict(worker_clis or {})
        self.skill_dir = Path(skill_dir)
        self.default_sandbox_root = Path(default_sandbox_root)
        self.family = family
        self.fallback = fallback
        # memory_root persists per-worker `memory.md` across rounds. Layout:
        #   <memory_root>/<wid>/memory.md         — per-worker private memory
        #   <memory_root>/task_memory.md          — family-shared task understanding
        # If None, memory is not persisted (each round starts fresh — equivalent
        # to no cross-round memory; useful for unit tests).
        self.memory_root = Path(memory_root) if memory_root else None
        self.max_turns = max_turns
        self.wall_clock_sec = wall_clock_sec
        # Optional task-update step: if a skill_dir is provided here, the
        # merger runs an extra pre-step before per-worker merges. The step
        # reads all M patches and updates a family-shared task_memory.md.
        # Each per-worker merge then sees that task_memory.md as read-only
        # context. None disables the step (back-compat with v4.x runs).
        self.task_update_skill_dir = (
            Path(task_update_skill_dir) if task_update_skill_dir else None
        )
        if self.task_update_skill_dir is not None:
            tu_skill_md = self.task_update_skill_dir / "SKILL.md"
            if not tu_skill_md.is_file():
                raise ValueError(
                    f"task_update_skill_dir missing SKILL.md: {tu_skill_md}"
                )
        self.task_update_max_turns = task_update_max_turns
        self.task_update_wall_clock_sec = task_update_wall_clock_sec
        # Set per call by merge_per_worker, consulted by _merge_one.
        self._cur_sandbox_root: Path = self.default_sandbox_root
        self._cur_round_idx: int = 0

    # -- ABC ---------------------------------------------------------------

    def merge(
        self,
        patches: list[WorkerPatch],
        *,
        existing_library: dict[str, str] | None = None,
        self_worker_id: str | None = None,
    ) -> MergedPatch:
        """Single-worker entrypoint. Runner uses merge_per_worker; this is
        here only to satisfy the ABC and for unit-test convenience."""
        wid = self_worker_id or "u0"
        out = self.merge_per_worker(
            patches, libraries={wid: existing_library or {}}
        )
        return out[wid]

    def merge_per_worker(
        self,
        patches: list[WorkerPatch],
        *,
        libraries: dict[str, dict[str, str]],
        round_idx: int | None = None,
        round_dir: Path | None = None,
    ) -> dict[str, MergedPatch]:
        self._cur_round_idx = round_idx if round_idx is not None else 0
        if round_dir is not None:
            self._cur_sandbox_root = Path(round_dir) / "cloud_skill_merge_sandboxes"
        else:
            self._cur_sandbox_root = self.default_sandbox_root
        # Step 1 (optional): update family-shared task_memory.md from this
        # round's patches before any per-worker merge runs. Failure here is
        # non-fatal — per-worker merges fall back to whatever task_memory.md
        # already exists (or no task_memory at all on round 0).
        if self.task_update_skill_dir is not None:
            try:
                self._update_family_task_memory(patches, libraries)
            except Exception as e:
                # Don't crash the round just because task update failed;
                # log via the conflicts on each merged patch downstream.
                self._task_update_error = (
                    f"__task_update_failed__:{type(e).__name__}:{e}"
                )
            else:
                self._task_update_error = None
        else:
            self._task_update_error = None
        # Step 2: per-worker merges, run in parallel.
        # Each `_merge_one` shells out to its own podman container with its
        # own sandbox dir (u0/u1/u2/) — no shared mutable state, no FS
        # collisions. The work is I/O-bound (waiting on agent-loop subprocess),
        # so ThreadPoolExecutor is the right primitive.
        import concurrent.futures
        out: dict[str, MergedPatch] = {}
        wids = list(libraries.keys())
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=max(1, len(wids))
        ) as ex:
            futures = {
                ex.submit(
                    self._merge_one, wid, patches, libraries[wid], libraries
                ): wid
                for wid in wids
            }
            for fut in concurrent.futures.as_completed(futures):
                wid = futures[fut]
                try:
                    out[wid] = fut.result()
                except Exception as e:
                    fb = self.fallback.merge(
                        patches, existing_library=libraries[wid],
                        self_worker_id=wid,
                    )
                    fb.conflicts.append(
                        f"__cloud_skill_fatal__:{type(e).__name__}:{e}"
                    )
                    out[wid] = fb
        # Append task_update_error after all merges finish, deterministic order.
        if self._task_update_error:
            for wid in wids:
                if out.get(wid) is not None:
                    out[wid].conflicts.append(self._task_update_error)
        return out

    # -- internals ---------------------------------------------------------

    def _task_memory_path(self) -> Path | None:
        if self.memory_root is None:
            return None
        return self.memory_root / "task_memory.md"

    @staticmethod
    def _extract_skill_descriptions(
        library: dict[str, str],
    ) -> list[tuple[str, str]]:
        """Pull (name, description) pairs from each `<skill>/SKILL.md` in a
        flat library dict. Used to build library_skills.md for step 1
        without exposing full SKILL.md content."""
        out: list[tuple[str, str]] = []
        for rel, content in library.items():
            if not rel.endswith("/SKILL.md") and rel != "SKILL.md":
                continue
            if rel.startswith("__") or content == "<binary>":
                continue
            name = ""
            desc = ""
            in_frontmatter = False
            for line in content.splitlines():
                stripped = line.strip()
                if stripped == "---":
                    if in_frontmatter:
                        break
                    in_frontmatter = True
                    continue
                if not in_frontmatter:
                    continue
                if stripped.startswith("name:"):
                    name = stripped[len("name:"):].strip()
                elif stripped.startswith("description:"):
                    desc = stripped[len("description:"):].strip()
            if name:
                out.append((name, desc))
        return out

    def _update_family_task_memory(
        self,
        patches: list[WorkerPatch],
        libraries: dict[str, dict[str, str]],
    ) -> None:
        """Step 1 of the two-step pipeline. Runs the task_update skill agent
        in a sandbox containing patches/ + prior task_memory.md +
        library_skills.md (digest of every worker's pre-task library — name
        and description per skill). Agent rewrites task_memory.md in place;
        we persist it back so per-worker merges (and later rounds) read it."""
        assert self.task_update_skill_dir is not None
        sandbox = self._cur_sandbox_root / ".task_update"
        if sandbox.exists():
            shutil.rmtree(sandbox)
        sandbox.mkdir(parents=True, exist_ok=True)
        # meta.json — family-level, no target_worker (this step is shared).
        meta = {
            "family": self.family,
            "round": self._cur_round_idx,
            "all_workers": {
                p.worker_id: {
                    "model": self.worker_models.get(p.worker_id, "unknown"),
                    "cli":   self.worker_clis.get(p.worker_id, "unknown"),
                }
                for p in patches
            },
            "merger_model": self.merger_model,
        }
        (sandbox / "meta.json").write_text(
            json.dumps(meta, indent=2), encoding="utf-8"
        )
        # patches/<wid>/{meta.json, files/} — same shape as merge sandbox.
        patches_dir = sandbox / "patches"
        for p in patches:
            wdir = patches_dir / p.worker_id
            wdir.mkdir(parents=True, exist_ok=True)
            (wdir / "meta.json").write_text(
                json.dumps({
                    "worker_id": p.worker_id,
                    "reward": p.reward,
                    "summary": p.summary,
                    "delete_paths": list(p.delete_paths),
                }, indent=2),
                encoding="utf-8",
            )
            files_dir = wdir / "files"
            files_dir.mkdir(exist_ok=True)
            for rel, content in p.upsert_files.items():
                safe = safe_rel_path(rel)
                if safe is None:
                    continue
                target = files_dir / safe
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
        # task_memory.md — copy prior round's content if any, else stub.
        sandbox_mem = sandbox / "task_memory.md"
        prior = self._task_memory_path()
        if prior is not None and prior.is_file():
            shutil.copy2(prior, sandbox_mem)
        else:
            sandbox_mem.write_text(
                "# task_memory — round 0, no prior task understanding yet.\n"
                "# Populate this file based on the patches/ in cwd.\n",
                encoding="utf-8",
            )
        # library_skills.md — compact digest of every worker's pre-task
        # library: skill name + description only (NOT full SKILL.md). Lets
        # step 1 cross-reference "is this round's task already covered by
        # an existing skill?" without leaking full library content (which
        # would push step 1 toward library decisions). One block per
        # worker, listing each skill in that worker's pre-task library.
        digest_lines = [
            "# library_skills — what each worker's library currently has",
            "# (pre-task snapshot for this round; updated each round)",
            "",
        ]
        for wid in sorted(libraries.keys()):
            model = self.worker_models.get(wid, "unknown")
            lib = libraries[wid]
            skills = self._extract_skill_descriptions(lib)
            digest_lines.append(f"## {wid} (model: {model})")
            if not skills:
                digest_lines.append("- (no skills)")
            else:
                for name, desc in skills:
                    one_line_desc = " ".join(desc.split())
                    digest_lines.append(f"- {name}: {one_line_desc}")
            digest_lines.append("")
        (sandbox / "library_skills.md").write_text(
            "\n".join(digest_lines), encoding="utf-8"
        )
        # No full library, no peer_libraries, no scripts dir — task update
        # reasons about coverage from descriptions + reward signals alone.
        skill_body = (
            self.task_update_skill_dir / "SKILL.md"
        ).read_text(encoding="utf-8")
        prompt = (
            f"Tool-call rule: when invoking any tool, only include parameters "
            f"with concrete values you actually need. Never pass an optional "
            f"parameter with an empty string \"\" — omit it entirely. (E.g. "
            f"do not call Read with `pages: \"\"`.)\n\n"
            f"You are the cloud merger, step 1 (task update). "
            f"Family: {self.family}. Round: {self._cur_round_idx}.\n"
            f"Workspace = current directory.\n\n"
            f"=====================================================\n"
            f"=== SKILL: task-update (your task procedure) ===\n"
            f"=====================================================\n"
            f"{skill_body}\n\n"
            f"Begin. The step is complete when you have rewritten "
            f"task_memory.md and written DONE.txt at the sandbox root."
        )
        self.agent_runner(
            sandbox_dir=sandbox,
            prompt=prompt,
            model_name=self.merger_model,
            max_turns=self.task_update_max_turns,
            wall_clock_sec=self.task_update_wall_clock_sec,
            env=self.merger_env or None,
        )
        # Persist updated task_memory.md back to memory_root.
        if prior is not None and sandbox_mem.is_file():
            prior.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(sandbox_mem, prior)

    def _merge_one(
        self,
        wid: str,
        patches: list[WorkerPatch],
        lib: dict[str, str],
        all_libraries: dict[str, dict[str, str]],
    ) -> MergedPatch:
        sandbox = self._cur_sandbox_root / wid
        if sandbox.exists():
            shutil.rmtree(sandbox)
        sandbox.mkdir(parents=True, exist_ok=True)
        self._build_sandbox(sandbox, wid, patches, lib, all_libraries)

        # Cross-round memory: copy persisted memory.md (if any) into sandbox.
        # The agent is expected to read it at start, write it back at end.
        if self.memory_root is not None:
            persist_path = self.memory_root / wid / "memory.md"
            sandbox_mem = sandbox / "memory.md"
            if persist_path.is_file():
                try:
                    shutil.copy2(persist_path, sandbox_mem)
                except Exception:
                    pass
            else:
                # First round: write a stub so the agent knows it exists.
                sandbox_mem.write_text(
                    "# merger memory — first round, no prior notes yet.\n",
                    encoding="utf-8",
                )
            # Family-shared task_memory.md (produced by step 1 task update).
            # Mounted as read-only context for the per-worker merge: agent
            # uses it to ground library decisions in the family's task
            # understanding, but should NOT modify it here.
            task_mem_path = self._task_memory_path()
            if task_mem_path is not None and task_mem_path.is_file():
                try:
                    shutil.copy2(task_mem_path, sandbox / "task_memory.md")
                except Exception:
                    pass

        # Pre-compute orient outputs so the agent doesn't burn turns on them.
        # summarize_patches.sh + peer_consensus.py are deterministic given the
        # sandbox state — no reason to make the agent run them. Embedding the
        # output in the prompt saves ~3-4 turns per merge.
        orient = self._precompute_orient(sandbox)
        skill_body = (self.skill_dir / "SKILL.md").read_text(encoding="utf-8")
        prompt = (
            f"Tool-call rule: when invoking any tool, only include parameters "
            f"with concrete values you actually need. Never pass an optional "
            f"parameter with an empty string \"\" — omit it entirely. (E.g. "
            f"do not call Read with `pages: \"\"`.)\n\n"
            f"You are the cloud merger working on behalf of worker `{wid}`.\n"
            f"Workspace = current directory.\n\n"
            f"=====================================================\n"
            f"=== SKILL: merge-skill-patch (your task procedure) ===\n"
            f"=====================================================\n"
            f"{skill_body}\n\n"
            f"=====================================================\n"
            f"=== ORIENT CONTEXT (pre-computed; skip step 1) ===\n"
            f"=====================================================\n"
            f"{orient}\n\n"
            f"Begin from procedure step 2. The merge is complete when you "
            f"write DECISIONS.md and DONE.txt at the sandbox root."
        )
        try:
            self.agent_runner(
                sandbox_dir=sandbox,
                prompt=prompt,
                model_name=self.merger_model,
                max_turns=self.max_turns,
                wall_clock_sec=self.wall_clock_sec,
                env=self.merger_env or None,
            )
        except Exception as e:
            fb = self.fallback.merge(
                patches, existing_library=lib, self_worker_id=wid
            )
            fb.conflicts.append(
                f"__cloud_skill_agent_failed__:{type(e).__name__}:{e}"
            )
            return fb

        done_file = sandbox / "DONE.txt"
        if not done_file.is_file():
            fb = self.fallback.merge(
                patches, existing_library=lib, self_worker_id=wid
            )
            fb.conflicts.append("__cloud_skill_agent_no_done__")
            return fb

        # Enforce SKILL.md's validation step at the runner level — the agent
        # is supposed to run validate_skill_md.py before writing DONE, but
        # we don't trust it to. Bad SKILL.md output leaks into next round
        # and pollutes patcher / merger inputs. Tag via conflict; don't
        # fall back to LLM since the agent's content is often still better
        # than nothing for borderline frontmatter issues.
        validation_issues = self._validate_skill_md(sandbox / "library")

        merged = self._library_to_merged_patch(
            sandbox / "library", lib, patches, done_file
        )
        merged.cost_usd = parse_claude_code_cost(sandbox / "claude-code.txt")
        for issue in validation_issues[:5]:   # cap to keep conflict list usable
            merged.conflicts.append(f"__skill_md_invalid__:{issue}")

        # Persist updated memory.md back so next round can read it.
        if self.memory_root is not None:
            sandbox_mem = sandbox / "memory.md"
            if sandbox_mem.is_file():
                persist_path = self.memory_root / wid / "memory.md"
                persist_path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.copy2(sandbox_mem, persist_path)
                except Exception:
                    pass
        return merged

    @staticmethod
    def _validate_skill_md(library_dir: Path) -> list[str]:
        """Run `scripts/validate_skill_md.py library/` over the post-merge
        library and return any issues. Empty list = OK.

        Imported lazily so the merger module doesn't depend on the helper
        script's location at import time. We call it as a function rather
        than spawning a subprocess to keep the merger self-contained.
        """
        # Walk every SKILL.md in library_dir and replicate the script's
        # checks inline. (Keeping the validator tool-only on purpose so
        # we don't import a script file from arbitrary paths.)
        import re
        try:
            import yaml  # type: ignore
            has_yaml = True
        except ImportError:
            has_yaml = False
        issues: list[str] = []
        if not library_dir.is_dir():
            return issues
        FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
        for skill_md in library_dir.rglob("SKILL.md"):
            rel = skill_md.relative_to(library_dir)
            try:
                text = skill_md.read_text(encoding="utf-8")
            except Exception as e:
                issues.append(f"{rel}: unreadable ({type(e).__name__})")
                continue
            n_lines = text.count("\n")
            if n_lines > 500:
                issues.append(f"{rel}: {n_lines} lines exceeds 500 cap")
            m = FRONTMATTER.match(text)
            if not m:
                issues.append(f"{rel}: no YAML frontmatter")
                continue
            body = m.group(1)
            fm: dict | None = None
            if has_yaml:
                try:
                    parsed = yaml.safe_load(body)
                    fm = parsed if isinstance(parsed, dict) else None
                except Exception as e:
                    issues.append(f"{rel}: bad YAML ({type(e).__name__})")
                    continue
            else:
                fm = {}
                for line in body.splitlines():
                    if ":" in line:
                        k, _, v = line.partition(":")
                        fm[k.strip()] = v.strip().strip('"').strip("'")
            if not fm or not fm.get("name"):
                issues.append(f"{rel}: frontmatter missing `name`")
                continue
            # Dir name should match `name` (normalized lowercase alnum).
            norm = lambda s: re.sub(r"[^a-z0-9]+", "", str(s).lower())
            dir_name = skill_md.parent.name
            if norm(fm["name"]) != norm(dir_name):
                issues.append(f"{rel}: name='{fm['name']}' mismatches dir '{dir_name}'")
            if not fm.get("description"):
                issues.append(f"{rel}: frontmatter missing `description`")
        return issues

    def _precompute_orient(self, sandbox: Path) -> str:
        """Run summarize_patches.sh + peer_consensus.py against the sandbox
        we just built, and return their stdout concatenated. Embedded into
        the agent's initial prompt so it doesn't burn turns on orient.

        Both scripts are deterministic given filesystem state; no LLM calls.
        Failures are non-fatal — we just include whatever they produced (the
        agent can re-run them if it wants).
        """
        import subprocess
        out_parts: list[str] = []
        for label, cmd in [
            ("meta.json", ["cat", "meta.json"]),
            ("summarize_patches.sh", ["bash", "scripts/summarize_patches.sh"]),
            ("peer_consensus.py", ["python3", "scripts/peer_consensus.py"]),
        ]:
            try:
                r = subprocess.run(
                    cmd, cwd=str(sandbox), capture_output=True,
                    text=True, timeout=30,
                )
                body = r.stdout if r.returncode == 0 else (r.stdout + r.stderr)
            except Exception as e:
                body = f"(failed to run {label}: {type(e).__name__}: {e})"
            out_parts.append(f"--- {label} ---\n{body.rstrip()}")
        return "\n\n".join(out_parts)

    def _build_sandbox(
        self,
        sandbox: Path,
        wid: str,
        patches: list[WorkerPatch],
        lib: dict[str, str],
        all_libraries: dict[str, dict[str, str]] | None = None,
    ) -> None:
        # meta.json — cloud-admin framing: name the target worker explicitly.
        peer_ids = sorted(p.worker_id for p in patches if p.worker_id != wid)
        meta = {
            "target_worker": wid,
            "target_model": self.worker_models.get(wid, "unknown"),
            "target_cli":   self.worker_clis.get(wid, "unknown"),
            "merger_model": self.merger_model,
            "family": self.family,
            "round": self._cur_round_idx,
            "peers": peer_ids,
        }
        # Include all worker→(model, cli) mappings if known, so the agent can
        # reason about heterogeneity along BOTH axes when consolidating peer
        # skills.
        if self.worker_models or self.worker_clis:
            meta["all_workers"] = {
                w: {
                    "model": self.worker_models.get(w, "unknown"),
                    "cli":   self.worker_clis.get(w, "unknown"),
                }
                for w in [wid] + peer_ids
            }
        (sandbox / "meta.json").write_text(
            json.dumps(meta, indent=2), encoding="utf-8"
        )

        # patches/<wid>/{meta.json, files/}
        patches_dir = sandbox / "patches"
        for p in patches:
            wdir = patches_dir / p.worker_id
            wdir.mkdir(parents=True, exist_ok=True)
            (wdir / "meta.json").write_text(
                json.dumps({
                    "worker_id": p.worker_id,
                    "reward": p.reward,
                    "summary": p.summary,
                    "delete_paths": list(p.delete_paths),
                }, indent=2),
                encoding="utf-8",
            )
            files_dir = wdir / "files"
            files_dir.mkdir(exist_ok=True)
            for rel, content in p.upsert_files.items():
                safe = safe_rel_path(rel)
                if safe is None:
                    continue
                target = files_dir / safe
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")

        # library/ + .baseline_library/ — target worker's pre-task snapshot.
        for dst_name in ("library", ".baseline_library"):
            dst = sandbox / dst_name
            dst.mkdir(exist_ok=True)
            for rel, content in lib.items():
                if rel.startswith("__"):
                    continue
                if content == "<binary>":
                    continue
                safe = safe_rel_path(rel)
                if safe is None:
                    continue
                target = dst / safe
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")

        # peer_libraries/<peer_wid>/ — read-only snapshots of every other
        # worker's library at the start of this round. Lets the merger see
        # what the federation as a whole already has, not just patches.
        if all_libraries:
            peer_root = sandbox / "peer_libraries"
            for peer_wid, peer_lib in all_libraries.items():
                if peer_wid == wid:
                    continue
                peer_dst = peer_root / peer_wid
                peer_dst.mkdir(parents=True, exist_ok=True)
                for rel, content in peer_lib.items():
                    if rel.startswith("__"):
                        continue
                    if content == "<binary>":
                        continue
                    safe = safe_rel_path(rel)
                    if safe is None:
                        continue
                    target = peer_dst / safe
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(content, encoding="utf-8")

        # scripts/ — helper scripts the agent runs via Bash. SKILL.md content
        # itself is now embedded in the prompt (see _merge_one) so MERGE_TASK.md
        # file copy is skipped — saves the agent a turn re-reading it.
        scripts_src = self.skill_dir / "scripts"
        if scripts_src.is_dir():
            shutil.copytree(scripts_src, sandbox / "scripts")

    def _library_to_merged_patch(
        self,
        library_dir: Path,
        pre_lib: dict[str, str],
        patches: list[WorkerPatch],
        done_file: Path,
    ) -> MergedPatch:
        """Diff final library/ against pre-task snapshot → MergedPatch.

        Same shape as the deleted PerWorkerSkillMerge: upsert = files that
        differ from pre_lib (or are new); delete = paths in pre_lib that no
        longer exist in library/. The runner resets the worker dir to
        pre-task before applying, so unchanged files don't need to be in the
        upsert set.
        """
        final: dict[str, str] = {}
        for p in library_dir.rglob("*"):
            if not p.is_file():
                continue
            rel = str(p.relative_to(library_dir))
            # Defensive: filesystem can have symlinks, hidden dirs, etc.
            # safe_rel_path filters anything escape-shaped before we expose
            # the path back to the runner.
            if safe_rel_path(rel) is None:
                continue
            # Junk filter: __pycache__, .pyc, .DS_Store etc. are agent
            # byproducts (e.g. py_compile creates __pycache__) that shouldn't
            # land in the next-round library.
            parts = Path(rel).parts
            if "__pycache__" in parts:
                continue
            if rel.endswith((".pyc", ".pyo")) or Path(rel).name == ".DS_Store":
                continue
            try:
                final[rel] = p.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue

        upsert_files: dict[str, str] = {}
        for rel, content in final.items():
            if pre_lib.get(rel) != content:
                upsert_files[rel] = content

        delete_paths: list[str] = []
        for rel, content in pre_lib.items():
            if rel.startswith("__"):
                continue
            if content == "<binary>":
                continue
            if safe_rel_path(rel) is None:
                continue
            if rel not in final:
                delete_paths.append(rel)
        delete_paths.sort()

        try:
            summary = done_file.read_text(encoding="utf-8").strip()
            if len(summary) > 500:
                summary = summary[:497] + "..."
        except Exception:
            summary = "(cloud skill merge: DONE.txt unreadable)"

        # Provenance + conflicts derived from input patches.
        touched: dict[str, list[tuple[str, float]]] = {}
        for p in patches:
            for rel in list(p.upsert_files.keys()) + list(p.delete_paths):
                touched.setdefault(rel, []).append((p.worker_id, p.reward))
        merged = MergedPatch(
            upsert_files=upsert_files,
            delete_paths=delete_paths,
            summary=summary,
            conflicts=sorted([p for p, ws in touched.items() if len(ws) > 1]),
        )
        for path in (set(merged.upsert_files) | set(merged.delete_paths)):
            contributors = touched.get(path, [])
            action: Literal["upsert", "delete"] = (
                "upsert" if path in merged.upsert_files else "delete"
            )
            if contributors:
                top_wid, top_reward = max(contributors, key=lambda x: x[1])
                merged.provenance[path] = (
                    action, f"cloud_skill_merge:top={top_wid}", top_reward
                )
            else:
                merged.provenance[path] = (
                    action, "cloud_skill_merge:synthesized", 0.0
                )
        return merged

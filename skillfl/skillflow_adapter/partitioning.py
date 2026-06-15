"""How to assign N tasks to M workers.

A TaskPartitioner is a pure function of (tasks, n_workers) → list-of-shards,
where shards[i] is the ordered task list for worker i. The order within each
shard determines the per-worker iteration order.

Preserving input order within each shard matters: for RoundRobin and Block the
original list (usually ranking-ordered by difficulty) should come through
untouched so per-worker iteration still follows the benchmark's intended order.
"""

from __future__ import annotations

import random
from abc import ABC, abstractmethod
from typing import Sequence, TypeVar

T = TypeVar("T")


class TaskPartitioner(ABC):
    """Assign a flat task list to M workers."""

    @abstractmethod
    def partition(self, tasks: Sequence[T], n_workers: int) -> list[list[T]]:
        """Return a list of length n_workers; each element is that worker's shard."""
        ...

    def __repr__(self) -> str:
        return f"{type(self).__name__}()"


class RoundRobinPartitioner(TaskPartitioner):
    """tasks[i] goes to worker (i % n_workers).

    Rationale: if `tasks` is difficulty-ordered (as skillflow's ranking file is),
    round-robin makes each worker see a near-uniform difficulty mix, so no worker
    gets stuck only doing easy (or only hard) tasks.
    """

    def partition(self, tasks: Sequence[T], n_workers: int) -> list[list[T]]:
        if n_workers < 1:
            raise ValueError(f"n_workers must be >= 1, got {n_workers}")
        shards: list[list[T]] = [[] for _ in range(n_workers)]
        for i, t in enumerate(tasks):
            shards[i % n_workers].append(t)
        return shards


class BlockPartitioner(TaskPartitioner):
    """tasks[:k] to worker 0, tasks[k:2k] to worker 1, ... where k = ceil(N/M).

    Rationale: if tasks are difficulty-ordered, earlier workers see easier tasks.
    Useful for modeling "curriculum per worker" or reproducing a stricter
    easy-before-hard per-worker schedule.
    """

    def partition(self, tasks: Sequence[T], n_workers: int) -> list[list[T]]:
        if n_workers < 1:
            raise ValueError(f"n_workers must be >= 1, got {n_workers}")
        n = len(tasks)
        # ceil(n / n_workers)
        k = -(-n // n_workers) if n > 0 else 0
        shards: list[list[T]] = []
        for w in range(n_workers):
            shards.append(list(tasks[w * k : (w + 1) * k]))
        return shards


class ReplicatePartitioner(TaskPartitioner):
    """Every worker gets the FULL task list (no splitting).

    Use when the experimental setup is "all M workers attempt every task,
    then merge their patches" — ensembling/replication rather than
    work-splitting. Pair with `isolated_worker_skills=True` in FedConfig
    so each worker's container sees its own private skill dir during a
    trial; otherwise all M workers concurrently writing to the same
    bind-mounted dir will trample each other.
    """

    def partition(self, tasks: Sequence[T], n_workers: int) -> list[list[T]]:
        if n_workers < 1:
            raise ValueError(f"n_workers must be >= 1, got {n_workers}")
        return [list(tasks) for _ in range(n_workers)]


class RandomPartitioner(TaskPartitioner):
    """Shuffle tasks deterministically by seed, then block-partition.

    Rationale: removes correlation between worker id and task difficulty/order.
    Deterministic given seed so experiments are reproducible.
    """

    def __init__(self, seed: int = 0) -> None:
        self.seed = seed

    def partition(self, tasks: Sequence[T], n_workers: int) -> list[list[T]]:
        if n_workers < 1:
            raise ValueError(f"n_workers must be >= 1, got {n_workers}")
        rng = random.Random(self.seed)
        shuffled = list(tasks)
        rng.shuffle(shuffled)
        return BlockPartitioner().partition(shuffled, n_workers)

    def __repr__(self) -> str:
        return f"RandomPartitioner(seed={self.seed})"

"""When workers pool their local patches into a shared merge.

A SyncSchedule decides, given a worker's step index within its own shard,
whether this is a sync boundary. The runner calls `should_sync(...)` after each
worker finishes its current-round task; if all workers agree it's a sync point,
their patches are merged and the resulting skill is broadcast.

In the default FedAvg-style loop the schedule is global (all workers share the
same cadence), so `should_sync` only needs the round index, not per-worker state.
Async / heterogeneous-cadence variants can be layered later by extending this
interface.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class SyncSchedule(ABC):
    """Decide whether a given completed step is a sync boundary."""

    @abstractmethod
    def should_sync(self, round_idx: int, is_last_round: bool) -> bool:
        """Called after round `round_idx` (0-based) completes across all workers.

        `is_last_round`: True if every worker has exhausted its shard with this
        round. Implementations should return True when is_last_round is True so
        the final state is always merged.
        """
        ...

    def __repr__(self) -> str:
        return f"{type(self).__name__}()"


class EveryTaskSync(SyncSchedule):
    """Sync after every round (1 task per worker = 1 round).

    This is the most FedAvg-like setting: maximum cross-worker information
    sharing, minimum cross-shard learning loss.
    """

    def should_sync(self, round_idx: int, is_last_round: bool) -> bool:
        return True


class EveryKTaskSync(SyncSchedule):
    """Sync every k rounds (and always at the end).

    Rationale: reduce merge overhead when merge is expensive (e.g. LLM-based),
    at the cost of letting worker skills diverge slightly between syncs.
    """

    def __init__(self, k: int) -> None:
        if k < 1:
            raise ValueError(f"k must be >= 1, got {k}")
        self.k = k

    def should_sync(self, round_idx: int, is_last_round: bool) -> bool:
        if is_last_round:
            return True
        # round_idx is 0-based; sync at the end of round k-1, 2k-1, ...
        return (round_idx + 1) % self.k == 0

    def __repr__(self) -> str:
        return f"EveryKTaskSync(k={self.k})"


class OnceAtEndSync(SyncSchedule):
    """Only sync at the very end — each worker trains entirely in isolation on
    its shard, and their final skills merge into one.

    Rationale: upper bound on cross-shard learning loss. Useful as a
    "skill-diversity" ablation — shows what we'd lose without any mid-training
    sharing.
    """

    def should_sync(self, round_idx: int, is_last_round: bool) -> bool:
        return is_last_round

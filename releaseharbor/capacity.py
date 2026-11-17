"""Offline synthetic workspace capacity contracts and admission planning.

The capacity ledger is a logical byte budget over the generated output
workspace (``artifact_dir`` + ``quarantine_dir``, including ``.part``
staging files).  It never consults real disk free space: occupancy is
measured from actual file sizes and classified as published targets,
resumable partials, or quarantined bytes.  Old verified targets keep
occupying the ledger until the incoming artifact passes size/SHA
acceptance; residual ``.part`` bytes are occupied but never published.

Two explicitly authorized contracts are supported:

- ``continuous``: every retained verified file stays on disk until its
  replacement is accepted, so a step peaks at
  ``occupancy + (new_bytes - part_bytes)``.
- ``space_first``: a step may register the old digest and retrieval
  source, mark the old generation retired, and delete its bytes before
  downloading, so it peaks at ``occupancy - old_bytes - part_bytes +
  new_bytes`` and an unavailability window is reported instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .errors import ConfigError

CONTRACT_CONTINUOUS = "continuous"
CONTRACT_SPACE_FIRST = "space_first"
CONTRACTS = (CONTRACT_CONTINUOUS, CONTRACT_SPACE_FIRST)
_EXACT_LIMIT = 16


@dataclass(frozen=True)
class CapacitySettings:
    workspace_bytes: int
    contract: str


def parse_capacity(raw) -> CapacitySettings | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigError("capacity must be an object")
    missing = {"workspace_bytes", "contract"} - set(raw)
    unknown = set(raw) - {"workspace_bytes", "contract"}
    if missing:
        raise ConfigError(f"capacity is missing keys: {', '.join(sorted(missing))}")
    if unknown:
        raise ConfigError(f"capacity has unknown keys: {', '.join(sorted(unknown))}")
    size = raw["workspace_bytes"]
    if isinstance(size, bool) or not isinstance(size, int) or not 1 <= size <= (1 << 40):
        raise ConfigError("capacity.workspace_bytes must be an integer in [1, 2**40]")
    contract = raw["contract"]
    if contract not in CONTRACTS:
        raise ConfigError("capacity.contract must be 'continuous' or 'space_first'")
    return CapacitySettings(size, contract)


@dataclass(frozen=True)
class UpdateTask:
    """One pending update: old/part bytes occupied now, new bytes declared."""

    artifact_uid: str
    dataset_id: str
    version_id: str
    filename: str
    old_bytes: int
    part_bytes: int
    new_bytes: int
    source_url: str

    @property
    def extra(self) -> int:
        """Bytes the staged ``.part`` still has to grow to reach the target."""
        return max(0, self.new_bytes - self.part_bytes)

    @property
    def delta(self) -> int:
        """Net occupancy change once the step completes."""
        return self.new_bytes - self.old_bytes - self.part_bytes


@dataclass(frozen=True)
class PlanStep:
    task: UpdateTask
    retire_old: bool
    peak: int
    occupancy_after: int


@dataclass
class CapacityPlan:
    contract: str
    capacity: int
    base_occupancy: int
    steps: tuple[PlanStep, ...]
    feasible: bool
    exact: bool
    peak: int | None
    final_occupancy: int
    lower_bounds: dict
    blocked: tuple[str, ...]


@dataclass(frozen=True)
class Occupancy:
    targets: int
    partials: int
    quarantined: int

    @property
    def total(self) -> int:
        return self.targets + self.partials + self.quarantined


def measure_workspace(artifact_dir: Path, quarantine_dir: Path) -> Occupancy:
    """Classify every byte of the generated output workspace."""
    targets = 0
    partials = 0
    if artifact_dir.is_dir():
        for path in artifact_dir.rglob("*"):
            if not path.is_file():
                continue
            if path.name.endswith(".part"):
                partials += path.stat().st_size
            else:
                targets += path.stat().st_size
    quarantined = 0
    if quarantine_dir.is_dir():
        for path in quarantine_dir.rglob("*"):
            if path.is_file():
                quarantined += path.stat().st_size
    return Occupancy(targets, partials, quarantined)


def _lower_bounds(tasks: list[UpdateTask], base: int, allow_retire: bool) -> dict:
    """Provable capacity floors: current, final, and best-case per-artifact."""
    final = base + sum(task.delta for task in tasks)
    individual = base
    for task in tasks:
        relief = sum(other.delta for other in tasks if other is not task and other.delta < 0)
        need = task.delta if allow_retire else task.extra
        individual = max(individual, base + relief + need)
    return {
        "current_occupancy": base,
        "final_occupancy": final,
        "individual_peak": individual,
    }


def _exact_order(tasks, base, capacity, allow_retire):
    """Subset DP: tightest achievable peak, then fewest retirements."""
    count = len(tasks)
    size = 1 << count
    occ = [0] * size  # delta-only; absolute occupancy is base + occ[mask]
    for mask in range(1, size):
        bit = mask & -mask
        occ[mask] = occ[mask ^ bit] + tasks[bit.bit_length() - 1].delta
    tight = [None] * size
    tight[0] = base
    for mask in range(1, size):
        best = None
        rest = mask
        while rest:
            bit = rest & -rest
            index = bit.bit_length() - 1
            prev = mask ^ bit
            if tight[prev] is not None:
                req = base + occ[prev] + tasks[index].extra
                if allow_retire:
                    req = min(req, base + occ[prev] + tasks[index].delta)
                candidate = max(tight[prev], req)
                if best is None or candidate < best:
                    best = candidate
            rest ^= bit
        tight[mask] = best
    full = size - 1
    if tight[full] is None or tight[full] > capacity:
        return None, tight[full]
    cost = [None] * size
    parent = [None] * size
    cost[0] = 0
    for mask in range(1, size):
        rest = mask
        while rest:
            bit = rest & -rest
            index = bit.bit_length() - 1
            prev = mask ^ bit
            if cost[prev] is not None:
                for retire in (False, True):
                    if retire and not allow_retire:
                        continue
                    req = base + occ[prev] + (
                        tasks[index].delta if retire else tasks[index].extra
                    )
                    if req > capacity:
                        continue
                    candidate = cost[prev] + (1 if retire else 0)
                    if cost[mask] is None or candidate < cost[mask]:
                        cost[mask] = candidate
                        parent[mask] = (prev, index, retire, req)
            rest ^= bit
    if cost[full] is None:
        return None, tight[full]
    steps = []
    mask = full
    while mask:
        prev, index, retire, req = parent[mask]
        steps.append(PlanStep(tasks[index], retire, req, base + occ[mask]))
        mask = prev
    steps.reverse()
    return steps, tight[full]


def _greedy_order(tasks, base, capacity, allow_retire):
    """Feasible-order heuristic for large batches; never emits an invalid plan."""
    remaining = list(tasks)
    occupancy = base
    steps = []
    while remaining:
        best = None
        for index, task in enumerate(remaining):
            for retire in ((False, True) if allow_retire else (False,)):
                req = occupancy + (task.delta if retire else task.extra)
                if req > capacity:
                    continue
                key = (req, retire, task.artifact_uid)
                if best is None or key < best[0]:
                    best = (key, index, retire, req)
        if best is None:
            return None
        _, index, retire, req = best
        task = remaining.pop(index)
        occupancy += task.delta
        steps.append(PlanStep(task, retire, req, occupancy))
    return steps


def plan_updates(
    tasks: list[UpdateTask], base_occupancy: int, capacity: int, contract: str
) -> CapacityPlan:
    """Search a complete execution order whose peaks stay within capacity.

    Never falls back to discovery order or smallest-file greed: small
    batches use an exact subset DP, large batches a feasibility-checked
    heuristic.  When no complete order fits, the plan reports the current
    occupancy plus provable capacity lower bounds and the blocked set.
    """
    allow_retire = contract == CONTRACT_SPACE_FIRST
    ordered = sorted(tasks, key=lambda task: task.artifact_uid)
    bounds = _lower_bounds(ordered, base_occupancy, allow_retire)
    exact = len(ordered) <= _EXACT_LIMIT
    if exact:
        steps, tight = _exact_order(ordered, base_occupancy, capacity, allow_retire)
        bounds["exact_peak"] = tight
    else:
        steps = _greedy_order(ordered, base_occupancy, capacity, allow_retire)
    final = base_occupancy + sum(task.delta for task in ordered)
    feasible = steps is not None
    peak = None
    if feasible:
        peak = max([base_occupancy, final] + [step.peak for step in steps])
    return CapacityPlan(
        contract=contract,
        capacity=capacity,
        base_occupancy=base_occupancy,
        steps=tuple(steps or ()),
        feasible=feasible,
        exact=exact,
        peak=peak,
        final_occupancy=final,
        lower_bounds=bounds,
        blocked=() if feasible else tuple(task.artifact_uid for task in ordered),
    )

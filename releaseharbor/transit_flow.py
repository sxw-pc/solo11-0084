"""Bounded integer flow solver for cross-version transit accounting.

The declared problem is a single-commodity transshipment network: every old
station contributes its recorded count, every new station requires its recorded
count, and allowed edges carry integer flow with optional lower/upper bounds.
``SOURCE`` and ``SINK`` (with an implicit external reservoir) are available only
when the edge table declares them; otherwise the totals must balance exactly.

Feasibility is decided by a max-flow with lower-bound transformation.  The
minimum and maximum counted cross-zone flow are then obtained from that feasible
flow by canceling negative-cost residual cycles, where the counted arcs cost 1
(or -1 for the maximum).  Every extremum therefore comes with a complete
integer edge assignment that can be re-checked from the witness alone.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from .transit_model import TransitInputs

SOURCE = "SOURCE"
SINK = "SINK"
ENV = "__external__"
OLD_NODE = "old:"
NEW_NODE = "new:"


class FlowModelError(Exception):
    """The declared model is malformed before any flow is attempted."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


class Infeasible(Exception):
    """No integer assignment satisfies every supply, demand, and capacity."""

    def __init__(self, conflict: dict) -> None:
        super().__init__(conflict.get("detail", "infeasible"))
        self.conflict = conflict


@dataclass(frozen=True)
class Arc:
    key: str
    source: str
    target: str
    low: int
    high: int
    kind: str = "edge"


@dataclass
class Network:
    nodes: list[str]
    arcs: list[Arc]
    balance: dict[str, int]
    old_ids: list[str]
    new_ids: list[str]
    zone_of: dict[str, str]
    cross_keys: list[str]
    old_zone: str
    new_zone: str
    code_of: dict[str, str] = field(default_factory=dict)
    edge_refs: list[dict] = field(default_factory=list)
    note: str = ""

    @property
    def cross_set(self) -> set[str]:
        return set(self.cross_keys)

    def declared_arcs(self) -> list[Arc]:
        return [arc for arc in self.arcs if arc.kind == "edge"]


@dataclass
class Bounds:
    minimum: int
    maximum: int
    min_flow: dict[str, int]
    max_flow: dict[str, int]
    min_witness: dict
    max_witness: dict


def build_network(
    inputs: TransitInputs,
    old_zone: str,
    new_zone: str,
    edge_refs: list[dict] | None = None,
) -> Network:
    old, new = inputs.old, inputs.new
    if old is None or new is None:
        raise FlowModelError("missing_side", "both base and next release sides are required")
    old_codes = sorted(old.stations)
    new_codes = sorted(new.stations)
    zone_of: dict[str, str] = {}
    for code in old_codes:
        label = old.labels.get(code)
        if label is None:
            raise FlowModelError("missing_zone", f"base station {code} has no zone label")
        zone_of[f"old:{code}"] = label.zone
    for code in new_codes:
        label = new.labels.get(code)
        if label is None:
            raise FlowModelError("missing_zone", f"next station {code} has no zone label")
        zone_of[f"new:{code}"] = label.zone
    if old_zone not in {zone_of[f"old:{code}"] for code in old_codes}:
        raise FlowModelError("unknown_zone", f"base labels contain no zone {old_zone!r}")
    if new_zone not in {zone_of[f"new:{code}"] for code in new_codes}:
        raise FlowModelError("unknown_zone", f"next labels contain no zone {new_zone!r}")

    total_old = sum(item.amount for item in old.stations.values())
    total_new = sum(item.amount for item in new.stations.values())
    if total_old > 1_000_000_000 or total_new > 1_000_000_000:
        raise FlowModelError("scale_limit", "station totals exceed the supported solver scale")

    old_set = set(old_codes)
    new_set = set(new_codes)
    arcs: list[Arc] = []
    declared_source = declared_sink = False

    def resolve(endpoint: str, is_source: bool, key: str, counterpart: str) -> str:
        if endpoint == SOURCE:
            return SOURCE
        if endpoint == SINK:
            return SINK
        # Normal allowed edge: base -> next.  SOURCE injects directly into a
        # next station; a next station may drain into SINK.
        if counterpart == SOURCE:
            allowed, node_side, expected = new_set, "new", "next"
        elif counterpart == SINK:
            allowed, node_side, expected = old_set, "old", "base"
        elif is_source:
            allowed, node_side, expected = old_set, "old", "base"
        else:
            allowed, node_side, expected = new_set, "new", "next"
        if endpoint not in allowed:
            raise FlowModelError(
                "unknown_node",
                f"edge {key} references {expected} station {endpoint!r}, which the "
                f"{expected} release does not declare",
            )
        return f"{node_side}:{endpoint}"

    for table in inputs.edge_tables:
        for item in table.edges:
            key = f"{table.version_id}/{item.key}"
            source = resolve(item.source, True, key, item.target)
            target = resolve(item.target, False, key, item.source)
            if source == SOURCE:
                declared_source = True
            if target == SINK:
                declared_sink = True
            if (source == SOURCE and target == SINK) or (
                source == SINK or target == SOURCE
            ):
                raise FlowModelError(
                    "reserved_edge",
                    f"edge {key} uses SOURCE as a destination or SINK as an origin",
                )
            arcs.append(Arc(key, source, target, item.minimum, item.maximum))
    balance: dict[str, int] = {}
    for code in old_codes:
        balance[f"old:{code}"] = -old.stations[code].amount
    for code in new_codes:
        balance[f"new:{code}"] = new.stations[code].amount
    if declared_source:
        balance[SOURCE] = 0
    if declared_sink:
        balance[SINK] = 0
    if not declared_source and not declared_sink:
        if total_old != total_new:
            raise FlowModelError(
                "conservation_conflict",
                f"base total {total_old} differs from next total {total_new} and the "
                "edge table declares no SOURCE/SINK node",
            )
        note = "strict conservation: totals must match exactly"
    else:
        limit = total_old + total_new + 1
        if declared_source:
            arcs.append(Arc("env/into-source", ENV, SOURCE, 0, limit, "external"))
        if declared_sink:
            arcs.append(Arc("env/from-sink", SINK, ENV, 0, limit, "external"))
        balance[ENV] = total_old - total_new
        note = "totals may differ only through the declared SOURCE/SINK nodes"

    nodes = sorted(balance)
    old_nodes = [f"old:{code}" for code in old_codes]
    new_nodes = [f"new:{code}" for code in new_codes]
    roots = list(old_nodes) + ([SOURCE] if declared_source else [])
    sinks = list(new_nodes) + ([SINK] if declared_sink else [])
    forward = _reach(arcs, roots, forward=True)
    backward = _reach(arcs, sinks, forward=False)
    # Zero-amount stations need no flow, so they may legitimately stay isolated.
    missing = [
        node
        for node in old_nodes
        if node not in backward and balance[node] != 0
    ]
    missing += [
        node
        for node in new_nodes
        if node not in forward and balance[node] != 0
    ]
    if missing:
        raise FlowModelError(
            "unreachable_node",
            "no declared edge path joins these stations to a supply and a demand: "
            + ", ".join(sorted(node.split(":", 1)[1] for node in set(missing))),
        )
    cross = [
        arc.key
        for arc in arcs
        if arc.kind == "edge"
        and arc.source.startswith(OLD_NODE)
        and arc.target.startswith(NEW_NODE)
        and zone_of[arc.source] == old_zone
        and zone_of[arc.target] == new_zone
    ]
    code_of = {f"old:{code}": code for code in old_codes}
    code_of.update({f"new:{code}": code for code in new_codes})
    return Network(
        nodes=nodes,
        arcs=arcs,
        balance=balance,
        old_ids=old_nodes,
        new_ids=new_nodes,
        zone_of=zone_of,
        cross_keys=cross,
        old_zone=old_zone,
        new_zone=new_zone,
        code_of=code_of,
        edge_refs=list(edge_refs or []),
        note=note,
    )


def _reach(arcs: list[Arc], roots: list[str], forward: bool) -> set[str]:
    parent: dict[str, list[str]] = {}
    for arc in arcs:
        if forward:
            parent.setdefault(arc.source, []).append(arc.target)
        else:
            parent.setdefault(arc.target, []).append(arc.source)
    seen = set(roots)
    queue = deque(roots)
    while queue:
        node = queue.popleft()
        for nxt in parent.get(node, ()):
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    return seen


def compute_bounds(network: Network) -> Bounds:
    from .flow_solver import feasible_flow
    from .flow_witness import optimize

    base = feasible_flow(network)
    cross = set(network.cross_keys)
    min_flow = optimize(network, base, {key: 1 for key in cross})
    max_flow = optimize(network, base, {key: -1 for key in cross})
    from .flow_witness import witness

    min_witness = witness(network, min_flow, "minimum")
    max_witness = witness(network, max_flow, "maximum")
    if not min_witness["checks"]["ok"] or not max_witness["checks"]["ok"]:
        raise FlowModelError(
            "witness_invalid", "solver produced an assignment that fails verification"
        )
    return Bounds(
        minimum=min_witness["cross_flow"],
        maximum=max_witness["cross_flow"],
        min_flow=min_flow,
        max_flow=max_flow,
        min_witness=min_witness,
        max_witness=max_witness,
    )

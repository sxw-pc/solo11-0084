"""Witness assembly and residual-cycle optimization for transit bounds."""

from __future__ import annotations

from .transit_flow import OLD_NODE, NEW_NODE, FlowModelError, Network


def _residual_edges(network: Network, flow: dict[str, int], costs: dict[str, int]):
    index = {name: position for position, name in enumerate(network.nodes)}
    edges = []
    for arc in network.arcs:
        used = flow[arc.key]
        cost = costs.get(arc.key, 0)
        if used < arc.high:
            edges.append((index[arc.source], index[arc.target], arc.high - used, cost, arc.key, 1))
        if used > arc.low:
            edges.append((index[arc.target], index[arc.source], used - arc.low, -cost, arc.key, -1))
    return edges


def _negative_cycle(count: int, edges) -> list | None:
    distance = [0] * count
    parent_node = [-1] * count
    parent_edge = [-1] * count
    changed = -1
    for _round in range(count):
        changed = -1
        for position, (_u, v, capacity, cost, _key, _sign) in enumerate(edges):
            if capacity <= 0:
                continue
            if distance[_u] + cost < distance[v]:
                distance[v] = distance[_u] + cost
                parent_node[v] = _u
                parent_edge[v] = position
                changed = v
        if changed < 0:
            return None
    if changed < 0:
        return None
    node = changed
    for _step in range(count):
        node = parent_node[node]
        if node < 0:
            return None
    cycle = []
    current = node
    guard = 0
    while True:
        position = parent_edge[current]
        if position < 0:
            return None
        cycle.append(edges[position])
        current = parent_node[current]
        guard += 1
        if current == node or guard > count:
            break
    return cycle if guard <= count else None


def optimize(network: Network, base: dict[str, int], costs: dict[str, int]) -> dict[str, int]:
    flow = dict(base)
    count = len(network.nodes)
    limit = 2_000_000 + 100 * sum(arc.high for arc in network.arcs if arc.kind == "edge")
    for _iteration in range(limit):
        edges = _residual_edges(network, flow, costs)
        cycle = _negative_cycle(count, edges)
        if not cycle:
            return flow
        amount = min(edge[2] for edge in cycle)
        if amount <= 0:
            return flow
        for _u, _v, _capacity, _cost, key, sign in cycle:
            flow[key] += sign * amount
    raise FlowModelError("solver_limit", "negative-cycle cancellation did not converge")


def witness(network: Network, flow: dict[str, int], label: str) -> dict:
    net = {name: 0 for name in network.balance}
    problems = []
    edge_rows = []
    for arc in network.arcs:
        value = flow[arc.key]
        if not isinstance(value, int) or isinstance(value, bool):
            problems.append(f"{arc.key} flow is not an integer")
            continue
        if not arc.low <= value <= arc.high:
            problems.append(f"{arc.key} flow {value} violates [{arc.low}, {arc.high}]")
        net[arc.source] -= value
        net[arc.target] += value
        if arc.kind == "edge":
            edge_rows.append(
                {
                    "key": arc.key,
                    "from": network.code_of.get(arc.source, arc.source),
                    "to": network.code_of.get(arc.target, arc.target),
                    "side_from": "old" if arc.source.startswith(OLD_NODE) else None,
                    "side_to": "new" if arc.target.startswith(NEW_NODE) else None,
                    "counted_cross_zone": arc.key in network.cross_set,
                    "min": arc.low,
                    "max": arc.high,
                    "flow": value,
                }
            )
    for name, required in network.balance.items():
        if net[name] != required:
            problems.append(f"{name} net inflow {net[name]} != required {required}")
    cross = sum(flow[key] for key in network.cross_keys)
    if any(not isinstance(flow[key], int) for key in network.cross_keys):
        problems.append("cross-zone flow is not integral")
    rows = []
    for code in network.old_ids:
        rows.append(
            {
                "station": network.code_of.get(code, code),
                "side": "old",
                "zone": network.zone_of[code],
                "required_net_inflow": network.balance[code],
                "observed_net_inflow": net[code],
            }
        )
    for code in network.new_ids:
        rows.append(
            {
                "station": network.code_of.get(code, code),
                "side": "new",
                "zone": network.zone_of[code],
                "required_net_inflow": network.balance[code],
                "observed_net_inflow": net[code],
            }
        )
    return {
        "label": label,
        "cross_flow": cross,
        "old_zone": network.old_zone,
        "new_zone": network.new_zone,
        "edges": sorted(edge_rows, key=lambda item: item["key"]),
        "stations": rows,
        "checks": {
            "integrality": not any("integer" in item or "integral" in item for item in problems),
            "capacity": not any("violates" in item for item in problems),
            "conservation": not any("net inflow" in item for item in problems),
            "problems": problems,
            "ok": not problems,
        },
    }


def compute_bounds(network: Network) -> Bounds:
    base = _feasible(network)
    cross = set(network.cross_keys)
    min_cost = {key: 1 for key in cross}
    max_cost = {key: -1 for key in cross}
    min_flow = _optimize(network, base, min_cost)
    max_flow = _optimize(network, base, max_cost)
    min_witness = _witness(network, min_flow, "minimum")
    max_witness = _witness(network, max_flow, "maximum")
    if not min_witness["checks"]["ok"] or not max_witness["checks"]["ok"]:
        raise FlowModelError("witness_invalid", "solver produced an assignment that fails verification")
    return Bounds(
        minimum=min_witness["cross_flow"],
        maximum=max_witness["cross_flow"],
        min_flow=min_flow,
        max_flow=max_flow,
        min_witness=min_witness,
        max_witness=max_witness,
    )

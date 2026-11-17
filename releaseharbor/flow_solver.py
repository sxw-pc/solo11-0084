"""Max-flow feasibility with lower bounds and residual-cycle optimization."""

from __future__ import annotations

from collections import deque

from .transit_flow import Arc, Infeasible, Network


def _add_edge(adj: list[list[list]], source: int, target: int, capacity: int) -> int:
    forward = len(adj[source])
    adj[target].append([source, 0, len(adj[source]), 0])
    adj[source].append([target, capacity, len(adj[target]) - 1, 0])
    return forward


def _dinic(adj: list[list[list]], source: int, sink: int) -> int:
    total = 0
    count = len(adj)
    while True:
        level = [-1] * count
        level[source] = 0
        queue = deque([source])
        while queue:
            node = queue.popleft()
            for edge in adj[node]:
                if edge[1] > 0 and level[edge[0]] < 0:
                    level[edge[0]] = level[node] + 1
                    queue.append(edge[0])
        if level[sink] < 0:
            return total
        cursor = [0] * count
        while True:
            pushed = _one_path(adj, level, cursor, source, sink)
            if pushed == 0:
                break
            total += pushed


def _one_path(adj, level, cursor, source, sink) -> int:
    stack = [source]
    path: list[tuple[int, int]] = []
    while stack:
        node = stack[-1]
        if node == sink:
            amount = min(adj[parent][slot][1] for parent, slot in path)
            for parent, slot in path:
                edge = adj[parent][slot]
                edge[1] -= amount
                adj[edge[0]][edge[2]][1] += amount
            return amount
        advanced = False
        while cursor[node] < len(adj[node]):
            edge = adj[node][cursor[node]]
            if edge[1] > 0 and level[edge[0]] == level[node] + 1:
                path.append((node, cursor[node]))
                stack.append(edge[0])
                advanced = True
                break
            cursor[node] += 1
        if not advanced:
            level[node] = -1
            stack.pop()
            if path:
                path.pop()
    return 0


def feasible_flow(network: Network) -> dict[str, int]:
    index = {name: position for position, name in enumerate(network.nodes)}
    adj: list[list[list]] = [[] for _ in network.nodes]
    records: list[tuple[str, Arc, int, int]] = []
    balance = dict(network.balance)
    for arc in network.arcs:
        slot = _add_edge(adj, index[arc.source], index[arc.target], arc.high - arc.low)
        records.append((arc.key, arc, index[arc.source], slot))
        balance[arc.source] += arc.low
        balance[arc.target] -= arc.low
    super_source = len(adj)
    super_sink = super_source + 1
    adj.extend([[], []])
    required = 0
    super_edges: dict[int, list[tuple[int, int]]] = {}
    for name, value in balance.items():
        node = index[name]
        if value > 0:
            slot = _add_edge(adj, node, super_sink, value)
            super_edges.setdefault(node, []).append((slot, value))
            required += value
        elif value < 0:
            _add_edge(adj, super_source, node, -value)
    pushed = _dinic(adj, super_source, super_sink)
    if pushed != required:
        raise Infeasible(
            _certificate(network, balance, adj, index, records, super_edges, super_source)
        )
    flow: dict[str, int] = {}
    for key, arc, node, slot in records:
        used = (arc.high - arc.low) - adj[node][slot][1]
        flow[key] = arc.low + used
    return flow


def _certificate(network, balance, adj, index, records, super_edges, source) -> dict:
    reachable = {source}
    queue = deque([source])
    while queue:
        node = queue.popleft()
        for edge in adj[node]:
            if edge[1] > 0 and edge[0] not in reachable:
                reachable.add(edge[0])
                queue.append(edge[0])
    names = {position: name for name, position in index.items()}
    cut_nodes = sorted(names[node] for node in reachable if node in names)
    cut_edges = []
    for _key, arc, node, slot in records:
        if node in reachable and index[arc.target] not in reachable:
            residual = adj[node][slot][1]
            cut_edges.append(
                {
                    "key": arc.key,
                    "from": arc.source,
                    "to": arc.target,
                    "lower": arc.low,
                    "upper": arc.high,
                    "residual": residual,
                }
            )
    unmet = []
    for node, edges in super_edges.items():
        short = sum(adj[node][slot][1] for slot, _cap in edges)
        if short:
            unmet.append({"node": names[node], "unmet": short})
    shortfalls = []
    supply_shortfalls = []
    for code in network.new_ids:
        inflow = [arc for arc in network.arcs if arc.target == code and arc.kind == "edge"]
        outflow = [arc for arc in network.arcs if arc.source == code and arc.kind == "edge"]
        capacity = sum(arc.high for arc in inflow)
        demand = network.balance.get(code, 0)
        if not outflow and capacity < demand:
            shortfalls.append(
                {
                    "station": network.code_of.get(code, code),
                    "zone": network.zone_of.get(code),
                    "demand": demand,
                    "declared_inflow_upper": capacity,
                    "shortfall": demand - capacity,
                }
            )
    for code in network.old_ids:
        inflow = [arc for arc in network.arcs if arc.target == code and arc.kind == "edge"]
        outflow = [arc for arc in network.arcs if arc.source == code and arc.kind == "edge"]
        capacity = sum(arc.high for arc in outflow)
        supply = -network.balance.get(code, 0)
        if not inflow and capacity < supply:
            supply_shortfalls.append(
                {
                    "station": network.code_of.get(code, code),
                    "zone": network.zone_of.get(code),
                    "supply": supply,
                    "declared_outflow_upper": capacity,
                    "shortfall": supply - capacity,
                }
            )
    return {
        "kind": "infeasible",
        "code": "no_feasible_integer_flow",
        "detail": (
            "supplies, demands, lower/upper bounds, and integrality cannot all be "
            "satisfied at once"
        ),
        "cut_nodes": cut_nodes,
        "cut_edges": cut_edges,
        "unmet_demand": sorted(unmet, key=lambda item: item["node"]),
        "new_station_shortfalls": sorted(shortfalls, key=lambda item: item["station"]),
        "old_station_shortfalls": sorted(supply_shortfalls, key=lambda item: item["station"]),
    }



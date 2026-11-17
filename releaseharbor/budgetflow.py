"""Exact integer feasibility for bounded transportation tableaux.

Budget cross-tabs are non-negative integer matrices with fixed row and/or
column totals.  Feasibility is decided with a max-flow circulation
construction (lower/upper bounds on every arc).  Because the constraint
matrix of a transportation problem is totally unimodular, an integral
feasible flow exists whenever a fractional one does, so the construction
is exact for the non-negative-integer model -- including cells that a
pure rank argument would call free but that the zero lower bound pins.
The same construction returns witness assignments for audit evidence.
"""

from __future__ import annotations


class _Dinic:
    """Small deterministic Dinic max-flow over integer capacities."""

    def __init__(self, nodes: int) -> None:
        self.adj: list[list[list[int]]] = [[] for _ in range(nodes)]

    def add_edge(self, u: int, v: int, cap: int) -> tuple[int, int]:
        self.adj[u].append([v, cap, len(self.adj[v])])
        self.adj[v].append([u, 0, len(self.adj[u]) - 1])
        return (u, len(self.adj[u]) - 1)

    def max_flow(self, source: int, sink: int) -> int:
        flow = 0
        while True:
            level = [-1] * len(self.adj)
            level[source] = 0
            queue = [source]
            for node in queue:
                for edge in self.adj[node]:
                    if edge[1] > 0 and level[edge[0]] < 0:
                        level[edge[0]] = level[node] + 1
                        queue.append(edge[0])
            if level[sink] < 0:
                return flow
            iterator = [0] * len(self.adj)
            while True:
                pushed = self._push(source, sink, 1 << 62, level, iterator)
                if not pushed:
                    break
                flow += pushed

    def _push(self, u: int, sink: int, limit: int, level: list[int], iterator: list[int]) -> int:
        if u == sink:
            return limit
        while iterator[u] < len(self.adj[u]):
            edge = self.adj[u][iterator[u]]
            v, cap, rev = edge
            if cap > 0 and level[v] == level[u] + 1:
                pushed = self._push(v, sink, min(limit, cap), level, iterator)
                if pushed:
                    edge[1] -= pushed
                    self.adj[v][rev][1] += pushed
                    return pushed
            iterator[u] += 1
        return 0


def circulation(num_nodes: int, edges: list[tuple[int, int, int, int]], want_flows: bool = False):
    """Feasible circulation with per-edge ``(lower, upper)`` bounds.

    Returns ``None`` when infeasible, otherwise ``True`` or -- with
    ``want_flows`` -- the list of per-edge flows in input order.
    """
    balance = [0] * num_nodes
    dinic = _Dinic(num_nodes + 2)
    refs = []
    for u, v, lower, upper in edges:
        if lower < 0 or upper < lower:
            return None
        refs.append((dinic.add_edge(u, v, upper - lower), upper - lower, lower))
        balance[u] -= lower
        balance[v] += lower
    super_source, super_sink = num_nodes, num_nodes + 1
    demand = 0
    for node, bal in enumerate(balance):
        if bal > 0:
            dinic.add_edge(super_source, node, bal)
            demand += bal
        elif bal < 0:
            dinic.add_edge(node, super_sink, -bal)
    if dinic.max_flow(super_source, super_sink) != demand:
        return None
    if not want_flows:
        return True
    return [lower + original - dinic.adj[u][idx][1] for (u, idx), original, lower in refs]


def transport(
    row_totals: list[int | None],
    col_totals: list[int | None],
    cells: dict[tuple[int, int], tuple[int, int]],
    want_flows: bool = False,
):
    """Feasibility of a bounded transportation tableau.

    ``row_totals`` / ``col_totals`` hold exact integer sums, or ``None``
    for an unconstrained margin.  ``cells`` maps every ``(row, col)`` to
    its ``(lower, upper)`` bound.  Returns ``None`` when infeasible,
    ``True`` otherwise, or a ``{(row, col): value}`` witness assignment.
    """
    rows = len(row_totals)
    cols = len(col_totals)
    sink = 1 + rows + cols
    big = 1 + sum(t for t in row_totals + col_totals if t is not None)
    edges: list[tuple[int, int, int, int]] = []
    order: list[tuple[int, int]] = []
    for i, total in enumerate(row_totals):
        lo, hi = (total, total) if total is not None else (0, big)
        edges.append((0, 1 + i, lo, hi))
    for (i, j), (lo, hi) in cells.items():
        order.append((i, j))
        edges.append((1 + i, 1 + rows + j, lo, hi))
    for j, total in enumerate(col_totals):
        lo, hi = (total, total) if total is not None else (0, big)
        edges.append((1 + rows + j, sink, lo, hi))
    edges.append((sink, 0, 0, big))
    flows = circulation(sink + 1, edges, want_flows)
    if flows is None:
        return None
    if not want_flows:
        return True
    return {order[k]: flows[rows + k] for k in range(len(order))}

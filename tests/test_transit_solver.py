"""Exhaustive cross-checks of the integer flow solver against brute force."""

from __future__ import annotations

import itertools
import unittest

from releaseharbor.transit_flow import (
    FlowModelError,
    Infeasible,
    build_network,
    compute_bounds,
)
from releaseharbor.transit_model import (
    Edge,
    EdgeTable,
    Label,
    SiteSide,
    Station,
    TransitInputs,
)


def _side(role, amounts, zones):
    side = SiteSide(role, "v", "x", 0, "")
    side.stations = {code: Station(code, value, 0) for code, value in amounts.items()}
    side.labels = {code: Label(code, zones[code], 0) for code in amounts}
    return side


def _network(old, old_zones, new, new_zones, rows):
    edges = [Edge(a, b, lo, hi, 0, index + 1) for index, (a, b, lo, hi) in enumerate(rows)]
    inputs = TransitInputs(
        "t",
        old=_side("old", old, old_zones),
        new=_side("new", new, new_zones),
        edge_tables=[EdgeTable("e", "x", 0, "", edges)],
    )
    return build_network(inputs, "E", "W")


def _brute(old, old_zones, new, new_zones, rows):
    old_ids = sorted(old)
    new_ids = sorted(new)
    best = worst = None
    for amounts in itertools.product(*[range(lo, hi + 1) for _a, _b, lo, hi in rows]):
        net = {code: 0 for code in [*old_ids, *new_ids, "SOURCE", "SINK"]}
        for (a, b, _lo, _hi), value in zip(rows, amounts):
            net[a] = net.get(a, 0) - value
            net[b] = net.get(b, 0) + value
        if any(net[code] != -old[code] for code in old_ids):
            continue
        if any(net[code] != new[code] for code in new_ids):
            continue
        if net["SOURCE"] > 0 or net["SINK"] < 0:
            continue
        if net["SINK"] + net["SOURCE"] != sum(old.values()) - sum(new.values()):
            continue
        cross = sum(
            value
            for (a, b, _lo, _hi), value in zip(rows, amounts)
            if a in old and b in new and old_zones[a] == "E" and new_zones[b] == "W"
        )
        best = cross if best is None else min(best, cross)
        worst = cross if worst is None else max(worst, cross)
    return None if best is None else (best, worst)


class SolverCrossCheckTests(unittest.TestCase):
    def _assert_matches(self, cases):
        for index, (old, old_zones, new, new_zones, rows) in enumerate(cases):
            with self.subTest(case=index):
                try:
                    bounds = compute_bounds(_network(old, old_zones, new, new_zones, rows))
                    got = (bounds.minimum, bounds.maximum)
                except (Infeasible, FlowModelError):
                    got = None
                self.assertEqual(got, _brute(old, old_zones, new, new_zones, rows))

    def test_small_balanced_networks_match_brute_force(self):
        cases = []
        for supply in range(0, 4):
            for demand in range(0, 4):
                if supply != demand:
                    continue
                rows = [
                    ("A", "C", 0, supply),
                    ("A", "D", 0, supply),
                    ("B", "C", 0, demand),
                    ("B", "D", 0, demand),
                ]
                cases.append(({"A": supply, "B": demand}, {"A": "E", "B": "W"},
                              {"C": demand, "D": supply}, {"C": "E", "D": "W"}, rows))
        cases.append(({"A": 2}, {"A": "E"}, {"C": 2}, {"C": "W"}, [("A", "C", 1, 2)]))
        cases.append(({"A": 2}, {"A": "E"}, {"C": 2}, {"C": "W"}, [("A", "C", 0, 1)]))
        cases.append(({"A": 1, "B": 1}, {"A": "E", "B": "W"},
                      {"C": 1, "D": 1}, {"C": "W", "D": "E"},
                      [("A", "C", 0, 1), ("A", "D", 0, 1), ("B", "C", 0, 1), ("B", "D", 0, 1)]))
        self._assert_matches(cases)

    def test_source_and_sink_networks_match_brute_force(self):
        cases = [
            ({"A": 2, "B": 2}, {"A": "E", "B": "W"}, {"C": 3, "D": 0}, {"C": "E", "D": "W"},
             [("A", "C", 1, 2), ("B", "C", 1, 2), ("SOURCE", "C", 0, 4), ("A", "SINK", 0, 1)]),
            ({"A": 1}, {"A": "E"}, {"C": 2}, {"C": "W"},
             [("A", "C", 0, 1), ("SOURCE", "C", 0, 2)]),
            ({"A": 2}, {"A": "E"}, {"C": 1}, {"C": "W"},
             [("A", "C", 0, 2), ("A", "SINK", 0, 2)]),
        ]
        self._assert_matches(cases)

    def test_infeasible_lower_bound_is_detected(self):
        network = _network(
            {"A": 1, "B": 1}, {"A": "E", "B": "W"}, {"C": 1, "D": 1}, {"C": "E", "D": "W"},
            [("A", "C", 0, 1), ("A", "D", 0, 1), ("B", "C", 0, 1), ("B", "D", 0, 1)],
        )
        lower = _network(
            {"A": 1, "B": 1}, {"A": "E", "B": "W"}, {"C": 1, "D": 1}, {"C": "E", "D": "W"},
            [("A", "C", 1, 1), ("A", "D", 1, 1), ("B", "C", 0, 1), ("B", "D", 0, 1)],
        )
        self.assertEqual((compute_bounds(network).minimum, compute_bounds(network).maximum), (0, 1))
        with self.assertRaises(Infeasible):
            compute_bounds(lower)


if __name__ == "__main__":
    unittest.main()

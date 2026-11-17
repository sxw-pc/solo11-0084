"""Feasible cross-version transit bounds and the two business contracts."""

from __future__ import annotations

import unittest

from releaseharbor.transit import load_query_spec
from transit_support import (
    FOUR_EDGES,
    TransitBaseCase,
    _csv,
    _sha,
    _write_json,
    contract,
    edges_csv,
    files_for,
    spec,
    versions_for,
)


class FeasibleBoundsTests(TransitBaseCase):
    def test_extremes_have_complete_integer_witnesses(self):
        self.build()
        result = self.fixture.query(spec())
        guaranteed = contract(result, "guaranteed_relocation")
        exposure = contract(result, "maximum_exposure")
        self.assertEqual((guaranteed["minimum"], guaranteed["maximum"]), (2, 5))
        self.assertEqual(exposure["value"], 5)
        self.assertEqual(guaranteed["value"], 2)
        for name, expected in (("minimum", 2), ("maximum", 5)):
            witness = guaranteed["witnesses"][name]
            self.assertEqual(witness["cross_flow"], expected)
            self.assertTrue(witness["checks"]["ok"])
            flow = {item["key"]: item["flow"] for item in witness["edges"]}
            self.assertEqual(flow["edges-v1/e02:A->D"], expected)
            self.assertEqual(flow["edges-v1/e01:A->C"] + flow["edges-v1/e02:A->D"], 7)
            self.assertEqual(flow["edges-v1/e03:B->C"] + flow["edges-v1/e04:B->D"], 3)
            self.assertEqual(flow["edges-v1/e01:A->C"] + flow["edges-v1/e03:B->C"], 5)
            for edge in witness["edges"]:
                self.assertIsInstance(edge["flow"], int)
                self.assertLessEqual(edge["min"], edge["flow"])
                self.assertLessEqual(edge["flow"], edge["max"])
        self.assertNotEqual(
            guaranteed["witnesses"]["minimum"]["edges"],
            guaranteed["witnesses"]["maximum"]["edges"],
        )
        self.assertEqual(len(result["evidence"]), 4)
        self.assertEqual(
            result["edge_evidence"][0]["observed_sha256"], _sha(edges_csv(FOUR_EDGES))
        )
        self.assertTrue(result["input_digest"])

    def test_lower_bound_recomputes_both_extremes(self):
        rows = [("A", "C", 4, 99)] + FOUR_EDGES[1:]
        self.build(files=files_for(**{"flow_edges.csv": edges_csv(rows)}))
        result = self.fixture.query(spec())
        guaranteed = contract(result, "guaranteed_relocation")
        self.assertEqual((guaranteed["minimum"], guaranteed["maximum"]), (2, 3))
        self.assertEqual(guaranteed["value"], 2)
        self.assertEqual(contract(result, "maximum_exposure")["value"], 3)
        flow_min = {
            e["key"]: e["flow"] for e in guaranteed["witnesses"]["minimum"]["edges"]
        }
        flow_max = {
            e["key"]: e["flow"] for e in guaranteed["witnesses"]["maximum"]["edges"]
        }
        self.assertEqual(flow_min["edges-v1/e01:A->C"], 5)
        self.assertEqual(flow_max["edges-v1/e01:A->C"], 4)
        self.assertEqual(flow_max["edges-v1/e02:A->D"], 3)
        self.assertNotEqual(
            guaranteed["witnesses"]["minimum"]["edges"],
            guaranteed["witnesses"]["maximum"]["edges"],
        )

    def test_split_and_merge_flow(self):
        files = files_for(**{
            "stations.csv": _csv(
                "station_code,station_name,weekday_boardings\nA,Alpha,5\nB,Bravo,5\n"
            ),
            "next_stations.csv": _csv(
                "station_code,station_name,weekday_boardings\nC,Charlie,6\nD,Delta,4\n"
            ),
        })
        self.build(files=files)
        result = self.fixture.query(spec())
        guaranteed = contract(result, "guaranteed_relocation")
        # Only A (east) -> D (west) is a cross-zone edge; D needs 4 and A holds 5.
        self.assertEqual(guaranteed["minimum"], 0)
        self.assertEqual(guaranteed["maximum"], 4)
        for name in ("minimum", "maximum"):
            self.assertTrue(guaranteed["witnesses"][name]["checks"]["ok"])

    def test_query_file_round_trip(self):
        self.build()
        query_path = _write_json(
            self.root / "query.json",
            {
                "dataset_id": "transit-counts",
                "old_version": "v1",
                "new_version": "v2",
                "old_zone": "E",
                "new_zone": "W",
                "new_counts": "next_stations.csv",
                "new_labels": "next_labels.csv",
                "edge_tables": [{"version": "edges-v1", "filename": "flow_edges.csv"}],
            },
        )
        loaded = load_query_spec(query_path)
        self.assertEqual(loaded, spec())




class WitnessIntegrityTests(TransitBaseCase):
    def test_witness_can_be_rechecked_from_its_own_numbers(self):
        self.build()
        result = self.fixture.query(spec())
        witness = contract(result, "guaranteed_relocation")["witnesses"]["maximum"]
        net = {}
        cross = 0
        for edge in witness["edges"]:
            self.assertTrue(edge["min"] <= edge["flow"] <= edge["max"])
            net[edge["from"]] = net.get(edge["from"], 0) - edge["flow"]
            net[edge["to"]] = net.get(edge["to"], 0) + edge["flow"]
            if edge["counted_cross_zone"]:
                cross += edge["flow"]
        self.assertEqual(cross, witness["cross_flow"])
        for station in witness["stations"]:
            self.assertEqual(net.get(station["station"], 0), station["required_net_inflow"])
            self.assertEqual(
                station["observed_net_inflow"], station["required_net_inflow"]
            )

    def test_source_node_explains_growth_and_native_flow(self):
        counts = _csv(
            "station_code,station_name,weekday_boardings\nC,Charlie,8\nD,Delta,8\n"
        )
        rows = [
            ("SOURCE", "C", 0, 99),
            ("A", "C", 0, 99),
            ("A", "D", 0, 99),
            ("B", "C", 0, 99),
            ("B", "D", 0, 99),
        ]
        self.build(
            files=files_for(
                **{"next_stations.csv": counts, "flow_edges.csv": edges_csv(rows)}
            )
        )
        result = self.fixture.query(spec())
        guaranteed = contract(result, "guaranteed_relocation")
        # Native supply is 10 and demand is 16.  SOURCE can only feed C, which
        # leaves at least 5 native A->D flow; total east->west can reach 7 when
        # SOURCE covers C as far as possible.
        self.assertEqual(guaranteed["minimum"], 5)
        self.assertEqual(guaranteed["maximum"], 7)
        for name in ("minimum", "maximum"):
            self.assertTrue(guaranteed["witnesses"][name]["checks"]["ok"])
        witness = guaranteed["witnesses"]["minimum"]
        source = next(
            edge for edge in witness["edges"] if edge["from"] == "SOURCE"
        )
        self.assertGreaterEqual(source["flow"], 0)
        self.assertEqual(witness["cross_flow"], guaranteed["minimum"])

    def test_multiple_edge_tables_are_merged_and_duplicates_rejected(self):
        files = files_for(
            **{
                "flow_edges.csv": edges_csv(FOUR_EDGES[:2]),
                "extra.csv": edges_csv(FOUR_EDGES[2:]),
            }
        )
        versions = versions_for(
            extra={"edges-v1b": {"released_at": "2025-02-12", "files": ["extra.csv"]}}
        )
        self.build(files=files, versions=versions)
        merged = self.fixture.query(
            spec(
                edge_tables=[
                    {"version": "edges-v1", "filename": "flow_edges.csv"},
                    {"version": "edges-v1b", "filename": "extra.csv"},
                ]
            )
        )
        self.assertFalse(contract(merged, "guaranteed_relocation")["unknown"])
        self.assertEqual(contract(merged, "guaranteed_relocation")["minimum"], 2)
        self.assertEqual(contract(merged, "guaranteed_relocation")["maximum"], 5)
        conflicting = self.fixture.query(
            spec(
                edge_tables=[
                    {"version": "edges-v1", "filename": "flow_edges.csv"},
                    {"version": "edges-v1", "filename": "flow_edges.csv"},
                ]
            )
        )
        self.assertIn("duplicate_edge", [d["code"] for d in conflicting["diagnostics"]])


if __name__ == "__main__":
    unittest.main()

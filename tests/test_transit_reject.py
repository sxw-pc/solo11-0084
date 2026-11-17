"""Rejected, infeasible, tampered, and edge-version cases for transit bounds."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from helpers import FIXTURE, free_port, portal_for, write_project
from releaseharbor.config import load_config
from releaseharbor.db import connect_readonly
from releaseharbor.pipeline import run_pipeline
from releaseharbor.transit import QuerySpec, query_bounds
from releaseharbor.transit_report import write_transit_result
from transit_support import (
    FOUR_EDGES,
    TransitBaseCase,
    TransitFixture,
    _csv,
    contract,
    edges_csv,
    files_for,
    spec,
    versions_for,
)


class InfeasibleAndRejectTests(TransitBaseCase):
    def test_cut_capacity_infeasible_is_not_a_partial_number(self):
        rows = [("A", "C", 0, 3), ("B", "C", 0, 2), ("B", "D", 0, 3)]
        self.build(files=files_for(**{"flow_edges.csv": edges_csv(rows)}))
        result = self.fixture.query(spec())
        for name in ("guaranteed_relocation", "maximum_exposure"):
            item = contract(result, name)
            self.assertTrue(item["unknown"])
            self.assertIsNone(item["value"])
            self.assertIsNone(item["minimum"])
            self.assertIsNone(item["witnesses"])
            self.assertEqual(item["unknown_reason"][0]["code"], "infeasible")
        conflict = result["conflict"]
        shortfall = conflict["new_station_shortfalls"][0]
        self.assertEqual(shortfall["station"], "D")
        self.assertLess(shortfall["declared_inflow_upper"], shortfall["demand"])
        old_shortfall = conflict["old_station_shortfalls"][0]
        self.assertEqual(old_shortfall["station"], "A")
        self.assertLess(old_shortfall["declared_outflow_upper"], old_shortfall["supply"])
        self.assertTrue(conflict["cut_edges"])

    def test_duplicate_station_is_located(self):
        duplicate = _csv(
            "station_code,station_name,weekday_boardings\n"
            "C,Charlie,5\nC,Charlie again,5\nD,Delta,5\n"
        )
        self.build(files=files_for(**{"next_stations.csv": duplicate}))
        result = self.fixture.query(spec())
        self.assertIn("duplicate_station", [d["code"] for d in result["diagnostics"]])
        entry = next(d for d in result["diagnostics"] if d["code"] == "duplicate_station")
        self.assertEqual((entry["filename"], entry["line"]), ("next_stations.csv", 3))
        self.assertTrue(contract(result, "guaranteed_relocation")["unknown"])

    def test_missing_zone_is_located(self):
        self.build(files=files_for(**{
            "next_labels.csv": _csv("station_code,short_label,zone\nC,Charlie,E\n")
        }))
        result = self.fixture.query(spec())
        self.assertIn("missing_zone", [d["code"] for d in result["diagnostics"]])
        self.assertTrue(contract(result, "maximum_exposure")["unknown"])

    def test_zone_label_conflict_is_located(self):
        conflict_labels = _csv(
            "station_code,short_label,zone\nC,Charlie,E\nD,Delta,W\nD,Delta again,E\n"
        )
        self.build(files=files_for(**{"next_labels.csv": conflict_labels}))
        result = self.fixture.query(spec())
        self.assertIn("duplicate_label", [d["code"] for d in result["diagnostics"]])
        entry = next(d for d in result["diagnostics"] if d["code"] == "duplicate_label")
        self.assertEqual(entry["line"], 4)
        for name in ("guaranteed_relocation", "maximum_exposure"):
            self.assertTrue(contract(result, name)["unknown"])

    def test_unknown_node_is_rejected(self):
        rows = FOUR_EDGES + [("A", "Z", 0, 9)]
        self.build(files=files_for(**{"flow_edges.csv": edges_csv(rows)}))
        result = self.fixture.query(spec())
        self.assertEqual(result["diagnostics"][0]["code"], "unknown_node")
        self.assertTrue(contract(result, "guaranteed_relocation")["unknown"])

    def test_negative_capacity_is_rejected(self):
        rows = [("A", "C", 0, -1)] + FOUR_EDGES[1:]
        self.build(files=files_for(**{"flow_edges.csv": edges_csv(rows)}))
        result = self.fixture.query(spec())
        self.assertIn("negative_capacity", [d["code"] for d in result["diagnostics"]])
        self.assertIsNone(contract(result, "guaranteed_relocation")["value"])

    def test_inverted_versions_are_rejected(self):
        self.build()
        inverted = spec(
            old_version="v2",
            new_version="v1",
            old_counts="next_stations.csv",
            old_labels="next_labels.csv",
            new_counts="stations.csv",
            new_labels="station_labels.csv",
        )
        result = self.fixture.query(inverted)
        self.assertIn("version_inversion", [d["code"] for d in result["diagnostics"]])
        for name in ("guaranteed_relocation", "maximum_exposure"):
            self.assertTrue(contract(result, name)["unknown"])

    def test_conservation_without_env_nodes(self):
        shifted = _csv(
            "station_code,station_name,weekday_boardings\nC,Charlie,8\nD,Delta,5\n"
        )
        self.build(files=files_for(**{"next_stations.csv": shifted}))
        result = self.fixture.query(spec())
        self.assertEqual(result["diagnostics"][0]["code"], "conservation_conflict")
        self.assertTrue(contract(result, "maximum_exposure")["unknown"])


class AcceptanceAndTamperTests(TransitBaseCase):
    def test_unaccepted_artifact_blocks_both_contracts(self):
        self.build(declared_sha={"next_stations.csv": "0" * 64})
        result = self.fixture.query(spec())
        self.assertEqual(result["diagnostics"][0]["code"], "artifact_not_accepted")
        for name in ("guaranteed_relocation", "maximum_exposure"):
            item = contract(result, name)
            self.assertTrue(item["unknown"])
            self.assertEqual(item["unknown_reason"][0]["code"], "artifact_not_accepted")

    def test_disk_tamper_blocks_both_contracts(self):
        self.build()
        target = self.fixture.config.download.artifact_dir / "transit-counts/v2/next_stations.csv"
        target.write_bytes(
            _csv("station_code,station_name,weekday_boardings\nC,Charlie,5\nD,Delta,4\n")
        )
        result = self.fixture.query(spec())
        self.assertEqual(result["diagnostics"][0]["code"], "reverify_mismatch")
        for name in ("guaranteed_relocation", "maximum_exposure"):
            self.assertTrue(contract(result, name)["unknown"])

    def test_query_does_not_mutate_release_state(self):
        self.build()
        artifact = self.fixture.config.download.artifact_dir / "transit-counts/v2/next_stations.csv"
        before_bytes = artifact.read_bytes()
        connection = connect_readonly(self.fixture.config.download.db_path)
        try:
            before = connection.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]
            before_runs = connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        finally:
            connection.close()
        self.fixture.query(spec())
        self.assertEqual(artifact.read_bytes(), before_bytes)
        connection = connect_readonly(self.fixture.config.download.db_path)
        try:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0], before
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0], before_runs
            )
            self.assertFalse(
                connection.execute(
                    "SELECT 1 FROM artifacts WHERE status NOT IN ('verified','skipped')"
                ).fetchone()
            )
        finally:
            connection.close()


class EdgeVersionAndPersistenceTests(TransitBaseCase):
    def test_new_edge_version_changes_only_bounds_and_old_result_stays_readable(self):
        first = self.build()
        result = first.query(spec())
        paths = write_transit_result(first.config, result)
        saved = self.root / "saved-bounds.json"
        saved.write_bytes((self.root / paths["transit_json"]).read_bytes())
        first.stop()

        rows = [("A", "C", 0, 9), ("A", "D", 0, 2), ("B", "C", 0, 5), ("B", "D", 0, 9)]
        files = files_for(**{"flow_edges.csv": edges_csv(rows)})
        versions = versions_for(
            extra={"edges-v2": {"released_at": "2025-02-12", "files": ["flow_edges.csv"]}}
        )
        second = TransitFixture(self.root, files, versions)
        second.start()
        self.addCleanup(second.stop)
        second.run()
        changed = second.query(
            spec(edge_tables=[{"version": "edges-v2", "filename": "flow_edges.csv"}])
        )
        guaranteed = contract(changed, "guaranteed_relocation")
        self.assertEqual((guaranteed["minimum"], guaranteed["maximum"]), (2, 2))
        self.assertEqual(changed["edge_evidence"][0]["version"], "edges-v2")

        old = json.loads(saved.read_text(encoding="utf-8"))
        self.assertEqual(old["input_digest"], result["input_digest"])
        self.assertEqual(contract(old, "maximum_exposure")["value"], 5)
        self.assertEqual(
            contract(old, "guaranteed_relocation")["witnesses"]["maximum"]["cross_flow"], 5
        )
        self.assertNotEqual(old["input_digest"], changed["input_digest"])
        archived = self.root / paths["transit_archive_json"]
        self.assertTrue(archived.is_file())
        reread_archived = json.loads(archived.read_text(encoding="utf-8"))
        self.assertEqual(reread_archived["input_digest"], result["input_digest"])
        self.assertEqual(
            contract(reread_archived, "maximum_exposure")["value"], 5
        )


class DemoFixtureTransitTests(unittest.TestCase):
    def test_next_release_demo_query_reports_zone_move(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = load_config(write_project(root, free_port()))
            from helpers import FIXTURE

            (root / "fixtures").mkdir(parents=True, exist_ok=True)
            (root / "fixtures/portal.json").write_bytes(FIXTURE.read_bytes())
            config = load_config(root / "config/demo.json")
            portal = portal_for(config).start()
            self.addCleanup(portal.stop)
            run_pipeline(config, reset=True)
            connection = connect_readonly(config.download.db_path)
            try:
                result = query_bounds(
                    config,
                    connection,
                    QuerySpec(
                        dataset_id="transit-counts",
                        old_version="v3",
                        new_version="v4",
                        old_zone="W1",
                        new_zone="N4",
                        old_label_version="v3-labels",
                        new_label_version="v4-labels",
                        edge_tables=[{"version": "flows-v3-v4", "filename": "flow_edges.csv"}],
                    ),
                )
            finally:
                connection.close()
            guaranteed = contract(result, "guaranteed_relocation")
            self.assertFalse(guaranteed["unknown"])
            self.assertGreaterEqual(guaranteed["minimum"], 0)
            self.assertLessEqual(guaranteed["maximum"], 18360)
            self.assertEqual(len(result["evidence"]), 4)


if __name__ == "__main__":
    unittest.main()

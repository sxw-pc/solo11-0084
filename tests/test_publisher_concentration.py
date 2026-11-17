"""Offline publisher attribution: renames, reuse, exclusions, HHI bounds."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from helpers import free_port, portal_for, registry_spec, write_project
from releaseharbor.attribution import analyze_run
from releaseharbor.clock import Clock
from releaseharbor.config import load_config
from releaseharbor.db import connect
from releaseharbor.discovery import DiscoveredDataset, DiscoveredVersion
from releaseharbor.fixture import load_portal_fixture
from releaseharbor.ledger import Ledger
from releaseharbor.pipeline import run_pipeline
from releaseharbor.registry import load_registry
from releaseharbor.util import sha256_bytes

NOW = "2026-01-15T12:00:00Z"


class PublisherConcentrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = connect(self.root / "var" / "ledger.sqlite3")
        self.ledger = Ledger(self.db, Clock(mode="fixed", fixed=NOW))

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def registry(self, spec, name="registry.json"):
        path = self.root / name
        path.write_text(json.dumps(spec), encoding="utf-8")
        return load_registry(path)

    def add_release(self, run_id, dataset_id, publisher, released_at, name, size, disk="ok",
                    version="v1"):
        if not self.db.execute("SELECT 1 FROM runs WHERE run_id = ?", (run_id,)).fetchone():
            self.ledger.start_run(run_id, f"fp-{run_id}")
        self.ledger.upsert_dataset(run_id, DiscoveredDataset(
            dataset_id, f"Title {dataset_id}", publisher, "CC0 (synthetic)",
            f"http://127.0.0.1/{dataset_id}"))
        self.ledger.upsert_version(run_id, DiscoveredVersion(
            version, f"v {dataset_id}", "stable", released_at, "http://127.0.0.1"), dataset_id)
        payload = b"x" * size
        digest = sha256_bytes(payload)
        target = self.root / "var" / "artifacts" / dataset_id / version / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload if disk == "ok" else payload + b"tampered")
        self.db.execute(
            "INSERT INTO artifacts(artifact_uid, dataset_id, version_id, filename, download_url,"
            " media_type, expected_size, expected_sha256, status, local_path, actual_size,"
            " actual_sha256, attempts, first_seen_run_id, last_run_id, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, 'http://127.0.0.1/f', 'application/octet-stream', ?, ?,"
            " 'verified', ?, ?, ?, 0, ?, ?, ?, ?)",
            (f"{dataset_id}:{version}:{name}", dataset_id, version, name, size, digest,
             f"var/artifacts/{dataset_id}/{version}/{name}", size, digest, run_id, run_id,
             NOW, NOW),
        )
        self.db.commit()

    def buckets_of(self, witness):
        return {bucket["bucket"]: bucket["bytes"] for bucket in witness["buckets"]}

    def test_legal_entity_and_group_contracts_disagree(self):
        self.add_release("run-1", "ds-a", "Alpha Releases", "2025-01-10", "a.bin", 5)
        self.add_release("run-1", "ds-b", "Beta Releases", "2025-01-10", "b.bin", 3)
        self.add_release("run-1", "ds-c", "Gamma Releases", "2025-01-10", "c.bin", 2)
        registry = self.registry(registry_spec(
            orgs=[("org-a", "grp-g"), ("org-b", "grp-g"), ("org-c", None)],
            groups=("grp-g",),
            aliases=[
                ("Alpha Releases", "2025-01-01", "2025-12-31", ["org-a"]),
                ("Beta Releases", "2025-01-01", "2025-12-31", ["org-b"]),
                ("Gamma Releases", "2025-01-01", "2025-12-31", ["org-c"]),
            ],
        ))
        analysis = analyze_run(self.db, "run-1", registry, self.root)
        self.assertEqual(analysis["status"], "ok")
        self.assertEqual(analysis["totals"]["verified_bytes"], 10)
        legal = analysis["concentration"]["legal_entity"]
        group = analysis["concentration"]["group_control"]
        self.assertAlmostEqual(legal["hhi_min"], 0.38, places=9)
        self.assertAlmostEqual(legal["hhi_max"], 0.38, places=9)
        self.assertAlmostEqual(group["hhi_min"], 0.68, places=9)
        self.assertAlmostEqual(group["hhi_max"], 0.68, places=9)
        self.assertEqual(self.buckets_of(legal["min_witness"]),
                         {"org-a": 5, "org-b": 3, "org-c": 2})
        self.assertEqual(self.buckets_of(group["min_witness"]), {"grp-g": 8, "org-c": 2})
        self.assertEqual(len(legal["min_witness"]["assignment"]), 3)

    def test_display_name_rename_and_reuse_across_years(self):
        self.add_release("run-1", "ds-1", "Dawn Analytics", "2023-06-01", "f1.bin", 2)
        self.add_release("run-1", "ds-2", "Dawn Data Bureau", "2024-06-01", "f2.bin", 3)
        self.add_release("run-1", "ds-3", "Harbor Metrics", "2022-06-01", "f3.bin", 1)
        self.add_release("run-1", "ds-4", "Harbor Metrics", "2024-06-01", "f4.bin", 4)
        registry = self.registry(registry_spec(
            orgs=[("org-dawn", None), ("org-p", None), ("org-q", None)],
            aliases=[
                ("Dawn Analytics", "2023-01-01", "2023-12-31", ["org-dawn"]),
                ("Dawn Data Bureau", "2024-01-01", "2024-12-31", ["org-dawn"]),
                ("Harbor Metrics", "2022-01-01", "2022-12-31", ["org-p"]),
                ("Harbor Metrics", "2024-01-01", "2024-12-31", ["org-q"]),
            ],
        ))
        analysis = analyze_run(self.db, "run-1", registry, self.root)
        self.assertEqual(analysis["status"], "ok")
        legal = analysis["concentration"]["legal_entity"]
        self.assertAlmostEqual(legal["hhi_min"], 0.42, places=9)
        self.assertEqual(self.buckets_of(legal["min_witness"]),
                         {"org-dawn": 5, "org-p": 1, "org-q": 4})

    def test_global_exclusion_shapes_hhi_bounds_and_witnesses(self):
        self.add_release("run-1", "ds-x", "Name X", "2025-03-01", "x.bin", 5)
        self.add_release("run-1", "ds-y", "Name Y", "2025-03-01", "y.bin", 3)
        self.add_release("run-1", "ds-z", "Name Z", "2025-03-01", "z.bin", 2)
        registry = self.registry(registry_spec(
            orgs=[("org-a", "grp-g"), ("org-b", "grp-g"), ("org-c", None)],
            groups=("grp-g",),
            aliases=[
                ("Name X", "2025-01-01", "2025-12-31", ["org-a", "org-b"]),
                ("Name Y", "2025-01-01", "2025-12-31", ["org-a", "org-c"]),
                ("Name Z", "2025-01-01", "2025-12-31", ["org-c"]),
            ],
            exclusions=[(("Name X", "org-a"), ("Name Y", "org-a"))],
        ))
        analysis = analyze_run(self.db, "run-1", registry, self.root)
        self.assertEqual(analysis["status"], "ok")
        legal = analysis["concentration"]["legal_entity"]
        group = analysis["concentration"]["group_control"]
        self.assertEqual(legal["feasible_assignments"], 3)
        self.assertAlmostEqual(legal["hhi_min"], 0.38, places=9)
        self.assertAlmostEqual(legal["hhi_max"], 0.5, places=9)
        self.assertAlmostEqual(group["hhi_min"], 0.5, places=9)
        self.assertAlmostEqual(group["hhi_max"], 0.68, places=9)
        self.assertEqual(legal["min_witness_count"], 1)
        self.assertEqual(legal["max_witness_count"], 2)
        assignment = {a["display_name"]: a["org_id"] for a in legal["min_witness"]["assignment"]}
        self.assertEqual(assignment, {"Name X": "org-b", "Name Y": "org-a", "Name Z": "org-c"})
        for contract in (legal, group):
            for witness in (contract["min_witness"], contract["max_witness"]):
                combo = {a["display_name"]: a["org_id"] for a in witness["assignment"]}
                self.assertEqual(len(combo), 3)
                self.assertFalse(combo["Name X"] == "org-a" and combo["Name Y"] == "org-a")
        statuses = {item["attribution"]["status"] for item in analysis["items"]}
        self.assertEqual(statuses, {"ambiguous", "resolved"})

    def test_old_publisher_snapshots_survive_later_upserts(self):
        self.add_release("run-1", "ds-1", "Dawn Analytics", "2023-06-01", "f1.bin", 4)
        self.add_release("run-2", "ds-1", "Dawn Data Bureau", "2024-06-01", "f2.bin", 4,
                         version="v2")
        row = self.db.execute("SELECT publisher FROM datasets WHERE dataset_id = 'ds-1'").fetchone()
        self.assertEqual(row["publisher"], "Dawn Data Bureau")
        self.assertEqual(self.ledger.publisher_snapshot("run-1", "ds-1"), "Dawn Analytics")
        self.assertEqual(self.ledger.publisher_snapshot("run-2", "ds-1"), "Dawn Data Bureau")
        registry = self.registry(registry_spec(
            orgs=[("org-dawn", None)],
            aliases=[
                ("Dawn Analytics", "2023-01-01", "2023-12-31", ["org-dawn"]),
                ("Dawn Data Bureau", "2024-01-01", "2024-12-31", ["org-dawn"]),
            ],
        ))
        old = analyze_run(self.db, "run-1", registry, self.root)
        new = analyze_run(self.db, "run-2", registry, self.root)
        self.assertEqual(old["status"], "ok")
        self.assertEqual(old["items"][0]["publisher"], "Dawn Analytics")
        self.assertEqual(new["items"][0]["publisher"], "Dawn Data Bureau")
        self.assertAlmostEqual(old["concentration"]["legal_entity"]["hhi_min"], 1.0, places=9)

    def test_unknown_evidence_nulls_concentration(self):
        self.add_release("run-1", "ds-ok", "Alpha Releases", "2025-01-10", "ok.bin", 4)
        self.add_release("run-1", "ds-blank", "", "2025-01-10", "b.bin", 5)
        self.add_release("run-1", "ds-date", "Alpha Releases", "2025-13-40", "d.bin", 3)
        self.add_release("run-1", "ds-disk", "Alpha Releases", "2025-01-10", "t.bin", 2,
                         disk="tampered")
        self.add_release("run-1", "ds-name", "Mystery Org", "2025-01-10", "m.bin", 1)
        registry = self.registry(registry_spec(
            orgs=[("org-a", None)],
            aliases=[("Alpha Releases", "2025-01-01", "2025-12-31", ["org-a"])],
        ))
        analysis = analyze_run(self.db, "run-1", registry, self.root)
        self.assertEqual(analysis["status"], "unknown")
        self.assertEqual(analysis["totals"]["verified_artifacts"], 5)
        self.assertEqual(analysis["totals"]["known_bytes"], 4)
        self.assertEqual(analysis["totals"]["unknown_bytes"], 11)
        reasons = {
            item["dataset_id"]: item["attribution"]["reasons"]
            for item in analysis["items"]
            if item["attribution"]["status"] == "unknown"
        }
        self.assertEqual(reasons, {
            "ds-blank": ["source_missing"],
            "ds-date": ["invalid_date"],
            "ds-disk": ["disk_mismatch"],
            "ds-name": ["name_not_registered"],
        })
        for contract in analysis["concentration"].values():
            self.assertEqual(contract["status"], "unknown")
            self.assertIsNone(contract["hhi_min"])
            self.assertIsNone(contract["hhi_max"])
            self.assertIsNone(contract["min_witness"])

    def test_registry_versions_replay_without_rewriting_history(self):
        self.add_release("run-1", "ds-a", "Alpha Releases", "2025-01-10", "a.bin", 5)
        self.add_release("run-1", "ds-b", "Beta Releases", "2025-01-10", "b.bin", 3)
        self.add_release("run-1", "ds-c", "Gamma Releases", "2025-01-10", "c.bin", 2)
        aliases = [
            ("Alpha Releases", "2025-01-01", "2025-12-31", ["org-a"]),
            ("Beta Releases", "2025-01-01", "2025-12-31", ["org-b"]),
            ("Gamma Releases", "2025-01-01", "2025-12-31", ["org-c"]),
        ]
        orgs = [("org-a", None), ("org-b", None), ("org-c", None)]
        v1 = self.registry(registry_spec(orgs=orgs, aliases=aliases, version="reg-1"))
        first = analyze_run(self.db, "run-1", v1, self.root)
        self.assertAlmostEqual(first["concentration"]["legal_entity"]["hhi_min"], 0.38, places=9)
        widened = [("Alpha Releases", "2025-01-01", "2025-12-31", ["org-a", "org-b"])] + aliases[1:]
        v2 = self.registry(registry_spec(orgs=orgs, aliases=widened, version="reg-2"),
                           name="registry-v2.json")
        second = analyze_run(self.db, "run-1", v2, self.root)
        legal = second["concentration"]["legal_entity"]
        self.assertAlmostEqual(legal["hhi_min"], 0.38, places=9)
        self.assertAlmostEqual(legal["hhi_max"], 0.68, places=9)
        self.assertEqual(first["registry"]["version"], "reg-1")
        self.assertEqual(second["registry"]["version"], "reg-2")
        self.assertNotEqual(first["registry"]["fingerprint"], second["registry"]["fingerprint"])
        self.assertEqual(analyze_run(self.db, "run-1", v1, self.root), first)
        self.assertEqual(self.ledger.publisher_snapshot("run-1", "ds-a"), "Alpha Releases")
        row = self.db.execute("SELECT status FROM artifacts WHERE dataset_id = 'ds-a'").fetchone()
        self.assertEqual(row["status"], "verified")

    def test_ambiguity_cap_and_empty_run_are_explicit(self):
        aliases = []
        for index in range(13):
            self.add_release("run-1", f"ds-{index}", f"Name {index}", "2025-01-10",
                             f"f{index}.bin", 1)
            aliases.append((f"Name {index}", "2025-01-01", "2025-12-31", ["org-a", "org-b"]))
        registry = self.registry(
            registry_spec(orgs=[("org-a", None), ("org-b", None)], aliases=aliases)
        )
        analysis = analyze_run(self.db, "run-1", registry, self.root)
        self.assertEqual(analysis["status"], "underdetermined")
        self.assertIsNone(analysis["concentration"]["legal_entity"]["hhi_min"])
        empty = analyze_run(self.db, "run-empty", registry, self.root)
        self.assertEqual(empty["status"], "empty")

    def test_pipeline_emits_attribution_report_when_configured(self):
        config_path = write_project(self.root, free_port())
        data = json.loads(config_path.read_text(encoding="utf-8"))
        data["attribution"] = {"registry": "fixtures/publishers.json"}
        config_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        spec = registry_spec(
            orgs=[("org-civic", "grp-metro"), ("org-transit", "grp-metro"), ("org-air", None)],
            groups=("grp-metro",),
            aliases=[
                ("Aurora Municipal Data Office", "2025-01-01", "2025-12-31", ["org-civic"]),
                ("Rivergate Transit Analytics Unit", "2025-01-01", "2025-12-31", ["org-transit"]),
                ("Northline Air Quality Collective", "2025-01-01", "2025-12-31", ["org-air"]),
            ],
            version="it-1",
        )
        (self.root / "fixtures" / "publishers.json").write_text(json.dumps(spec), encoding="utf-8")
        config = load_config(config_path)
        portal = portal_for(config).start()
        try:
            outcome = run_pipeline(config, reset=True)
        finally:
            portal.stop()
        self.assertIn("attribution_json", outcome.reports)
        analysis = json.loads((config.download.report_dir / "attribution.json").read_text("utf-8"))
        self.assertEqual(analysis["status"], "ok")
        self.assertEqual(analysis["registry"]["version"], "it-1")
        self.assertEqual(analysis["totals"]["verified_artifacts"], 8)
        fixture = load_portal_fixture(config.portal.fixture)
        org_of = {"civic-budget": "org-civic", "transit-counts": "org-transit",
                  "air-quality": "org-air"}
        expected = {"org-civic": 0, "org-transit": 0, "org-air": 0}
        for dataset in fixture.datasets:
            for version in dataset.versions:
                if version.channel not in ("stable", "beta"):
                    continue
                for artifact in version.artifacts:
                    if artifact.name != "samples.csv":
                        expected[org_of[dataset.dataset_id]] += artifact.actual_size
        legal = analysis["concentration"]["legal_entity"]
        self.assertEqual(self.buckets_of(legal["min_witness"]), expected)
        total = sum(expected.values())
        self.assertAlmostEqual(legal["hhi_min"], sum((w / total) ** 2 for w in expected.values()),
                               places=9)
        self.assertAlmostEqual(legal["hhi_min"], legal["hhi_max"], places=12)
        report = json.loads((config.download.report_dir / "report.json").read_text("utf-8"))
        self.assertNotIn("attribution", report)


if __name__ == "__main__":
    unittest.main()

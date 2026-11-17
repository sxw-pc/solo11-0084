"""Workspace capacity contract: admission, ordering, contracts, recovery."""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from helpers import free_port
from releaseharbor.capacity import UpdateTask, measure_workspace, parse_capacity, plan_updates
from releaseharbor.clock import Clock
from releaseharbor.config import load_config
from releaseharbor.db import connect
from releaseharbor.discovery import ArtifactRef
from releaseharbor.downloader import Downloader, artifact_destination
from releaseharbor.errors import ConfigError
from releaseharbor.fixture import load_portal_fixture
from releaseharbor.ledger import Ledger
from releaseharbor.pipeline import run_pipeline
from releaseharbor.portal_server import PortalServer
from releaseharbor.verify import verify_file


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fixture_json(artifacts) -> dict:
    entries = []
    for name, content, declared_size, declared_sha, transient in artifacts:
        item = {
            "name": name,
            "media_type": "application/octet-stream",
            "content": content.decode("ascii"),
            "sha256": declared_sha or sha(content),
        }
        if declared_size is not None:
            item["declared_size"] = declared_size
        if transient:
            item["transient_failures"] = transient
        entries.append(item)
    return {
        "schema_version": 1,
        "portal_title": "t", "portal_description": "d", "publisher": "p",
        "datasets": [{
            "id": "ds", "title": "t", "summary": "s", "publisher": "p", "license": "CC0",
            "versions": [{"id": "v1", "label": "v1", "channel": "stable",
                          "released_at": "2026-01-01", "artifacts": entries}],
        }],
    }


def pack(name, content, declared_size=None, declared_sha=None, transient=0):
    return (name, content, declared_size, declared_sha, transient)


class Project:
    def __init__(self, test, capacity, contract="continuous"):
        self.temp = tempfile.TemporaryDirectory()
        test.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.capacity = capacity
        self.contract = contract
        self.config = None

    def run(self, artifacts, reset=False):
        fixture_dir = self.root / "fixtures"
        fixture_dir.mkdir(exist_ok=True)
        (fixture_dir / "portal.json").write_text(json.dumps(fixture_json(artifacts)))
        config = {
            "schema_version": 1, "root": "..",
            "portal": {"fixture": "fixtures/portal.json", "bind_host": "127.0.0.1",
                       "base_url": f"http://127.0.0.1:{free_port()}"},
            "discovery": {"max_datasets": 5, "max_versions": 5, "max_artifacts": 10,
                          "max_pages": 20, "channels": ["stable"]},
            "download": {"work_dir": "var", "artifact_dir": "var/artifacts",
                         "quarantine_dir": "var/quarantine",
                         "db_path": "var/releaseharbor.sqlite3", "report_dir": "reports",
                         "retries": 0, "retry_backoff_ms": 0, "chunk_size": 4096,
                         "max_artifact_bytes": 1048576},
            "security": {"request_timeout_seconds": 3},
            "clock": {"mode": "fixed", "fixed": "2026-01-15T12:00:00Z"},
        }
        if self.capacity is not None:
            config["capacity"] = {"workspace_bytes": self.capacity, "contract": self.contract}
        config_dir = self.root / "config"
        config_dir.mkdir(exist_ok=True)
        (config_dir / "demo.json").write_text(json.dumps(config))
        self.config = load_config(config_dir / "demo.json")
        fixture = load_portal_fixture(self.config.portal.fixture)
        portal = PortalServer(fixture, "127.0.0.1",
                              int(self.config.portal.base_url.rsplit(":", 1)[1]))
        portal.start()
        try:
            return run_pipeline(self.config, reset=reset)
        finally:
            portal.stop()

    def target(self, filename="data.bin"):
        return artifact_destination(
            self.config.download.artifact_dir, "ds", "v1", filename)

    def events(self, run_id, code=None):
        connection = connect(self.config.download.db_path)
        try:
            rows = connection.execute(
                "SELECT code, subject, detail_json FROM events WHERE run_id = ? ORDER BY seq",
                (run_id,)).fetchall()
        finally:
            connection.close()
        out = [(r["code"], r["subject"], json.loads(r["detail_json"])) for r in rows]
        return [item for item in out if code is None or item[0] == code]

    def row(self, filename="data.bin"):
        connection = connect(self.config.download.db_path)
        try:
            row = connection.execute(
                "SELECT * FROM artifacts WHERE filename = ?", (filename,)).fetchone()
            return {key: row[key] for key in row.keys()}
        finally:
            connection.close()

    def seed_retirement(self, run_id, old, delete=True, mark="retired"):
        target = self.target()
        if delete:
            target.unlink()
        connection = connect(self.config.download.db_path)
        try:
            ledger = Ledger(connection, Clock())
            ledger.update_artifact(
                self.row()["artifact_uid"], run_id, status=mark, local_path=None,
                actual_size=len(old), actual_sha256=sha(old))
            ledger.record_event(run_id, "capacity.retired", self.row()["artifact_uid"], {
                "artifact": self.row()["artifact_uid"], "old_sha256": sha(old),
                "old_size": len(old), "source_url": "http://fixture/files/ds/v1/data.bin"})
        finally:
            connection.close()


class PlannerTests(unittest.TestCase):
    def test_capacity_config_validation(self):
        self.assertIsNone(parse_capacity(None))
        settings = parse_capacity({"workspace_bytes": 10, "contract": "space_first"})
        self.assertEqual((settings.workspace_bytes, settings.contract), (10, "space_first"))
        for bad in ({"workspace_bytes": 0, "contract": "continuous"},
                    {"workspace_bytes": 10, "contract": "bogus"},
                    {"workspace_bytes": 10},
                    {"workspace_bytes": 10, "contract": "continuous", "x": 1}):
            with self.assertRaises(ConfigError):
                parse_capacity(bad)

    def test_ordering_counterexample(self):
        task_a = UpdateTask("a", "ds", "v1", "a.bin", 5, 0, 4, "http://x/a")
        task_b = UpdateTask("b", "ds", "v1", "b.bin", 3, 0, 6, "http://x/b")
        plan = plan_updates([task_a, task_b], 8, 13, "continuous")
        self.assertTrue(plan.feasible)
        self.assertEqual([s.task.artifact_uid for s in plan.steps], ["a", "b"])
        self.assertEqual([s.peak for s in plan.steps], [12, 13])
        self.assertEqual(plan.peak, 13)
        self.assertGreater(8 + 6, 13)  # discovery order (b first) would overflow

    def test_smallest_file_greedy_is_not_used(self):
        small = UpdateTask("s", "ds", "v1", "s.bin", 0, 0, 5, "http://x/s")
        large = UpdateTask("l", "ds", "v1", "l.bin", 8, 0, 6, "http://x/l")
        plan = plan_updates([small, large], 8, 14, "continuous")
        self.assertTrue(plan.feasible)
        self.assertEqual([s.task.artifact_uid for s in plan.steps], ["l", "s"])
        self.assertEqual([s.peak for s in plan.steps], [14, 11])

    def test_no_solution_reports_lower_bound(self):
        task_a = UpdateTask("a", "ds", "v1", "a.bin", 5, 0, 4, "http://x/a")
        task_b = UpdateTask("b", "ds", "v1", "b.bin", 3, 0, 6, "http://x/b")
        plan = plan_updates([task_a, task_b], 8, 12, "continuous")
        self.assertFalse(plan.feasible)
        self.assertEqual(plan.blocked, ("a", "b"))
        self.assertEqual(plan.lower_bounds["exact_peak"], 13)
        self.assertEqual(plan.lower_bounds["current_occupancy"], 8)
        self.assertEqual(plan.lower_bounds["final_occupancy"], 10)

    def test_single_artifact_contracts_disagree(self):
        task = UpdateTask("x", "ds", "v1", "x.bin", 100, 0, 100, "http://x")
        continuous = plan_updates([task], 100, 150, "continuous")
        self.assertFalse(continuous.feasible)
        self.assertEqual(continuous.lower_bounds["exact_peak"], 200)
        space = plan_updates([task], 100, 150, "space_first")
        self.assertTrue(space.feasible)
        self.assertTrue(space.steps[0].retire_old)
        self.assertEqual(space.steps[0].peak, 100)

    def test_part_and_quarantine_bytes_stay_occupied(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "art/sub").mkdir(parents=True)
            (root / "quar").mkdir()
            (root / "art/sub/f.bin").write_bytes(b"t" * 10)
            (root / "art/sub/g.bin.part").write_bytes(b"p" * 7)
            (root / "quar/h.bin").write_bytes(b"q" * 5)
            occ = measure_workspace(root / "art", root / "quar")
        self.assertEqual((occ.targets, occ.partials, occ.quarantined), (10, 7, 5))
        task = UpdateTask("t", "ds", "v1", "t.bin", 10, 7, 20, "http://x/t")
        self.assertTrue(plan_updates([task], occ.total, 35, "continuous").feasible)
        self.assertFalse(plan_updates([task], occ.total, 34, "continuous").feasible)


class PipelineContractTests(unittest.TestCase):
    def test_continuous_rejects_and_preserves_verified(self):
        old, new = b"o" * 100, b"n" * 100
        project = Project(self, 150, "continuous")
        first = project.run([pack("data.bin", old)], reset=True)
        self.assertEqual(first.summary["verified"], 1)
        second = project.run([pack("data.bin", new)])
        self.assertEqual(second.summary.get("capacity_blocked"), 1)
        self.assertEqual(second.status, "partial")
        target = project.target()
        self.assertEqual(target.read_bytes(), old)  # old verified preserved
        self.assertTrue(verify_file(target, 100, sha(old)).accepted)
        row = project.row()
        self.assertEqual(row["status"], "capacity_blocked")
        self.assertEqual(row["error_code"], "capacity_rejected")
        rejected = project.events(second.run_id, "capacity.rejected")
        self.assertEqual(len(rejected), 1)
        detail = rejected[0][2]
        self.assertEqual(detail["occupancy"], 100)
        self.assertEqual(detail["lower_bounds"]["exact_peak"], 200)
        self.assertEqual(detail["blocked"], [row["artifact_uid"]])
        # capacity rejection is audited apart from source validation failure
        self.assertEqual(project.events(second.run_id, "artifact.quarantined"), [])
        self.assertEqual(project.events(second.run_id, "artifact.failed"), [])

    def test_space_first_completes_with_unavailability_window(self):
        old, new = b"o" * 100, b"n" * 100
        project = Project(self, 150, "space_first")
        project.run([pack("data.bin", old)], reset=True)
        second = project.run([pack("data.bin", new)])
        self.assertEqual(second.summary["verified"], 1)
        self.assertEqual(project.target().read_bytes(), new)
        retired = project.events(second.run_id, "capacity.retired")
        self.assertEqual(len(retired), 1)
        self.assertEqual(retired[0][2]["old_sha256"], sha(old))
        self.assertEqual(retired[0][2]["old_size"], 100)
        self.assertTrue(retired[0][2]["unavailable"])
        restored = project.events(second.run_id, "capacity.restored")
        self.assertEqual(len(restored), 1)
        ordered = [code for code, _, _ in project.events(second.run_id)]
        self.assertLess(ordered.index("capacity.retired"), ordered.index("capacity.restored"))

    def test_space_first_failure_keeps_reviewable_state(self):
        old, new = b"o" * 100, b"n" * 100
        project = Project(self, 150, "space_first")
        project.run([pack("data.bin", old)], reset=True)
        second = project.run([pack("data.bin", new, None, None, 5)])
        row = project.row()
        self.assertEqual(row["status"], "retired")  # old retired, new failed
        self.assertFalse(project.target().exists())  # old bytes not falsely available
        self.assertEqual(second.summary.get("retired"), 1)
        failed = project.events(second.run_id, "capacity.restore_failed")
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0][2]["old_sha256"], sha(old))
        self.assertFalse(failed[0][2]["old_available"])
        self.assertTrue(project.events(second.run_id, "capacity.replan"))

    def test_ordering_counterexample_pipeline(self):
        project = Project(self, 13, "continuous")
        project.run([pack("a.bin", b"B" * 3), pack("b.bin", b"A" * 5)], reset=True)
        second = project.run([pack("a.bin", b"D" * 6), pack("b.bin", b"C" * 4)])
        self.assertEqual(second.summary["verified"], 2)
        plan = project.events(second.run_id, "capacity.plan")[0][2]
        self.assertTrue(plan["feasible"])
        connection = connect(project.config.download.db_path)
        try:
            names = {r["artifact_uid"]: r["filename"] for r in connection.execute(
                "SELECT artifact_uid, filename FROM artifacts")}
        finally:
            connection.close()
        order = [names[step["artifact"]] for step in plan["steps"]]
        self.assertEqual(order, ["b.bin", "a.bin"])  # not discovery order a, b
        self.assertEqual([step["peak"] for step in plan["steps"]], [12, 13])
        self.assertEqual(project.target("a.bin").read_bytes(), b"D" * 6)
        self.assertEqual(project.target("b.bin").read_bytes(), b"C" * 4)

    def test_head_size_change_is_source_failure_not_capacity(self):
        project = Project(self, 10000, "continuous")
        project.run([pack("data.bin", b"x" * 50)], reset=True)
        second = project.run([pack("data.bin", b"y" * 60, 999)])
        row = project.row()
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["error_code"], "remote_size_mismatch")
        self.assertEqual(project.events(second.run_id, "capacity.rejected"), [])
        self.assertTrue(project.events(second.run_id, "capacity.replan"))
        self.assertEqual(project.target().read_bytes(), b"x" * 50)

    def test_recovery_mid_download_resume(self):
        new = bytes(ord("a") + i % 26 for i in range(50))
        project = Project(self, 60, "continuous")
        project.run([pack("data.bin", b"o" * 10)], reset=True)
        partial = project.target().with_name("data.bin.part")
        partial.write_bytes(new[:20])  # interrupted mid-download
        second = project.run([pack("data.bin", new)])
        self.assertEqual(second.summary["verified"], 1)
        verified = project.events(second.run_id, "artifact.verified")[0][2]
        self.assertEqual(verified["resumed_from"], 20)  # part not double-billed
        self.assertEqual(project.target().read_bytes(), new)

    def test_recovery_before_replace_and_plan_gap(self):
        new = b"n" * 30
        project = Project(self, 40, "continuous")
        first = project.run([pack("data.bin", b"o" * 10)], reset=True)
        project.target().with_name("data.bin.part").write_bytes(new)  # full, unreplaced
        connection = connect(project.config.download.db_path)
        try:
            Ledger(connection, Clock()).record_event(
                first.run_id, "capacity.plan", "capacity", {"stale": True})
        finally:
            connection.close()
        second = project.run([pack("data.bin", new)])
        self.assertEqual(second.summary["verified"], 1)
        self.assertEqual(project.target().read_bytes(), new)
        plan = project.events(second.run_id, "capacity.plan")[0][2]
        self.assertEqual(plan["steps"][0]["part_bytes"], 30)  # rebuilt, not stale

    def test_recovery_after_retirement(self):
        old, new = b"o" * 40, b"n" * 60
        project = Project(self, 60, "continuous")
        first = project.run([pack("data.bin", old)], reset=True)
        project.seed_retirement(first.run_id, old, delete=True)
        second = project.run([pack("data.bin", new)])
        self.assertEqual(second.summary["verified"], 1)  # freed bytes not phantom-billed
        self.assertEqual(second.summary["skipped"], 0)
        self.assertEqual(project.target().read_bytes(), new)
        # a ledger-verified row with retirement evidence is downgraded, not trusted
        project2 = Project(self, 60, "continuous")
        first2 = project2.run([pack("data.bin", old)], reset=True)
        project2.seed_retirement(first2.run_id, old, delete=True, mark="verified")
        second2 = project2.run([pack("data.bin", new)])
        reconciled = project2.events(second2.run_id, "capacity.reconciled")
        self.assertEqual(reconciled[0][2]["to"], "retired")
        self.assertEqual(second2.summary["verified"], 1)

    def test_retirement_rollback_restores_availability(self):
        old = b"o" * 40
        project = Project(self, 40, "space_first")
        first = project.run([pack("data.bin", old)], reset=True)
        project.seed_retirement(first.run_id, old, delete=False)  # delete never ran
        second = project.run([pack("data.bin", old)])  # portal unchanged
        self.assertEqual(second.summary["skipped"], 1)
        self.assertEqual(project.target().read_bytes(), old)
        reconciled = project.events(second.run_id, "capacity.reconciled")
        self.assertEqual(reconciled[0][2]["to"], "verified")


class _StubResponse:
    status = 200
    headers: dict = {}

    def __init__(self, body):
        self.body = body
        self.content_length = None

    def iter_chunks(self, size):
        for index in range(0, len(self.body), 10):
            yield self.body[index:index + 10]

    def close(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None


class _RangeIgnoringClient:
    def __init__(self, body):
        self.body = body

    def open(self, method, url, headers=None):
        return _StubResponse(self.body)  # always 200, Range ignored


class RangeIgnoredCapacityTests(unittest.TestCase):
    def test_range_ignored_recomputes_and_guards(self):
        body = b"z" * 50
        seen = []
        with tempfile.TemporaryDirectory() as directory:
            project = Project(self, None)
            project.root = Path(directory)
            project.run([pack("data.bin", body)], reset=True)
            config = project.config
            target = config.download.artifact_dir / "ds/v1/data.bin"
            target.unlink()
            partial = target.with_name("data.bin.part")
            partial.write_bytes(body[:20])
            downloader = Downloader(config, _RangeIgnoringClient(body), Clock(),
                                    on_event=lambda code, subject, detail: seen.append(code))
            ref = ArtifactRef("ds", "t", "v1", "v1", "stable", "2026-01-01", "data.bin",
                              "http://127.0.0.1:1/files/ds/v1/data.bin",
                              "application/octet-stream", 50, sha(body))
            refused = downloader.download(ref, part_limit=49)
            self.assertEqual(refused.error_code, "capacity_exceeded")
            self.assertIn("download.range_ignored", seen)
            self.assertEqual(partial.stat().st_size, 40)  # accounted bytes kept
            accepted = downloader.download(ref, part_limit=50)
            self.assertTrue(accepted.accepted)
            self.assertEqual(target.read_bytes(), body)


if __name__ == "__main__":
    unittest.main()

import json
import tempfile
import unittest
from pathlib import Path

from helpers import free_port, portal_for, write_project
from releaseharbor.config import load_config
from releaseharbor.db import connect
from releaseharbor.pipeline import run_pipeline


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = load_config(write_project(Path(self.temp.name), free_port()))
        self.portal = portal_for(self.config).start()

    def tearDown(self):
        self.portal.stop()
        self.temp.cleanup()

    def test_end_to_end_audit_and_quarantine(self):
        outcome = run_pipeline(self.config, reset=True)
        self.assertEqual(outcome.status, "partial")
        self.assertEqual(outcome.summary["datasets"], 3)
        self.assertEqual(outcome.summary["versions"], 5)
        self.assertEqual(outcome.summary["artifacts"], 9)
        self.assertEqual(outcome.summary["verified"], 8)
        self.assertEqual(outcome.summary["quarantined"], 1)
        self.assertEqual(outcome.summary["failed"], 0)
        self.assertEqual(outcome.summary["blocked_targets"], 0)
        report_path = self.config.root / outcome.reports["report_json"]
        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(report["run"]["status"], "partial")
        self.assertEqual(report["summary"], outcome.summary)
        self.assertEqual(len(report["events"]), outcome.summary["events"])
        audit = (self.config.root / outcome.reports["audit_jsonl"]).read_text(encoding="utf-8")
        self.assertIn("download.retry", audit)
        self.assertIn("artifact.quarantined", audit)
        manifest = (self.config.root / outcome.reports["manifest_csv"]).read_text(encoding="utf-8")
        self.assertEqual(len(manifest.splitlines()), 10)
        self.assertTrue(
            (self.config.download.quarantine_dir / "air-quality/2025-01/samples.csv").is_file()
        )
        budget_notes = self.config.download.artifact_dir / "civic-budget/2025.1/budget_notes.txt"
        self.assertTrue(budget_notes.is_file())
        self.assertTrue((self.config.root / outcome.reports["report_markdown"]).is_file())

    def test_rerun_skips_verified_files_and_tamper_forces_redownload(self):
        first = run_pipeline(self.config, reset=True)
        self.assertEqual(first.summary["verified"], 8)
        tampered = self.config.download.artifact_dir / "civic-budget/2025.1/budget_lines.csv"
        tampered.write_bytes(b"tampered local copy\n")
        second = run_pipeline(self.config)
        self.assertNotEqual(first.run_id, second.run_id)
        self.assertEqual(second.summary["verified"], 1)
        self.assertEqual(second.summary["skipped"], 7)
        self.assertEqual(second.summary["quarantined"], 1)
        self.assertEqual(second.summary["artifacts"], 9)
        self.assertNotEqual(tampered.read_bytes(), b"tampered local copy\n")
        connection = connect(self.config.download.db_path)
        try:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0], 9)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 2)
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()

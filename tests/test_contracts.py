import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from helpers import free_port, portal_for, small_fixture, write_project
from releaseharbor.catalog import (
    PREVIEW,
    STRICT,
    current_view,
    load_declarations,
    reconcile,
)
from releaseharbor.clock import Clock
from releaseharbor.config import load_config
from releaseharbor.db import connect
from releaseharbor.errors import DiscoveryError
from releaseharbor.ledger import Ledger
from releaseharbor.pipeline import run_pipeline
from releaseharbor.queries import query_artifact, query_version


class ContractTests(unittest.TestCase):
    """Strict vs preview contracts over the same runs of the A/B portal."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = load_config(
            write_project(self.root, free_port(), fixture_obj=small_fixture())
        )
        self.portal = portal_for(self.config).start()

    def tearDown(self):
        self.portal.stop()
        self.temp.cleanup()

    def limited(self, **overrides):
        return replace(
            self.config, discovery=replace(self.config.discovery, **overrides)
        )

    def test_strict_and_preview_differ_for_same_budgeted_input(self):
        full = run_pipeline(self.config, reset=True)
        self.assertEqual(full.views[STRICT]["status"], "published")
        self.assertEqual(
            full.views[STRICT]["datasets"],
            {"alpha": {"versions": ["v1", "v2"]}, "beta": {"versions": ["v1", "v2"]}},
        )
        budgeted = run_pipeline(self.limited(max_versions=1))
        self.assertEqual(budgeted.views[STRICT]["status"], "withheld")
        self.assertEqual(budgeted.views[PREVIEW]["status"], "published")
        self.assertEqual(
            sorted(budgeted.views[PREVIEW]["datasets"]["alpha"]["versions"]), ["v1"]
        )
        connection = connect(self.config.download.db_path)
        try:
            # Strict retains the last complete view; preview reflects this run.
            strict_v2 = query_version(connection, STRICT, "alpha", "v2")
            self.assertEqual(strict_v2["state"], "present")
            self.assertEqual(strict_v2["view_run_id"], full.run_id)
            preview_v2 = query_version(connection, PREVIEW, "alpha", "v2")
            self.assertEqual(preview_v2["state"], "unknown")
            self.assertEqual(preview_v2["reason"], "budget_skipped")
            self.assertEqual(preview_v2["view_run_id"], budgeted.run_id)
            # Absence provable only under the retained strict view.
            strict_v9 = query_version(connection, STRICT, "alpha", "v9")
            self.assertEqual(strict_v9["state"], "absent_proven")
            preview_v9 = query_version(connection, PREVIEW, "alpha", "v9")
            self.assertEqual(preview_v9["state"], "unknown")
            # Preview never downgrades a covered branch.
            preview_v1 = query_version(connection, PREVIEW, "alpha", "v1")
            self.assertEqual(preview_v1["state"], "present")
            preview_file = query_artifact(connection, PREVIEW, "alpha", "v1", "a-one.txt")
            self.assertEqual(preview_file["state"], "present")
        finally:
            connection.close()

    def test_resume_cut_point_after_interrupted_and_failed_runs(self):
        cut = self.limited(max_pages=2)
        with self.assertRaises(DiscoveryError):
            run_pipeline(cut, reset=True)
        connection = connect(self.config.download.db_path)
        failed = connection.execute(
            "SELECT run_id, status FROM runs ORDER BY started_at"
        ).fetchall()
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["status"], "failed")
        cut_decls = load_declarations(connection, failed[0]["run_id"])
        self.assertTrue(cut_decls)
        self.assertTrue(all(d["stability"] == "unverified" for d in cut_decls))
        outcomes = {
            d["outcome"] for decl in cut_decls for d in decl["decisions"]
        }
        self.assertIn("fetch_failed", outcomes)
        # Simulate a crashed run that never finished.
        ledger = Ledger(connection, Clock())
        ledger.start_run("ghost-run", "fingerprint")
        connection.close()

        recovered = run_pipeline(self.config)
        self.assertEqual(recovered.views[STRICT]["status"], "published")
        connection = connect(self.config.download.db_path)
        try:
            ghost = connection.execute(
                "SELECT status FROM runs WHERE run_id = 'ghost-run'"
            ).fetchone()
            self.assertEqual(ghost["status"], "interrupted")
            events = connection.execute(
                "SELECT detail_json FROM events WHERE run_id = ? AND code = 'run.recovered'",
                (recovered.run_id,),
            ).fetchall()
            self.assertEqual(len(events), 1)
            self.assertIn("ghost-run", json.loads(events[0]["detail_json"])["interrupted_runs"])
            decls = load_declarations(connection, recovered.run_id)
            self.assertEqual(reconcile(decls), [])
            self.assertTrue(all(d["stability"] == "confirmed" for d in decls))
            answer = query_version(connection, STRICT, "alpha", "v2")
            self.assertEqual(answer["view_run_id"], recovered.run_id)
            self.assertEqual(answer["state"], "present")
        finally:
            connection.close()

        # A later budget-cut run must not inherit children from earlier runs.
        narrowed = run_pipeline(self.limited(max_datasets=1))
        self.assertEqual(narrowed.views[STRICT]["status"], "withheld")
        connection = connect(self.config.download.db_path)
        try:
            preview_beta = query_version(connection, PREVIEW, "beta", "v1")
            self.assertEqual(preview_beta["state"], "unknown")
            self.assertEqual(preview_beta["reason"], "budget_skipped")
            self.assertEqual(preview_beta["view_run_id"], narrowed.run_id)
            strict_beta = query_version(connection, STRICT, "beta", "v1")
            self.assertEqual(strict_beta["state"], "present")
            self.assertEqual(strict_beta["view_run_id"], recovered.run_id)
            retained = current_view(connection, STRICT)
            self.assertEqual(retained["run_id"], recovered.run_id)
        finally:
            connection.close()

    def test_report_carries_coverage_and_catalog(self):
        outcome = run_pipeline(self.config, reset=True)
        report_path = self.config.root / outcome.reports["report_json"]
        report = json.loads(report_path.read_text(encoding="utf-8"))
        declarations = report["coverage"]["declarations"]
        self.assertEqual(len(declarations), 7)  # 1 portal + 2 datasets + 4 versions
        self.assertTrue(all(d["stability"] == "confirmed" for d in declarations))
        self.assertEqual(report["summary"]["coverage"]["confirmed"], 7)
        self.assertEqual(report["catalog"]["strict"]["status"], "published")
        self.assertEqual(report["catalog"]["preview"]["status"], "published")
        self.assertEqual(report["catalog"]["preview"]["unknowns"], [])
        portal = next(d for d in declarations if d["level"] == "portal")
        self.assertEqual(portal["candidates"], 2)
        self.assertEqual(sorted(portal["children"]), sorted(portal["children"].keys()))
        self.assertEqual(len(portal["children"]), 2)
        markdown = (self.config.root / outcome.reports["report_markdown"]).read_text(
            encoding="utf-8"
        )
        self.assertIn("Coverage declarations", markdown)


if __name__ == "__main__":
    unittest.main()

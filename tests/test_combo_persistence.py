import json
from pathlib import Path

from combo_case import ComboFixtureCase
from releaseharbor.combo_models import CONTRACT_INTERNAL, CONTRACT_PUBLIC


class ReproducibilityTests(ComboFixtureCase):
    def test_same_version_and_input_reproduce_same_decision_and_keep_evidence(self):
        first = self.query(CONTRACT_PUBLIC, [self.z(), self.src(3, "FICT-W-1.0")])
        decision_id = first.decision["decision_id"]
        second = self.query(CONTRACT_PUBLIC, [self.z(), self.src(3, "FICT-W-1.0")])
        # Deterministic identity and identical evidence, second call is not a new row.
        self.assertEqual(second.decision["decision_id"], decision_id)
        self.assertEqual(first.decision, second.decision)
        self.assertTrue(first.saved)
        self.assertFalse(second.saved)
        stored = self.ledger.get_combo_decision(decision_id)
        self.assertEqual(stored, first.decision)
        # The decision record exists as an independent audit artifact.
        record = self.config.combo.decision_dir / f"{decision_id}.json"
        self.assertTrue(record.is_file())
        on_disk = json.loads(record.read_text(encoding="utf-8"))
        self.assertEqual(on_disk, first.decision)

    def test_new_rule_table_version_creates_new_decision_without_overwriting(self):
        request_sources = [self.x(), self.y()]
        internal = self.query(CONTRACT_INTERNAL, request_sources)
        old_id = internal.decision["decision_id"]
        old_evidence = self.ledger.get_combo_decision(old_id)
        # Simulate a new released rule table version by writing a copied table
        # with a bumped version; old evidence must remain untouched.
        new_table_path = self.config.root / "fixtures" / "combo-rules-v2.json"
        payload = json.loads(
            self.config.combo.rule_table.read_text(encoding="utf-8")
        )
        payload["rule_table_version"] = "combo-rules-2026.2"
        new_table_path.write_text(json.dumps(payload), encoding="utf-8")
        import dataclasses
        bumped = dataclasses.replace(self.config, combo=dataclasses.replace(
            self.config.combo, rule_table=new_table_path
        ))
        from releaseharbor.combo_service import run_combo_query
        second = run_combo_query(
            bumped, self.connection, self.clock,
            {
                "contract_id": CONTRACT_INTERNAL,
                "rule_table_version": "combo-rules-2026.2",
                "sources": request_sources,
            },
        )
        self.assertNotEqual(second.decision["decision_id"], old_id)
        self.assertEqual(
            second.decision["rule_table"]["version"], "combo-rules-2026.2"
        )
        # The old decision is still on record and byte-identical.
        self.assertEqual(self.ledger.get_combo_decision(old_id), old_evidence)
        index = {item["decision_id"] for item in self.ledger.combo_decision_index()}
        self.assertIn(old_id, index)
        self.assertIn(second.decision["decision_id"], index)

    def test_audit_trail_and_markdown_are_written_independently(self):
        outcome = self.query(CONTRACT_INTERNAL, [self.x(), self.y()])
        directory = self.config.combo.decision_dir
        markdown = directory / f"{outcome.decision['decision_id']}.md"
        audit = directory / "combo-audit.jsonl"
        self.assertTrue(markdown.is_file())
        self.assertTrue(audit.is_file())
        text = markdown.read_text(encoding="utf-8")
        self.assertIn("Fictional combination decision", text)
        self.assertIn("x-restricted", text)
        lines = [line for line in audit.read_text(encoding="utf-8").splitlines() if line]
        self.assertTrue(lines)
        event = json.loads(lines[-1])
        self.assertEqual(event["decision_id"], outcome.decision["decision_id"])
        # Intake reports are not touched by combination auditing.
        intake_report = self.config.download.report_dir / "report.json"
        self.assertTrue(intake_report.is_file())
        self.assertFalse(
            (self.config.download.report_dir / "combo-audit.jsonl").exists()
        )

    def test_ledger_acceptance_state_is_unchanged_by_combo_queries(self):
        before = {
            row["artifact_uid"]: (row["status"], row["actual_sha256"])
            for row in self.connection.execute(
                "SELECT artifact_uid, status, actual_sha256 FROM artifacts"
            ).fetchall()
        }
        self.query(CONTRACT_PUBLIC, [self.x(), self.y()])
        self.query(CONTRACT_INTERNAL, [self.z()])
        after = {
            row["artifact_uid"]: (row["status"], row["actual_sha256"])
            for row in self.connection.execute(
                "SELECT artifact_uid, status, actual_sha256 FROM artifacts"
            ).fetchall()
        }
        self.assertEqual(before, after)

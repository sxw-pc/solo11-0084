from combo_case import ComboFixtureCase
from releaseharbor.combo_models import CONTRACT_PUBLIC


class GateTests(ComboFixtureCase):
    def _codes(self, decision) -> set[str]:
        return {failure["code"] for failure in decision["gate_failures"]}

    def test_unknown_label_is_undecidable_not_allowed(self):
        outcome = self.query(
            CONTRACT_PUBLIC, [self.src(0, "FICT-UNHEARD-OF-9.9")]
        )
        self.assertEqual(outcome.decision["status"], "undecidable")
        self.assertIn("unknown_fictional_label", self._codes(outcome.decision))
        self.assertEqual(outcome.decision["solutions"], [])

    def test_stale_rule_table_version_is_undecidable(self):
        outcome = self.query(
            CONTRACT_PUBLIC, [self.x()], version="combo-rules-1999.0"
        )
        self.assertEqual(outcome.decision["status"], "undecidable")
        self.assertIn("stale_rule_table_version", self._codes(outcome.decision))

    def test_artifact_not_accepted_in_ledger_blocks_delivery(self):
        # The quarantined air-quality samples.csv is in the ledger but not accepted.
        row = self.connection.execute(
            "SELECT artifact_uid, expected_sha256 FROM artifacts "
            "WHERE status = 'checksum_mismatch'"
        ).fetchone()
        source = {
            "artifact_uid": row["artifact_uid"],
            "fictional_label": "FICT-W-1.0",
            "expected_sha256": row["expected_sha256"],
        }
        outcome = self.query(CONTRACT_PUBLIC, [source])
        self.assertEqual(outcome.decision["status"], "undecidable")
        self.assertIn("artifact_not_accepted", self._codes(outcome.decision))

    def test_artifact_missing_from_ledger_blocks_delivery(self):
        source = dict(self.x())
        source["artifact_uid"] = "deadbeef" * 8
        outcome = self.query(CONTRACT_PUBLIC, [source])
        self.assertEqual(outcome.decision["status"], "undecidable")
        self.assertIn("artifact_not_in_ledger", self._codes(outcome.decision))

    def test_disk_byte_mismatch_blocks_delivery(self):
        source = self.x()
        row = self.connection.execute(
            "SELECT local_path FROM artifacts WHERE artifact_uid = ?",
            (source["artifact_uid"],),
        ).fetchone()
        target = self.config.root / row["local_path"]
        original = target.read_bytes()
        try:
            target.write_bytes(original + b"tampered\n")
            outcome = self.query(CONTRACT_PUBLIC, [source])
            self.assertEqual(outcome.decision["status"], "undecidable")
            self.assertIn("disk_bytes_mismatch", self._codes(outcome.decision))
        finally:
            target.write_bytes(original)

    def test_expected_digest_mismatch_blocks_delivery(self):
        source = dict(self.x())
        source["expected_sha256"] = "0" * 64
        outcome = self.query(CONTRACT_PUBLIC, [source])
        self.assertEqual(outcome.decision["status"], "undecidable")
        self.assertIn("expected_digest_mismatch", self._codes(outcome.decision))

    def test_duplicate_artifact_uid_is_rejected(self):
        outcome = self.query(CONTRACT_PUBLIC, [self.x(), self.x()])
        self.assertEqual(outcome.decision["status"], "undecidable")
        self.assertIn("duplicate_artifact_uid", self._codes(outcome.decision))

    def test_evidence_retains_sources_and_rule_versions(self):
        outcome = self.query(CONTRACT_PUBLIC, [self.x(), self.y()])
        for source in outcome.decision["sources"]:
            self.assertTrue(source["fictional_label"])
            self.assertIn("ledger_status", source)
            self.assertIn("disk_recheck", source)
        self.assertEqual(
            outcome.decision["rule_table"]["version"], "combo-rules-2026.1"
        )

    def test_unbounded_combination_space_is_undecidable(self):
        import dataclasses
        # With max_combinations=1, even a two-branch single source exceeds the
        # bound; the engine refuses rather than truncating the search.
        tight = dataclasses.replace(
            self.config,
            combo=dataclasses.replace(self.config.combo, max_combinations=1),
        )
        from releaseharbor.combo_service import run_combo_query
        outcome = run_combo_query(
            tight, self.connection, self.clock,
            {
                "contract_id": CONTRACT_PUBLIC,
                "rule_table_version": self.rule_table.version,
                "sources": [self.z()],
            },
        )
        self.assertEqual(outcome.decision["status"], "undecidable")
        self.assertIn("unbounded_combination", self._codes(outcome.decision))

    def test_no_derivatives_label_is_blocked_for_public_but_allowed_internal(self):
        import json
        table_path = self.config.combo.rule_table
        payload = json.loads(table_path.read_text(encoding="utf-8"))
        payload["labels"].append(
            {
                "label": "FICT-ND-1.0",
                "branches": [
                    {
                        "branch_id": "nd-only",
                        "obligations": [
                            {"type": "attribution", "template": "AT-W"},
                            {"type": "no_derivatives"},
                        ],
                    }
                ],
            }
        )
        table_path.write_text(json.dumps(payload), encoding="utf-8")
        source = self.src(5, "FICT-ND-1.0")
        from releaseharbor.combo_models import CONTRACT_INTERNAL
        internal = self.query(CONTRACT_INTERNAL, [source])
        self.assertEqual(internal.decision["status"], "deliverable")
        public = self.query(CONTRACT_PUBLIC, [source])
        self.assertEqual(public.decision["status"], "undecidable")
        self.assertIn(
            "no_contract_eligible_branch", self._codes(public.decision)
        )

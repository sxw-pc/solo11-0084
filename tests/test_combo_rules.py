import json
import tempfile
import unittest
from pathlib import Path

from helpers import ROOT
from releaseharbor.combo_rules import load_rule_table
from releaseharbor.errors import RuleTableError


class RuleTableTests(unittest.TestCase):
    def setUp(self):
        self.table = load_rule_table(ROOT / "fixtures" / "combo-rules.json")

    def test_versioned_table_loads_fictional_labels_and_aliases(self):
        self.assertEqual(self.table.version, "combo-rules-2026.1")
        self.assertEqual(len(self.table.fingerprint), 64)
        x = self.table.resolve_label("FICT-X-1.0")
        self.assertEqual([branch.branch_id for branch in x.branches],
                         ["x-restricted", "x-open"])
        aliased = self.table.resolve_label("FICT-Y-ALIAS")
        self.assertEqual(aliased.label, "FICT-Y-1.0")

    def test_unknown_label_is_rejected_not_default_allowed(self):
        with self.assertRaises(RuleTableError):
            self.table.resolve_label("FICT-DOES-NOT-EXIST")

    def _write(self, payload: dict) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "rules.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def _base(self) -> dict:
        return json.loads((ROOT / "fixtures" / "combo-rules.json").read_text())

    def test_self_contradictory_branch_is_rejected(self):
        payload = self._base()
        # One branch cannot simultaneously forbid derivatives and demand share-alike.
        payload["labels"][0]["branches"][0]["obligations"] = [
            {"type": "attribution", "template": "AT-X-PUB"},
            {"type": "no_derivatives"},
            {"type": "share_alike"},
        ]
        with self.assertRaises(RuleTableError):
            load_rule_table(self._write(payload))

    def test_circular_alias_is_rejected(self):
        payload = self._base()
        payload["aliases"] = [
            {"alias": "FICT-LOOP-A", "target": "FICT-LOOP-B"},
            {"alias": "FICT-LOOP-B", "target": "FICT-LOOP-A"},
        ]
        with self.assertRaises(RuleTableError):
            load_rule_table(self._write(payload))

    def test_alias_to_unknown_label_is_rejected(self):
        payload = self._base()
        payload["aliases"] = [{"alias": "FICT-ORPHAN", "target": "FICT-NOWHERE"}]
        with self.assertRaises(RuleTableError):
            load_rule_table(self._write(payload))

    def test_unbounded_branch_count_is_rejected(self):
        payload = self._base()
        payload["max_branches_per_label"] = 1
        # FICT-X-1.0 declares two branches, exceeding the cap of one.
        with self.assertRaises(RuleTableError):
            load_rule_table(self._write(payload))

    def test_mutually_exclusive_templates_in_one_branch_are_rejected(self):
        payload = self._base()
        payload["labels"][0]["branches"][0]["obligations"].append(
            {"type": "attribution", "template": "AT-Y-PUB"}
        )
        with self.assertRaises(RuleTableError):
            load_rule_table(self._write(payload))

    def test_unknown_template_reference_is_rejected(self):
        payload = self._base()
        payload["labels"][1]["branches"][0]["obligations"][0]["template"] = "AT-MISSING"
        with self.assertRaises(RuleTableError):
            load_rule_table(self._write(payload))

    def test_unknown_top_level_key_is_rejected(self):
        payload = self._base()
        payload["surprise"] = True
        with self.assertRaises(RuleTableError):
            load_rule_table(self._write(payload))


if __name__ == "__main__":
    unittest.main()

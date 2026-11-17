from combo_case import ComboFixtureCase
from releaseharbor.combo_models import CONTRACT_INTERNAL, CONTRACT_PUBLIC


class TwoContractTests(ComboFixtureCase):
    def test_internal_contract_allows_no_public_branch_and_lists_obligations(self):
        # X has (attribution + no_public) vs (attribution + share_alike); Y is
        # attribution + share_alike. Internally, X's restricted branch avoids the
        # mutually exclusive public attribution template clash.
        outcome = self.query(CONTRACT_INTERNAL, [self.x(), self.y()])
        decision = outcome.decision
        self.assertEqual(decision["status"], "deliverable")
        self.assertEqual(decision["rule_table"]["version"], "combo-rules-2026.1")
        # The surviving solution uses X's no_public branch.
        selection = {
            witness["artifact_uid"]: witness["branch_id"]
            for witness in decision["solutions"][0]["selection"]
        }
        self.assertEqual(selection[self.x()["artifact_uid"]], "x-restricted")
        self.assertEqual(selection[self.y()["artifact_uid"]], "y-open")
        types = {item["type"] for item in decision["solutions"][0]["obligations"]}
        self.assertIn("no_public", types)
        self.assertIn("attribution", types)
        # Each attribution template keeps the witness that demands it; the two
        # templates differ so they stay separate obligation elements.
        attribution = [
            item for item in decision["solutions"][0]["obligations"]
            if item["type"] == "attribution"
        ]
        witnesses = {uid for item in attribution for uid in item["witness_uids"]}
        self.assertEqual(
            witnesses,
            {self.x()["artifact_uid"], self.y()["artifact_uid"]},
        )
        # The restricted X source carries the no_public obligation on its own.
        no_public = next(
            item for item in decision["solutions"][0]["obligations"]
            if item["type"] == "no_public"
        )
        self.assertEqual(no_public["witness_uids"], [self.x()["artifact_uid"]])

    def test_public_contract_has_no_feasible_combo_and_explains_two_source_conflict(self):
        outcome = self.query(CONTRACT_PUBLIC, [self.x(), self.y()])
        decision = outcome.decision
        self.assertEqual(decision["status"], "infeasible")
        self.assertEqual(decision["solutions"], [])
        self.assertTrue(decision["conflicts"])
        conflict = decision["conflicts"][0]
        self.assertEqual(conflict["kind"], "mutually_exclusive_attribution_templates")
        # Minimal explanation involves exactly the two contributing sources.
        self.assertEqual(
            sorted(conflict["artifact_uids"]),
            sorted([self.x()["artifact_uid"], self.y()["artifact_uid"]]),
        )
        self.assertEqual(conflict["templates"], ["AT-X-PUB", "AT-Y-PUB"])

    def test_public_contract_rejects_no_public_branch_outright(self):
        # X alone: its restricted branch is blocked for public redistribution,
        # but its open branch still works, so the decision stays deliverable.
        outcome = self.query(CONTRACT_PUBLIC, [self.x()])
        self.assertEqual(outcome.decision["status"], "deliverable")
        branch = outcome.decision["solutions"][0]["selection"][0]["branch_id"]
        self.assertEqual(branch, "x-open")


class ParetoTests(ComboFixtureCase):
    def test_all_non_dominated_feasible_sets_are_returned_with_witnesses(self):
        # Z has two branches that both satisfy the public contract but carry
        # different obligation sets; W adds an attribution-only requirement.
        w = self.src(3, "FICT-W-1.0")
        outcome = self.query(CONTRACT_PUBLIC, [self.z(), w])
        decision = outcome.decision
        self.assertEqual(decision["status"], "deliverable")
        # Two distinct non-dominated obligation sets, not just the first rule.
        self.assertEqual(len(decision["solutions"]), 2)
        digests = []
        for solution in decision["solutions"]:
            z_witness = next(
                item for item in solution["selection"]
                if item["artifact_uid"] == self.z()["artifact_uid"]
            )
            digests.append(z_witness["branch_id"])
            self.assertEqual(len(solution["selection"]), 2)
            for witness in solution["selection"]:
                self.assertEqual(
                    witness["rule_table_version"], "combo-rules-2026.1"
                )
        self.assertEqual(sorted(digests), ["z-attrib-only", "z-attrib-share"])
        # Neither solution's obligation set contains the other.
        def keyset(solution):
            return frozenset(
                item["type"] if item["template"] is None
                else f"{item['type']}:{item['template']}"
                for item in solution["obligations"]
            )

        sets = [keyset(solution) for solution in decision["solutions"]]
        self.assertFalse(sets[0] < sets[1] or sets[1] < sets[0])

    def test_dominated_solution_is_pruned(self):
        # Q has two branches sharing the same attribution template; the
        # share_alike branch is a strict superset of the attribution-only
        # branch's obligations, so only the smaller non-dominated set survives.
        q = self.src(4, "FICT-Q-1.0")
        outcome = self.query(CONTRACT_PUBLIC, [q])
        decision = outcome.decision
        self.assertEqual(decision["status"], "deliverable")
        self.assertEqual(len(decision["solutions"]), 1)
        q_branch = decision["solutions"][0]["selection"][0]["branch_id"]
        self.assertEqual(q_branch, "q-attrib-only")
        self.assertNotIn(
            "share_alike",
            [item["type"] for item in decision["solutions"][0]["obligations"]],
        )

"""Tests for offline synthetic budget disclosure adjudication."""

from __future__ import annotations

import hashlib
import itertools
import json
import tempfile
import unittest
from pathlib import Path

from helpers import free_port, portal_for, write_project
from releaseharbor.budget import (
    adjudicate,
    adjudicate_matrix,
    cell_feasible_range,
    suppression_is_safe,
)
from releaseharbor.budgetinput import SensitiveCell, parse_budget_csv
from releaseharbor.config import load_config
from releaseharbor.db import connect
from releaseharbor.errors import BudgetError
from releaseharbor.pipeline import run_pipeline

HEADER = "department,fiscal_year,account,amount_usd"

# Core 2x2 sample: A=(X:2, Y:8), B=(X:8, Y:2); every row/column total is 10.
ROWS_2X2 = [("A", 2025, "X", 2), ("A", 2025, "Y", 8), ("B", 2025, "X", 8), ("B", 2025, "Y", 2)]


def csv_text(rows, header=HEADER):
    return "\n".join([header] + [",".join(str(part) for part in row) for row in rows]) + "\n"


def matrix(rows):
    return parse_budget_csv(csv_text(rows))[0]


def decision_for(report, year, contract):
    for decision in report["decisions"]:
        if decision["fiscal_year"] == year and decision["contract"] == contract:
            return decision
    raise KeyError((year, contract))


def suppressed_labels(decision):
    return {(cell["department"], cell["account"]) for cell in decision["suppressed"]}


def check_witness(testcase, decision, assessment, witness):
    """A witness must satisfy every published fact and differ at the sensitive cell."""
    departments = decision["departments"]
    accounts = decision["accounts"]
    values = witness["matrix"]["values"]
    publication = decision["publication"]
    testcase.assertEqual(witness["matrix"]["departments"], departments)
    testcase.assertEqual(witness["matrix"]["accounts"], accounts)
    for department, row in zip(departments, values):
        testcase.assertEqual(sum(row), publication["row_totals"][department])
    if "col_totals" in publication:
        for j, account in enumerate(accounts):
            testcase.assertEqual(
                sum(values[i][j] for i in range(len(departments))),
                publication["col_totals"][account],
            )
    visible = {(c["department"], c["account"]): c["amount_usd"] for c in publication["cells"]}
    for i, department in enumerate(departments):
        for j, account in enumerate(accounts):
            testcase.assertIsInstance(values[i][j], int)
            testcase.assertGreaterEqual(values[i][j], 0)
            if (department, account) in visible:
                testcase.assertEqual(values[i][j], visible[(department, account)])
    cell = assessment["cell"]
    observed = values[departments.index(cell["department"])][accounts.index(cell["account"])]
    testcase.assertEqual(observed, witness["cell_value"])


def check_assessment(testcase, decision):
    """Every sensitive cell needs a non-trivial range and two valid witnesses."""
    testcase.assertEqual(decision["status"], "publishable")
    for assessment in decision["assessments"]:
        testcase.assertLess(assessment["minimum"], assessment["maximum"])
        witnesses = assessment["witnesses"]
        testcase.assertEqual(len(witnesses), 2)
        values = sorted(w["cell_value"] for w in witnesses)
        testcase.assertEqual(values, [assessment["minimum"], assessment["maximum"]])
        testcase.assertNotEqual(witnesses[0]["matrix"]["values"], witnesses[1]["matrix"]["values"])
        for witness in witnesses:
            check_witness(testcase, decision, assessment, witness)
        # Neither witness is marked as the true source value.
        for witness in witnesses:
            testcase.assertNotIn("is_source", witness)
            testcase.assertNotIn("source", witness["matrix"])


class TwoByTwoInferenceTests(unittest.TestCase):
    def test_single_suppression_leaks_through_row_total(self):
        m = matrix(ROWS_2X2)
        # Suppressing only A-X: row A total 10 and visible A-Y=8 pin A-X to 2.
        self.assertEqual(cell_feasible_range(m, "marginals", {("A", "X")}, ("A", "X")), (2, 2))
        self.assertEqual(cell_feasible_range(m, "details", {("A", "X")}, ("A", "X")), (2, 2))
        self.assertFalse(
            suppression_is_safe(m, "marginals", [("A", "X")], {("A", "X")})
        )

    def test_marginals_contract_requires_full_cycle(self):
        m = matrix(ROWS_2X2)
        decision = adjudicate_matrix(m, [SensitiveCell(2025, "A", "X")], "marginals")
        self.assertEqual(decision["status"], "publishable")
        # The whole feasible cycle (all four cells) must be suppressed.
        self.assertEqual(
            suppressed_labels(decision), {("A", "X"), ("A", "Y"), ("B", "X"), ("B", "Y")}
        )
        self.assertEqual(decision["search"]["minimum_extra_suppressed"], 3)
        self.assertEqual(decision["search"]["equivalent_schemes"], 1)
        self.assertEqual(decision["publication"]["row_totals"], {"A": 10, "B": 10})
        self.assertEqual(decision["publication"]["col_totals"], {"X": 10, "Y": 10})
        self.assertEqual(decision["publication"]["cells"], [])
        assessment = decision["assessments"][0]
        self.assertEqual((assessment["minimum"], assessment["maximum"]), (0, 10))
        check_assessment(self, decision)

    def test_details_contract_keeps_other_row_visible(self):
        m = matrix(ROWS_2X2)
        decision = adjudicate_matrix(m, [SensitiveCell(2025, "A", "X")], "details")
        self.assertEqual(decision["status"], "publishable")
        # Only A-X and A-Y are suppressed; B-X/B-Y stay visible.
        self.assertEqual(suppressed_labels(decision), {("A", "X"), ("A", "Y")})
        self.assertEqual(decision["search"]["minimum_extra_suppressed"], 1)
        visible = {(c["department"], c["account"]): c["amount_usd"] for c in decision["publication"]["cells"]}
        self.assertEqual(visible, {("B", "X"): 8, ("B", "Y"): 2})
        self.assertEqual(decision["publication"]["row_totals"], {"A": 10, "B": 10})
        self.assertNotIn("col_totals", decision["publication"])
        assessment = decision["assessments"][0]
        self.assertEqual((assessment["minimum"], assessment["maximum"]), (0, 10))
        check_assessment(self, decision)

    def test_contracts_yield_different_publications_for_same_input(self):
        m = matrix(ROWS_2X2)
        sensitive = [SensitiveCell(2025, "A", "X")]
        marginals = adjudicate_matrix(m, sensitive, "marginals")
        details = adjudicate_matrix(m, sensitive, "details")
        self.assertNotEqual(suppressed_labels(marginals), suppressed_labels(details))
        self.assertEqual(len(suppressed_labels(marginals)), 4)
        self.assertEqual(len(suppressed_labels(details)), 2)
        # Both contracts still prove A-X non-unique with two witnesses each.
        check_assessment(self, marginals)
        check_assessment(self, details)


class MultiSensitiveTests(unittest.TestCase):
    ROWS_3X3 = [
        ("D1", 2026, "X", 1), ("D1", 2026, "Y", 2), ("D1", 2026, "Z", 3),
        ("D2", 2026, "X", 4), ("D2", 2026, "Y", 0), ("D2", 2026, "Z", 2),
        ("D3", 2026, "X", 3), ("D3", 2026, "Y", 5), ("D3", 2026, "Z", 1),
    ]

    def sensitive(self):
        return [SensitiveCell(2026, "D1", "X"), SensitiveCell(2026, "D2", "Y")]

    def test_details_contract_two_sensitive_cells(self):
        m = matrix(self.ROWS_3X3)
        decision = adjudicate_matrix(m, self.sensitive(), "details")
        self.assertEqual(decision["status"], "publishable")
        # One extra suppression per sensitive row; D3 stays fully visible.
        self.assertEqual(decision["search"]["minimum_extra_suppressed"], 2)
        self.assertEqual(decision["search"]["equivalent_schemes"], 4)
        self.assertEqual(
            suppressed_labels(decision),
            {("D1", "X"), ("D1", "Y"), ("D2", "X"), ("D2", "Y")},
        )
        ranges = {a["cell"]["account"]: (a["minimum"], a["maximum"]) for a in decision["assessments"]}
        self.assertEqual(ranges, {"X": (0, 3), "Y": (0, 4)})
        check_assessment(self, decision)

    def test_marginals_contract_two_sensitive_cells(self):
        m = matrix(self.ROWS_3X3)
        decision = adjudicate_matrix(m, self.sensitive(), "marginals")
        self.assertEqual(decision["status"], "publishable")
        check_assessment(self, decision)
        suppressed = suppressed_labels(decision)
        self.assertIn(("D1", "X"), suppressed)
        self.assertIn(("D2", "Y"), suppressed)
        # Cross-check global minimality independently: no smaller set is safe.
        extra = decision["search"]["minimum_extra_suppressed"]
        candidates = [
            (d, a)
            for d in ("D1", "D2", "D3")
            for a in ("X", "Y", "Z")
            if (d, a) not in {("D1", "X"), ("D2", "Y")}
        ]
        sensitive_labels = [("D1", "X"), ("D2", "Y")]
        for size in range(extra):
            for combo in itertools.combinations(candidates, size):
                free = set(combo) | set(sensitive_labels)
                self.assertFalse(
                    suppression_is_safe(m, "marginals", sensitive_labels, free),
                    f"smaller set {free} would already be safe",
                )
        self.assertTrue(suppression_is_safe(m, "marginals", sensitive_labels, suppressed))


class BoundaryCollapseTests(unittest.TestCase):
    def test_zero_column_total_refuses_marginals_but_not_details(self):
        # Column X total is 0: non-negativity pins A-X to 0 under marginals
        # even with every detail suppressed, although the cycle rank is 1.
        rows = [("A", 2025, "X", 0), ("A", 2025, "Y", 10), ("B", 2025, "X", 0), ("B", 2025, "Y", 5)]
        m = matrix(rows)
        sensitive = [SensitiveCell(2025, "A", "X")]
        marginals = adjudicate_matrix(m, sensitive, "marginals")
        self.assertEqual(marginals["status"], "refused")
        self.assertNotIn("publication", marginals)
        self.assertNotIn("assessments", marginals)
        conflict = marginals["conflict"]
        self.assertEqual(conflict["cell"]["department"], "A")
        self.assertEqual(
            conflict["constraints"], [{"type": "col_total", "account": "X", "value": 0}]
        )
        # The same matrix is publishable under the details contract.
        details = adjudicate_matrix(m, sensitive, "details")
        self.assertEqual(details["status"], "publishable")
        self.assertEqual(suppressed_labels(details), {("A", "X"), ("A", "Y")})
        check_assessment(self, details)

    def test_zero_lower_bound_tightens_feasible_range(self):
        # B-X=3 leaves only 5 units of cycle slack: A-X is capped at 5, not 10.
        rows = [("A", 2025, "X", 2), ("A", 2025, "Y", 8), ("B", 2025, "X", 3), ("B", 2025, "Y", 7)]
        m = matrix(rows)
        decision = adjudicate_matrix(m, [SensitiveCell(2025, "A", "X")], "marginals")
        self.assertEqual(decision["status"], "publishable")
        assessment = decision["assessments"][0]
        self.assertEqual((assessment["minimum"], assessment["maximum"]), (0, 5))
        check_assessment(self, decision)

    def test_range_collapse_forces_global_resolve_not_template(self):
        # Adding a zero column to the 2x2 template changes the marginals
        # outcome from publishable to refused: no template reuse is possible.
        rows = [("A", 2025, "X", 0), ("A", 2025, "Y", 10), ("B", 2025, "X", 0), ("B", 2025, "Y", 10)]
        m = matrix(rows)
        decision = adjudicate_matrix(m, [SensitiveCell(2025, "A", "X")], "marginals")
        self.assertEqual(decision["status"], "refused")


class GlobalMinimumTests(unittest.TestCase):
    def test_global_minimum_beats_local_greedy(self):
        # Two sensitive cells in row A; the unique minimum scheme suppresses
        # two cells in row B (freeing both columns) and nothing in row A.
        rows = [
            ("A", 2025, "X", 4), ("A", 2025, "Y", 5), ("A", 2025, "Z", 3),
            ("B", 2025, "X", 1), ("B", 2025, "Y", 2), ("B", 2025, "Z", 3),
        ]
        m = matrix(rows)
        sensitive = [SensitiveCell(2025, "A", "X"), SensitiveCell(2025, "A", "Y")]
        decision = adjudicate_matrix(m, sensitive, "marginals")
        self.assertEqual(decision["status"], "publishable")
        self.assertEqual(decision["search"]["minimum_extra_suppressed"], 2)
        self.assertEqual(decision["search"]["equivalent_schemes"], 1)
        self.assertEqual(
            suppressed_labels(decision),
            {("A", "X"), ("A", "Y"), ("B", "X"), ("B", "Y")},
        )
        ranges = {a["cell"]["account"]: (a["minimum"], a["maximum"]) for a in decision["assessments"]}
        self.assertEqual(ranges, {"X": (2, 5), "Y": (4, 7)})
        check_assessment(self, decision)

    def test_tie_breaking_is_stable_business_key_order(self):
        m = matrix(self.ROWS_TIE)
        decision = adjudicate_matrix(m, [SensitiveCell(2025, "A", "X")], "details")
        # Both A-row companions work; the lexicographically first key wins.
        self.assertEqual(decision["search"]["equivalent_schemes"], 2)
        self.assertEqual(suppressed_labels(decision), {("A", "X"), ("A", "Y")})
        self.assertIn("stable", decision["search"]["reason"])

    ROWS_TIE = [("A", 2025, "X", 1), ("A", 2025, "Y", 1), ("A", 2025, "Z", 1)]


class InputValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def write_csv(self, rows, name="budget_lines.csv", header=HEADER):
        path = self.root / name
        text = csv_text(rows, header)
        path.write_text(text, encoding="utf-8")
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return path, len(text.encode("utf-8")), digest

    def write_manifest(self, sensitive, declared=None):
        manifest = {"schema_version": 1, "sensitive_cells": sensitive}
        if declared is not None:
            manifest["declared_totals"] = declared
        path = self.root / "sensitive.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        return path

    def adjudicate_rows(self, rows, sensitive, declared=None):
        path, size, digest = self.write_csv(rows)
        manifest = self.write_manifest(sensitive, declared)
        return adjudicate(path, size, digest, manifest)

    def test_duplicate_key_rejected(self):
        rows = [("A", 2025, "X", 1), ("A", 2025, "X", 2)]
        with self.assertRaises(BudgetError) as ctx:
            parse_budget_csv(csv_text(rows))
        self.assertEqual(ctx.exception.code, "duplicate_key")

    def test_missing_amount_rejected(self):
        text = HEADER + "\nA,2025,X,\n"
        with self.assertRaises(BudgetError) as ctx:
            parse_budget_csv(text)
        self.assertEqual(ctx.exception.code, "missing_amount")

    def test_negative_amount_rejected(self):
        with self.assertRaises(BudgetError) as ctx:
            parse_budget_csv(csv_text([("A", 2025, "X", -5)]))
        self.assertEqual(ctx.exception.code, "negative_amount")

    def test_non_integer_amount_rejected(self):
        with self.assertRaises(BudgetError) as ctx:
            parse_budget_csv(csv_text([("A", 2025, "X", "1.5")]))
        self.assertEqual(ctx.exception.code, "invalid_amount")

    def test_missing_column_rejected(self):
        with self.assertRaises(BudgetError) as ctx:
            parse_budget_csv("department,fiscal_year,account\nA,2025,X\n")
        self.assertEqual(ctx.exception.code, "missing_column")

    def test_declared_totals_mismatch_rejected(self):
        declared = {"2025": {"rows": {"A": 999, "B": 10}, "cols": {"X": 10, "Y": 10}}}
        with self.assertRaises(BudgetError) as ctx:
            self.adjudicate_rows(
                ROWS_2X2, [{"fiscal_year": 2025, "department": "A", "account": "X"}], declared
            )
        self.assertEqual(ctx.exception.code, "total_mismatch")

    def test_declared_totals_accepted_when_consistent(self):
        declared = {"2025": {"rows": {"A": 10, "B": 10}, "cols": {"X": 10, "Y": 10}}}
        report = self.adjudicate_rows(
            ROWS_2X2, [{"fiscal_year": 2025, "department": "A", "account": "X"}], declared
        )
        self.assertEqual(decision_for(report, 2025, "marginals")["status"], "publishable")

    def test_source_byte_tamper_rejected(self):
        path, size, digest = self.write_csv(ROWS_2X2)
        manifest = self.write_manifest([{"fiscal_year": 2025, "department": "A", "account": "X"}])
        report = adjudicate(path, size, digest, manifest)
        self.assertEqual(report["source"]["sha256"], digest)
        # Tamper with the verified file on disk: re-verification must fail.
        path.write_text(csv_text(ROWS_2X2).replace("A,2025,X,2", "A,2025,X,3"), encoding="utf-8")
        with self.assertRaises(BudgetError) as ctx:
            adjudicate(path, size, digest, manifest)
        self.assertEqual(ctx.exception.code, "source_reverify_failed")

    def test_unknown_sensitive_cell_rejected(self):
        with self.assertRaises(BudgetError) as ctx:
            self.adjudicate_rows(ROWS_2X2, [{"fiscal_year": 2025, "department": "A", "account": "ZZ"}])
        self.assertEqual(ctx.exception.code, "unknown_sensitive_cell")

    def test_manifest_schema_rejected(self):
        path, size, digest = self.write_csv(ROWS_2X2)
        manifest = self.root / "bad.json"
        manifest.write_text(json.dumps({"schema_version": 2, "sensitive_cells": []}), "utf-8")
        with self.assertRaises(BudgetError) as ctx:
            adjudicate(path, size, digest, manifest)
        self.assertEqual(ctx.exception.code, "manifest_invalid")


class MultiYearTests(unittest.TestCase):
    def test_fiscal_years_are_adjudicated_independently(self):
        rows = [
            ("A", 2024, "X", 1), ("A", 2024, "Y", 1), ("B", 2024, "X", 1), ("B", 2024, "Y", 1),
        ] + ROWS_2X2
        m = parse_budget_csv(csv_text(rows))
        self.assertEqual([year.fiscal_year for year in m], [2024, 2025])
        self.assertEqual(m[0].row_totals(), [2, 2])
        self.assertEqual(m[1].row_totals(), [10, 10])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "budget_lines.csv"
            text = csv_text(rows)
            path.write_text(text, encoding="utf-8")
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            manifest = root / "sensitive.json"
            manifest.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "sensitive_cells": [
                            {"fiscal_year": 2025, "department": "A", "account": "X"}
                        ],
                    }
                ),
                encoding="utf-8",
            )
            report = adjudicate(path, len(text.encode("utf-8")), digest, manifest)
        # 2024 has no sensitive cells: fully publishable, nothing suppressed.
        year_2024 = decision_for(report, 2024, "marginals")
        self.assertEqual(year_2024["status"], "publishable")
        self.assertEqual(year_2024["suppressed"], [])
        self.assertEqual(year_2024["assessments"], [])
        self.assertEqual(len(year_2024["publication"]["cells"]), 4)
        # 2025 keeps its own adjudication; years never mix.
        year_2025 = decision_for(report, 2025, "marginals")
        self.assertEqual(len(year_2025["suppressed"]), 4)
        self.assertEqual(len(report["decisions"]), 4)


class PipelineIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        config_path = write_project(self.root, free_port())
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["budget"] = {
            "adjudications": [
                {
                    "dataset_id": "civic-budget",
                    "version_id": "2025.1",
                    "filename": "budget_lines.csv",
                    "manifest": "config/budget_sensitive.json",
                }
            ]
        }
        config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
        manifest = {
            "schema_version": 1,
            "sensitive_cells": [{"fiscal_year": 2025, "department": "Parks", "account": "5100"}],
            "declared_totals": {
                "2025": {
                    "rows": {"Parks": 184200, "Library": 96750, "Roads": 241000},
                    "cols": {"5100": 184200, "5200": 96750, "5300": 241000},
                }
            },
        }
        (self.root / "config" / "budget_sensitive.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
        self.config = load_config(config_path)
        self.portal = portal_for(self.config).start()

    def tearDown(self):
        self.portal.stop()
        self.temp.cleanup()

    def test_pipeline_adjudicates_without_changing_verified_status(self):
        outcome = run_pipeline(self.config, reset=True)
        self.assertEqual(outcome.summary["verified"], 8)
        connection = connect(self.config.download.db_path)
        try:
            row = connection.execute(
                "SELECT status FROM artifacts WHERE filename = 'budget_lines.csv'"
            ).fetchone()
            self.assertEqual(row[0], "verified")
            events = [
                json.loads(item["detail_json"])
                for item in connection.execute(
                    "SELECT detail_json FROM events WHERE code LIKE 'budget.%' ORDER BY seq"
                )
            ]
        finally:
            connection.close()
        self.assertEqual(len(events), 2)
        self.assertEqual({event["contract"] for event in events}, {"marginals", "details"})
        self.assertTrue(all(event["status"] == "publishable" for event in events))
        adjudication_path = self.config.download.report_dir / (
            "budget/civic-budget/2025.1/budget_lines.csv.adjudication.json"
        )
        self.assertTrue(adjudication_path.is_file())
        report = json.loads(adjudication_path.read_text(encoding="utf-8"))
        marginals = decision_for(report, 2025, "marginals")
        self.assertEqual(
            suppressed_labels(marginals),
            {("Parks", "5100"), ("Parks", "5200"), ("Library", "5100"), ("Library", "5200")},
        )
        self.assertEqual(marginals["search"]["equivalent_schemes"], 2)
        assessment = marginals["assessments"][0]
        self.assertEqual((assessment["minimum"], assessment["maximum"]), (87450, 184200))
        check_assessment(self, marginals)
        details = decision_for(report, 2025, "details")
        self.assertEqual(suppressed_labels(details), {("Parks", "5100"), ("Parks", "5200")})
        check_assessment(self, details)
        # The main report surfaces the adjudication section.
        main_report = json.loads(
            (self.config.download.report_dir / "report.json").read_text(encoding="utf-8")
        )
        self.assertEqual(len(main_report["budget_adjudications"]), 2)
        markdown = (self.config.download.report_dir / "report.md").read_text(encoding="utf-8")
        self.assertIn("Budget disclosure adjudication", markdown)


if __name__ == "__main__":
    unittest.main()

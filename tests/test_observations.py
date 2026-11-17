"""Offline dual-format observation mirror adjudication tests."""

from __future__ import annotations

import json
import socket
import tempfile
import unittest
from pathlib import Path

from helpers import free_port, portal_for, write_project
from releaseharbor.config import load_config
from releaseharbor.db import connect
from releaseharbor.observations import (
    CONTRACT_AUDIT,
    CONTRACT_SUMMARY,
    STATUS_FAIL,
    STATUS_PASS,
    STATUS_UNKNOWN,
    MAX_GROUP_ROWS,
    PrecisionRule,
    adjudicate,
    load_artifact_source,
    parse_csv,
    parse_jsonl,
)
from releaseharbor.pipeline import run_pipeline
from releaseharbor.reports import build_observation_contracts
from releaseharbor.util import sha256_bytes

CSV_HEADER = "site_id,observed_on,pm25,pm10\n"
JSON_MEDIA = "application/x-ndjson"


def _write(directory: Path, name: str, text: str) -> Path:
    path = directory / name
    path.write_bytes(text.encode("utf-8"))
    return path


def source(kind: str, text: str, directory: Path | None = None):
    """Build a verified ArtifactSource from in-memory text."""

    holder = directory or Path(tempfile.mkdtemp())
    name = "samples.csv" if kind == "csv" else "samples.jsonl"
    media = "text/csv" if kind == "csv" else JSON_MEDIA
    path = _write(holder, name, text)
    data = text.encode("utf-8")
    return load_artifact_source(
        kind=kind,
        filename=name,
        media_type=media,
        status="verified",
        expected_size=len(data),
        expected_sha256=sha256_bytes(data),
        path=path,
    )


def contracts(csv_text: str, json_text: str, precision: PrecisionRule | None = None):
    return adjudicate(
        source("csv", csv_text),
        source("jsonl", json_text),
        precision=precision or PrecisionRule(),
    )


class CrossPairingTests(unittest.TestCase):
    """The stated counterexample: identical projections, no whole-row mirror."""

    CSV = CSV_HEADER + "S,2025-01-01,8,20\nS,2025-01-01,12,24\n"
    JSON = (
        '{"site_id":"S","observed_on":"2025-01-01","pm25":8,"pm10":24}\n'
        '{"site_id":"S","observed_on":"2025-01-01","pm25":12,"pm10":20}\n'
    )

    def test_audit_fails_while_daily_summary_passes(self):
        result = contracts(self.CSV, self.JSON)
        audit = result[CONTRACT_AUDIT]
        summary = result[CONTRACT_SUMMARY]
        self.assertEqual(audit["status"], STATUS_FAIL)
        self.assertEqual(summary["status"], STATUS_PASS)
        group = audit["groups"][0]
        self.assertEqual(group["audit_reasons"], ["no_global_row_bijection"])
        self.assertEqual(group["pairing"], [])
        # The aggregate contract sees identical totals; only the whole-row
        # bijection distinguishes the two contracts.
        aggregate = summary["groups"][0]
        self.assertEqual(aggregate["totals"]["csv"], aggregate["totals"]["json"])
        self.assertEqual(aggregate["csv_count"], aggregate["json_count"])
        self.assertNotIn("pairing", aggregate)
        self.assertTrue(group["unmatched_csv_lines"])
        self.assertTrue(group["unmatched_json_lines"])
        self.assertIn("MUST NOT be used for row-level audit", summary["notice"])
        self.assertEqual(audit["witness"]["kind"], "minimal_removal")
        self.assertEqual(summary["witness"]["kind"], "aggregate_totals")
        self.assertEqual(
            audit["witness"]["unmatchable"][0]["partial_pairing"], []
        )
        aggregate = summary["witness"]["groups"][0]
        self.assertEqual(aggregate["csv_totals"], aggregate["json_totals"])
        self.assertNotIn("pairing", aggregate)
        self.assertEqual(aggregate["count"], 2)

    def test_both_contracts_share_digest_precision_and_sources(self):
        result = contracts(self.CSV, self.JSON)
        audit = result[CONTRACT_AUDIT]
        summary = result[CONTRACT_SUMMARY]
        self.assertEqual(audit["source_digest"], summary["source_digest"])
        self.assertEqual(audit["precision"], summary["precision"])
        self.assertEqual(audit["sources"], summary["sources"])
        self.assertEqual(
            [item["status"] for item in audit["sources"]],
            ["verified", "verified"],
        )

    def test_shuffled_jsonl_rows_still_mirror(self):
        shuffled = (
            '{"site_id":"S","observed_on":"2025-01-01","pm25":12,"pm10":24}\n'
            '{"site_id":"S","observed_on":"2025-01-01","pm25":8,"pm10":20}\n'
        )
        result = contracts(self.CSV, shuffled)
        self.assertEqual(result[CONTRACT_AUDIT]["status"], STATUS_PASS)
        self.assertEqual(result[CONTRACT_SUMMARY]["status"], STATUS_PASS)
        pairing = result[CONTRACT_AUDIT]["groups"][0]["pairing"]
        self.assertEqual(
            sorted((item["csv_line"], item["json_line"]) for item in pairing),
            [(2, 2), (3, 1)],
        )
        self.assertEqual(result[CONTRACT_AUDIT]["witness"]["kind"], "global_row_bijection")
        self.assertEqual(
            result[CONTRACT_AUDIT]["witness"]["groups"][0]["pairs"], 2
        )

    def test_pairing_is_independent_of_physical_order(self):
        reversed_csv = CSV_HEADER + "S,2025-01-01,12,24\nS,2025-01-01,8,20\n"
        result = contracts(reversed_csv, self.JSON)
        self.assertEqual(result[CONTRACT_AUDIT]["status"], STATUS_FAIL)
        swapped = (
            '{"site_id":"S","observed_on":"2025-01-01","pm25":12,"pm10":20}\n'
            '{"site_id":"S","observed_on":"2025-01-01","pm25":8,"pm10":24}\n'
        )
        self.assertEqual(contracts(self.CSV, swapped)[CONTRACT_AUDIT]["status"], STATUS_FAIL)


class PrecisionChainTests(unittest.TestCase):
    """Greedy file-order pairing exhausts the last row; a global match exists."""

    CSV = (
        CSV_HEADER
        + "S,2025-01-01,8,20\n"
        + "S,2025-01-01,8,21\n"
        + "S,2025-01-01,8,30\n"
    )
    JSON = (
        '{"site_id":"S","observed_on":"2025-01-01","pm25":8,"pm10":20.5}\n'
        '{"site_id":"S","observed_on":"2025-01-01","pm25":8,"pm10":19.5}\n'
        '{"site_id":"S","observed_on":"2025-01-01","pm25":8,"pm10":29.5}\n'
    )

    def _greedy_by_file_order(self):
        csv_rows = parse_csv(self.CSV).rows
        json_rows = parse_jsonl(self.JSON).rows
        from releaseharbor.observations import _compatible

        used: set[int] = set()
        for row in csv_rows:
            for index, candidate in enumerate(json_rows):
                if index in used:
                    continue
                if _compatible(row, candidate, PrecisionRule(), {}, {}):
                    used.add(index)
                    break
        return len(used)

    def test_greedy_fails_but_global_matching_succeeds(self):
        self.assertEqual(self._greedy_by_file_order(), 2)
        result = contracts(self.CSV, self.JSON)
        self.assertEqual(result[CONTRACT_AUDIT]["status"], STATUS_PASS)
        pairing = result[CONTRACT_AUDIT]["groups"][0]["pairing"]
        self.assertEqual(len(pairing), 3)
        self.assertEqual(
            sorted((item["csv_line"], item["json_line"]) for item in pairing),
            [(2, 2), (3, 1), (4, 3)],
        )

    def test_half_unit_interval_edges_are_inclusive(self):
        # CSV 8 (places 0) -> [7.5, 8.5]; JSON 7.5 (places 1) -> [7.45, 7.55].
        csv_text = CSV_HEADER + "S,2025-01-01,8,20\n"
        json_text = '{"site_id":"S","observed_on":"2025-01-01","pm25":7.5,"pm10":20}\n'
        self.assertEqual(contracts(csv_text, json_text)[CONTRACT_AUDIT]["status"], STATUS_PASS)
        json_far = '{"site_id":"S","observed_on":"2025-01-01","pm25":7.4,"pm10":20}\n'
        self.assertEqual(
            contracts(csv_text, json_far)[CONTRACT_AUDIT]["status"], STATUS_FAIL
        )
        # Touching endpoints count as overlap: 8 -> [7.5, 8.5], 8.5 -> [8.45, 8.55].
        json_edge = '{"site_id":"S","observed_on":"2025-01-01","pm25":8.5,"pm10":20}\n'
        self.assertEqual(contracts(csv_text, json_edge)[CONTRACT_AUDIT]["status"], STATUS_PASS)

    def test_declared_precision_changes_intervals(self):
        csv_text = "# precision: pm25=1, pm10=1\n" + CSV_HEADER + "S,2025-01-01,8,20\n"
        json_text = '{"site_id":"S","observed_on":"2025-01-01","pm25":8.04,"pm10":20}\n'
        loose = contracts(csv_text, json_text)
        self.assertEqual(loose[CONTRACT_AUDIT]["status"], STATUS_PASS)
        strict = adjudicate(
            source("csv", csv_text),
            source("jsonl", json_text),
            precision=PrecisionRule(pm25=2, pm10=2),
        )
        self.assertEqual(strict[CONTRACT_AUDIT]["status"], STATUS_FAIL)
        self.assertNotEqual(
            loose[CONTRACT_AUDIT]["precision"]["fingerprint"],
            strict[CONTRACT_AUDIT]["precision"]["fingerprint"],
        )

    def test_jsonl_precision_directive_is_honoured(self):
        json_text = (
            '{"_precision":{"pm25":2,"pm10":2}}\n'
            '{"site_id":"S","observed_on":"2025-01-01","pm25":8,"pm10":20}\n'
        )
        csv_text = CSV_HEADER + "S,2025-01-01,8.03,20\n"
        # JSON places 2 -> [7.995, 8.005]; CSV places 2 -> [8.025, 8.035].
        self.assertEqual(contracts(csv_text, json_text)[CONTRACT_AUDIT]["status"], STATUS_FAIL)
        # With no declaration, 8.03 vs 8 would overlap as [7.5, 8.5].
        default_csv = CSV_HEADER + "S,2025-01-01,8.03,20\n"
        default_json = '{"site_id":"S","observed_on":"2025-01-01","pm25":8,"pm10":20}\n'
        self.assertEqual(
            contracts(default_csv, default_json)[CONTRACT_AUDIT]["status"], STATUS_PASS
        )


class PrecisionRecalculationTests(unittest.TestCase):
    def test_same_bytes_different_rule_cannot_reuse_a_verdict(self):
        csv_text = CSV_HEADER + "S,2025-01-01,8,20\n"
        json_text = '{"site_id":"S","observed_on":"2025-01-01","pm25":8.03,"pm10":20}\n'
        loose = contracts(csv_text, json_text)
        tight = adjudicate(
            source("csv", csv_text),
            source("jsonl", json_text),
            precision=PrecisionRule(pm25=2, pm10=2),
        )
        self.assertEqual(loose[CONTRACT_AUDIT]["status"], STATUS_PASS)
        self.assertEqual(tight[CONTRACT_AUDIT]["status"], STATUS_FAIL)
        self.assertEqual(
            loose[CONTRACT_AUDIT]["source_digest"],
            tight[CONTRACT_AUDIT]["source_digest"],
        )
        self.assertNotEqual(
            loose[CONTRACT_AUDIT]["precision"]["fingerprint"],
            tight[CONTRACT_AUDIT]["precision"]["fingerprint"],
        )


class UnmatchableWitnessTests(unittest.TestCase):
    def test_minimal_line_set_is_reported_with_a_real_partial_witness(self):
        csv_text = CSV_HEADER + "S,2025-01-01,8,20\nS,2025-01-01,12,24\nS,2025-01-01,30,40\n"
        json_text = (
            '{"site_id":"S","observed_on":"2025-01-01","pm25":8,"pm10":20}\n'
            '{"site_id":"S","observed_on":"2025-01-01","pm25":12,"pm10":24}\n'
            '{"site_id":"S","observed_on":"2025-01-01","pm25":32,"pm10":40}\n'
        )
        result = contracts(csv_text, json_text)[CONTRACT_AUDIT]
        self.assertEqual(result["status"], STATUS_FAIL)
        group = result["groups"][0]
        self.assertEqual(group["witness_kind"], "minimal_removal")
        # One line per side must go: the maximum matching has size 2 of 3.
        self.assertEqual(group["unmatched_csv_lines"], [4])
        self.assertEqual(group["unmatched_json_lines"], [3])
        self.assertEqual(group["witness_sources"], {"csv": [4], "jsonl": [3]})
        # The reported remainder really does pair without reuse.
        remaining_csv = set(group["csv_lines"]) - set(group["unmatched_csv_lines"])
        remaining_json = set(group["json_lines"]) - set(group["unmatched_json_lines"])
        self.assertEqual(
            {item["csv_line"] for item in group["pairing"]},
            remaining_csv,
        )
        self.assertEqual(
            {item["json_line"] for item in group["pairing"]},
            remaining_json,
        )


class MatchingAlgorithmTests(unittest.TestCase):
    """The matcher must return a true maximum matching on arbitrary graphs."""

    def test_iterative_matching_equals_brute_force(self):
        import itertools
        import random

        from releaseharbor.observations import _match_graph

        def brute_force_size(adjacency):
            right_count = max(
                (right for row in adjacency for right in row), default=-1
            ) + 1
            best = 0
            for assignment in itertools.product(
                *[list(row) + [None] for row in adjacency]
            ):
                used = [right for right in assignment if right is not None]
                if len(used) == len(set(used)):
                    best = max(best, len(used))
            return best

        random.seed(11)
        for _ in range(250):
            left = random.randint(1, 5)
            right = random.randint(1, 5)
            adjacency = [
                [index for index in range(right) if random.random() < 0.5]
                for _ in range(left)
            ]
            result = _match_graph(adjacency)
            matched = sum(1 for value in result if value >= 0)
            self.assertEqual(matched, brute_force_size(adjacency), adjacency)
            seen = set()
            for index, value in enumerate(result):
                if value < 0:
                    continue
                self.assertIn(value, adjacency[index])
                self.assertNotIn(value, seen)
                seen.add(value)


class CountMismatchTests(unittest.TestCase):
    def test_extra_csv_row_fails_audit_with_that_row_as_witness(self):
        csv_text = CSV_HEADER + "S,2025-01-01,8,20\nS,2025-01-01,12,24\nS,2025-01-01,50,60\n"
        json_text = (
            '{"site_id":"S","observed_on":"2025-01-01","pm25":8,"pm10":20}\n'
            '{"site_id":"S","observed_on":"2025-01-01","pm25":12,"pm10":24}\n'
        )
        result = contracts(csv_text, json_text)
        self.assertEqual(result[CONTRACT_AUDIT]["status"], STATUS_FAIL)
        self.assertEqual(result[CONTRACT_SUMMARY]["status"], STATUS_FAIL)
        group = result[CONTRACT_AUDIT]["groups"][0]
        self.assertEqual(group["audit_reasons"], ["observation_count_mismatch"])
        self.assertEqual(group["unmatched_csv_lines"], [4])
        self.assertEqual(group["unmatched_json_lines"], [])
        self.assertEqual(len(group["pairing"]), 2)

    def test_extra_json_row_fails_audit_symmetrically(self):
        csv_text = CSV_HEADER + "S,2025-01-01,8,20\nS,2025-01-01,12,24\n"
        json_text = (
            '{"site_id":"S","observed_on":"2025-01-01","pm25":8,"pm10":20}\n'
            '{"site_id":"S","observed_on":"2025-01-01","pm25":12,"pm10":24}\n'
            '{"site_id":"S","observed_on":"2025-01-01","pm25":50,"pm10":60}\n'
        )
        result = contracts(csv_text, json_text)
        self.assertEqual(result[CONTRACT_AUDIT]["status"], STATUS_FAIL)
        self.assertEqual(result[CONTRACT_SUMMARY]["status"], STATUS_FAIL)
        group = result[CONTRACT_AUDIT]["groups"][0]
        self.assertEqual(group["unmatched_json_lines"], [3])
        self.assertEqual(group["unmatched_csv_lines"], [])


class WitnessDeterminismTests(unittest.TestCase):
    CSV_ROWS = [(8, 20), (8, 21), (8, 22), (8, 30)]
    JSON_VALUES = ["19.6", "20.4", "21.4", "29.6"]

    def _content_witness(self, csv_order, json_order):
        csv_text = CSV_HEADER + "".join(
            f"S,2025-01-01,{self.CSV_ROWS[i][0]},{self.CSV_ROWS[i][1]}\n"
            for i in csv_order
        )
        json_text = "".join(
            '{"site_id":"S","observed_on":"2025-01-01","pm25":8,"pm10":'
            + self.JSON_VALUES[i]
            + "}\n"
            for i in json_order
        )
        group = contracts(csv_text, json_text)[CONTRACT_AUDIT]["groups"][0]
        csv_rows = {
            row.line: (str(row.values["pm25"]), str(row.values["pm10"]))
            for row in parse_csv(csv_text).rows
        }
        json_rows = {
            row.line: (str(row.values["pm25"]), str(row.values["pm10"]))
            for row in parse_jsonl(json_text).rows
        }
        return (
            tuple(sorted(csv_rows[line] for line in group["unmatched_csv_lines"])),
            tuple(sorted(json_rows[line] for line in group["unmatched_json_lines"])),
            tuple(
                sorted(
                    (csv_rows[item["csv_line"]], json_rows[item["json_line"]])
                    for item in group["pairing"]
                )
            ),
            group["minimum_removal_size"],
        )

    def test_witness_is_invariant_under_physical_reordering(self):
        import itertools

        outcomes = {
            self._content_witness(csv_order, json_order)
            for json_order in itertools.permutations(range(4))
            for csv_order in itertools.permutations(range(4))
        }
        self.assertEqual(len(outcomes), 1)
        unmatched_csv, unmatched_json, pairing, size = outcomes.pop()
        self.assertEqual(unmatched_csv, (("8", "22"),))
        self.assertEqual(unmatched_json, (("8", "19.6"),))
        self.assertEqual(size, 2)
        self.assertEqual(len(pairing), 3)

    def test_witness_is_a_genuine_bijection_of_the_remaining_rows(self):
        group = contracts(
            CSV_HEADER + "S,2025-01-01,8,20\nS,2025-01-01,12,24\nS,2025-01-01,30,40\n",
            '{"site_id":"S","observed_on":"2025-01-01","pm25":8,"pm10":20}\n'
            '{"site_id":"S","observed_on":"2025-01-01","pm25":12,"pm10":24}\n'
            '{"site_id":"S","observed_on":"2025-01-01","pm25":32,"pm10":40}\n',
        )[CONTRACT_AUDIT]["groups"][0]
        remaining_csv = set(group["csv_lines"]) - set(group["unmatched_csv_lines"])
        remaining_json = set(group["json_lines"]) - set(group["unmatched_json_lines"])
        paired_csv = [item["csv_line"] for item in group["pairing"]]
        paired_json = [item["json_line"] for item in group["pairing"]]
        self.assertEqual(len(paired_csv), len(set(paired_csv)))
        self.assertEqual(len(paired_json), len(set(paired_json)))
        self.assertEqual(set(paired_csv), remaining_csv)
        self.assertEqual(set(paired_json), remaining_json)
        self.assertEqual(
            group["minimum_removal_size"],
            len(group["unmatched_csv_lines"]) + len(group["unmatched_json_lines"]),
        )


class GroupKeyTests(unittest.TestCase):
    def test_non_unique_group_keys_are_matched_as_multisets(self):
        csv_text = CSV_HEADER + "S,2025-01-01,8,20\nS,2025-01-01,8,20\n"
        json_text = (
            '{"site_id":"S","observed_on":"2025-01-01","pm25":8,"pm10":20}\n'
            '{"site_id":"S","observed_on":"2025-01-01","pm25":8,"pm10":20}\n'
        )
        result = contracts(csv_text, json_text)
        group = result[CONTRACT_AUDIT]["groups"][0]
        self.assertEqual(result[CONTRACT_AUDIT]["status"], STATUS_PASS)
        self.assertEqual(len(group["pairing"]), 2)
        self.assertEqual(len(group["duplicate_rows"]), 2)
        self.assertNotEqual(
            group["pairing"][0]["csv_line"], group["pairing"][1]["csv_line"]
        )

    def test_duplicate_rows_on_one_side_alone_break_audit(self):
        csv_text = CSV_HEADER + "S,2025-01-01,8,20\nS,2025-01-01,8,20\n"
        json_text = '{"site_id":"S","observed_on":"2025-01-01","pm25":8,"pm10":20}\n'
        result = contracts(csv_text, json_text)
        self.assertEqual(result[CONTRACT_AUDIT]["status"], STATUS_FAIL)
        self.assertEqual(result[CONTRACT_SUMMARY]["status"], STATUS_FAIL)

    def test_distinct_sites_with_same_day_are_independent_groups(self):
        csv_text = CSV_HEADER + "A,2025-01-01,8,20\nB,2025-01-01,8,20\n"
        json_text = (
            '{"site_id":"A","observed_on":"2025-01-01","pm25":8,"pm10":20}\n'
            '{"site_id":"B","observed_on":"2025-01-01","pm25":8,"pm10":20}\n'
        )
        result = contracts(csv_text, json_text)
        self.assertEqual(result[CONTRACT_AUDIT]["status"], STATUS_PASS)
        self.assertEqual(len(result[CONTRACT_AUDIT]["groups"]), 2)


class InvalidValueTests(unittest.TestCase):
    def _unknown_codes(self, csv_text, json_text):
        result = contracts(csv_text, json_text)
        self.assertEqual(result[CONTRACT_AUDIT]["status"], STATUS_UNKNOWN)
        self.assertEqual(result[CONTRACT_SUMMARY]["status"], STATUS_UNKNOWN)
        return {issue["code"] for issue in result[CONTRACT_AUDIT]["issues"]}

    def test_nan_and_infinity_are_rejected(self):
        codes = self._unknown_codes(
            CSV_HEADER + "S,2025-01-01,8,20\n",
            '{"site_id":"S","observed_on":"2025-01-01","pm25":NaN,"pm10":20}\n',
        )
        self.assertIn("non_finite_number", codes)
        codes = self._unknown_codes(
            CSV_HEADER + "S,2025-01-01,8,20\n",
            '{"site_id":"S","observed_on":"2025-01-01","pm25":8,"pm10":Infinity}\n',
        )
        self.assertIn("non_finite_number", codes)

    def test_duplicate_json_key_is_rejected_with_line(self):
        result = contracts(
            CSV_HEADER + "S,2025-01-01,8,20\n",
            '{"site_id":"S","site_id":"S","observed_on":"2025-01-01","pm25":8,"pm10":20}\n',
        )
        self.assertEqual(result[CONTRACT_AUDIT]["status"], STATUS_UNKNOWN)
        issue = result[CONTRACT_AUDIT]["issues"][0]
        self.assertEqual(issue["code"], "duplicate_json_key")
        self.assertEqual(issue["line"], 1)
        self.assertEqual(issue["source"], "jsonl")

    def test_missing_field_is_reported_with_source_line(self):
        result = contracts(
            CSV_HEADER + "S,2025-01-01,,20\n",
            '{"site_id":"S","observed_on":"2025-01-01","pm25":8,"pm10":20}\n',
        )
        self.assertEqual(result[CONTRACT_AUDIT]["status"], STATUS_UNKNOWN)
        issue = result[CONTRACT_AUDIT]["issues"][0]
        self.assertEqual((issue["source"], issue["line"], issue["code"]), ("csv", 2, "missing_field"))

    def test_unit_mismatch_is_rejected(self):
        codes = self._unknown_codes(
            CSV_HEADER + "S,2025-01-01,8,20,mg/m3\n",
            '{"site_id":"S","observed_on":"2025-01-01","pm25":8,"pm10":20}\n',
        )
        self.assertIn("row_length", codes)  # extra cell has no header
        codes = self._unknown_codes(
            "site_id,observed_on,pm25,pm10,unit\nS,2025-01-01,8,20,mg/m3\n",
            '{"site_id":"S","observed_on":"2025-01-01","pm25":8,"pm10":20}\n',
        )
        self.assertIn("unit_mismatch", codes)

    def test_missing_date_is_rejected_as_missing_field(self):
        result = contracts(
            CSV_HEADER + "S,,8,20\n",
            '{"site_id":"S","observed_on":"2025-01-01","pm25":8,"pm10":20}\n',
        )
        issue = result[CONTRACT_AUDIT]["issues"][0]
        self.assertEqual((issue["source"], issue["line"], issue["code"]), ("csv", 2, "missing_field"))
        self.assertEqual(issue["detail"]["field"], "observed_on")

    def test_invalid_date_is_rejected(self):
        codes = self._unknown_codes(
            CSV_HEADER + "S,2025-13-40,8,20\n",
            '{"site_id":"S","observed_on":"2025-01-01","pm25":8,"pm10":20}\n',
        )
        self.assertIn("invalid_date", codes)

    def test_pm25_greater_than_pm10_is_rejected(self):
        result = contracts(
            CSV_HEADER + "S,2025-01-01,25,20\n",
            '{"site_id":"S","observed_on":"2025-01-01","pm25":8,"pm10":20}\n',
        )
        self.assertEqual(result[CONTRACT_AUDIT]["status"], STATUS_UNKNOWN)
        self.assertEqual(result[CONTRACT_AUDIT]["issues"][0]["code"], "pm_order")

    def test_issue_codes_do_not_depend_on_input_order(self):
        first = contracts(
            CSV_HEADER + "S,2025-01-01,25,20\n",
            '{"site_id":"S","observed_on":"2025-01-01","pm25":8,"pm10":20}\n',
        )
        second = contracts(
            CSV_HEADER + "S,2025-01-01,8,20\n",
            '{"site_id":"S","observed_on":"2025-01-01","pm25":25,"pm10":20}\n',
        )
        self.assertEqual(
            first[CONTRACT_AUDIT]["issues"][0]["code"],
            second[CONTRACT_AUDIT]["issues"][0]["code"],
        )
        self.assertEqual(first[CONTRACT_AUDIT]["issues"][0]["source"], "csv")
        self.assertEqual(second[CONTRACT_AUDIT]["issues"][0]["source"], "jsonl")


class BoundedWorkTests(unittest.TestCase):
    def test_oversized_group_is_unknown_for_both_contracts(self):
        limit = MAX_GROUP_ROWS
        csv_text = CSV_HEADER + "".join(
            f"S,2025-01-01,{index % 10},{index % 10 + 5}\n"
            for index in range(limit + 1)
        )
        json_text = "".join(
            '{"site_id":"S","observed_on":"2025-01-01","pm25":'
            f'{index % 10},"pm10":{index % 10 + 5}}}\n'
            for index in range(limit + 1)
        )
        result = contracts(csv_text, json_text)
        for name in (CONTRACT_AUDIT, CONTRACT_SUMMARY):
            self.assertEqual(result[name]["status"], STATUS_UNKNOWN)
            self.assertEqual(result[name]["issues"][0]["code"], "group_too_large")


class IntegrityGateTests(unittest.TestCase):
    def test_tampered_csv_makes_both_contracts_unknown(self):
        directory = Path(tempfile.mkdtemp())
        csv_path = _write(directory, "samples.csv", CSV_HEADER + "S,2025-01-01,8,20\n")
        data = csv_path.read_bytes()
        json_source = source(
            "jsonl",
            '{"site_id":"S","observed_on":"2025-01-01","pm25":8,"pm10":20}\n',
        )
        csv_source = load_artifact_source(
            kind="csv",
            filename="samples.csv",
            media_type="text/csv",
            status="verified",
            expected_size=len(data),
            expected_sha256=sha256_bytes(data),
            path=csv_path,
        )
        csv_path.write_bytes((CSV_HEADER + "S,2025-01-01,9,20\n").encode("utf-8"))
        tampered = load_artifact_source(
            kind="csv",
            filename="samples.csv",
            media_type="text/csv",
            status="verified",
            expected_size=len(data),
            expected_sha256=sha256_bytes(data),
            path=csv_path,
        )
        self.assertTrue(csv_source.issues == [])
        self.assertEqual(tampered.issues[0].code, "artifact_tampered")
        result = adjudicate(tampered, json_source)
        for name in (CONTRACT_AUDIT, CONTRACT_SUMMARY):
            self.assertEqual(result[name]["status"], STATUS_UNKNOWN)
            self.assertIsNone(result[name]["witness"])
            self.assertEqual(result[name]["groups"], [])

    def test_missing_file_is_unknown_not_a_stale_verdict(self):
        csv_source = source("csv", CSV_HEADER + "S,2025-01-01,8,20\n")
        json_source = source(
            "jsonl", '{"site_id":"S","observed_on":"2025-01-01","pm25":8,"pm10":20}\n'
        )
        missing = load_artifact_source(
            kind="jsonl",
            filename="samples.jsonl",
            media_type=JSON_MEDIA,
            status="verified",
            expected_size=json_source.expected_size,
            expected_sha256=json_source.expected_sha256,
            path=Path(tempfile.mkdtemp()) / "samples.jsonl",
        )
        result = adjudicate(csv_source, missing)
        self.assertEqual(result[CONTRACT_AUDIT]["status"], STATUS_UNKNOWN)
        self.assertEqual(result[CONTRACT_SUMMARY]["status"], STATUS_UNKNOWN)
        self.assertEqual(result[CONTRACT_SUMMARY]["issues"][0]["code"], "artifact_missing")

    def test_unaccepted_artifact_is_unknown(self):
        csv_source = source("csv", CSV_HEADER + "S,2025-01-01,8,20\n")
        json_source = source(
            "jsonl",
            '{"site_id":"S","observed_on":"2025-01-01","pm25":8,"pm10":20}\n',
        )
        rejected = load_artifact_source(
            kind="jsonl",
            filename="samples.jsonl",
            media_type=JSON_MEDIA,
            status="checksum_mismatch",
            expected_size=json_source.expected_size,
            expected_sha256=json_source.expected_sha256,
            path=None,
        )
        result = adjudicate(csv_source, rejected)
        self.assertEqual(result[CONTRACT_AUDIT]["status"], STATUS_UNKNOWN)
        self.assertEqual(
            result[CONTRACT_AUDIT]["issues"][0]["code"], "artifact_not_accepted"
        )


class PipelineIntegrationTests(unittest.TestCase):
    """The demo's real pipeline output: quarantined CSV means unknown, not pass."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = load_config(write_project(Path(self.temp.name), free_port()))
        self.portal = None

    def tearDown(self):
        if self.portal is not None:
            self.portal.stop()
        self.temp.cleanup()

    def _start_portal(self):
        self.portal = portal_for(self.config).start()
        return self.portal

    def _contracts(self, run_id):
        return build_observation_contracts(
            connect(self.config.download.db_path), self.config, run_id
        )

    def _rewrite_air_quality(self, mutate):
        fixture_path = self.config.portal.fixture
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        for dataset in fixture["datasets"]:
            if dataset["id"] != "air-quality":
                continue
            for version in dataset["versions"]:
                for artifact in version["artifacts"]:
                    mutate(artifact)
        fixture_path.write_text(json.dumps(fixture), encoding="utf-8")

    def test_reports_are_written_and_quarantine_gives_unknown(self):
        self._start_portal()
        outcome = run_pipeline(self.config, reset=True)
        for key in (
            "observation_audit_json",
            "observation_audit_markdown",
            "daily_summary_json",
            "daily_summary_markdown",
        ):
            self.assertTrue((self.config.root / outcome.reports[key]).is_file())
        report = self._contracts(outcome.run_id)
        self.assertEqual(report["scope"], ["air-quality"])
        version = report["versions"][0]
        for name in (CONTRACT_AUDIT, CONTRACT_SUMMARY):
            self.assertEqual(version["contracts"][name]["status"], STATUS_UNKNOWN)
            codes = [issue["code"] for issue in version["contracts"][name]["issues"]]
            self.assertEqual(codes, ["artifact_not_accepted"])

    def test_verified_pair_is_adjudicated_independently_of_quarantine(self):
        def fix(artifact):
            if artifact["name"] == "samples.csv":
                content = artifact["content"].encode("utf-8")
                artifact["sha256"] = sha256_bytes(content)

        self._rewrite_air_quality(fix)
        self._start_portal()
        outcome = run_pipeline(self.config, reset=True)
        self.assertEqual(outcome.summary["quarantined"], 0)
        report = self._contracts(outcome.run_id)
        version = report["versions"][0]
        self.assertEqual(version["contracts"][CONTRACT_AUDIT]["status"], STATUS_PASS)
        self.assertEqual(version["contracts"][CONTRACT_SUMMARY]["status"], STATUS_PASS)
        audit_groups = version["contracts"][CONTRACT_AUDIT]["groups"]
        self.assertEqual(len(audit_groups), 3)
        pairings = [item for group in audit_groups for item in group["pairing"]]
        self.assertEqual(len(pairings), 3)
        self.assertEqual(len({item["csv_line"] for item in pairings}), 3)
        summary_groups = version["contracts"][CONTRACT_SUMMARY]["groups"]
        self.assertEqual(
            [group["totals"]["csv"] for group in summary_groups],
            [group["totals"]["json"] for group in summary_groups],
        )

    def test_cross_pairing_through_real_pipeline(self):
        csv_text = (
            "site_id,observed_on,pm25,pm10\n"
            "S,2025-01-01,8,20\nS,2025-01-01,12,24\n"
        )
        json_text = (
            '{"site_id":"S","observed_on":"2025-01-01","pm25":8,"pm10":24}\n'
            '{"site_id":"S","observed_on":"2025-01-01","pm25":12,"pm10":20}\n'
        )

        def swap(artifact):
            if artifact["name"] == "samples.csv":
                artifact["content"] = csv_text
                artifact["sha256"] = sha256_bytes(csv_text.encode("utf-8"))
                artifact["declared_size"] = len(csv_text.encode("utf-8"))
            elif artifact["name"] == "samples.jsonl":
                artifact["content"] = json_text
                artifact["sha256"] = sha256_bytes(json_text.encode("utf-8"))
                artifact["declared_size"] = len(json_text.encode("utf-8"))

        self._rewrite_air_quality(swap)
        self._start_portal()
        outcome = run_pipeline(self.config, reset=True)
        self.assertEqual(outcome.summary["quarantined"], 0)
        report = self._contracts(outcome.run_id)
        version = report["versions"][0]
        self.assertEqual(version["contracts"][CONTRACT_AUDIT]["status"], STATUS_FAIL)
        self.assertEqual(version["contracts"][CONTRACT_SUMMARY]["status"], STATUS_PASS)

    def test_missing_counterpart_format_is_unknown_not_absent(self):
        fixture_path = self.config.portal.fixture
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        for dataset in fixture["datasets"]:
            if dataset["id"] != "air-quality":
                continue
            for version in dataset["versions"]:
                version["artifacts"] = [
                    artifact
                    for artifact in version["artifacts"]
                    if artifact["name"] != "samples.jsonl"
                ]
        fixture_path.write_text(json.dumps(fixture), encoding="utf-8")
        self._start_portal()
        outcome = run_pipeline(self.config, reset=True)
        report = self._contracts(outcome.run_id)
        self.assertEqual(len(report["versions"]), 1)
        version = report["versions"][0]
        for name in (CONTRACT_AUDIT, CONTRACT_SUMMARY):
            self.assertEqual(version["contracts"][name]["status"], STATUS_UNKNOWN)
            codes = [issue["code"] for issue in version["contracts"][name]["issues"]]
            self.assertIn("artifact_missing", codes)

    def test_version_without_observation_media_is_out_of_scope(self):
        fixture_path = self.config.portal.fixture
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        for dataset in fixture["datasets"]:
            if dataset["id"] != "air-quality":
                continue
            for version in dataset["versions"]:
                version["artifacts"] = [
                    artifact
                    for artifact in version["artifacts"]
                    if artifact["name"] == "samples-readme.md"
                ]
        fixture_path.write_text(json.dumps(fixture), encoding="utf-8")
        self._start_portal()
        outcome = run_pipeline(self.config, reset=True)
        report = self._contracts(outcome.run_id)
        self.assertEqual(report["versions"], [])

    def test_non_ndjson_datasets_are_out_of_scope(self):
        self._start_portal()
        outcome = run_pipeline(self.config, reset=True)
        report = self._contracts(outcome.run_id)
        self.assertEqual(report["scope"], ["air-quality"])
        self.assertEqual(
            [(item["dataset_id"], item["version_id"]) for item in report["versions"]],
            [("air-quality", "2025-01")],
        )

    def test_observation_reports_do_not_change_intake_summary(self):
        self._start_portal()
        outcome = run_pipeline(self.config, reset=True)
        self.assertEqual(outcome.summary["artifacts"], 9)
        self.assertEqual(outcome.summary["quarantined"], 1)
        self.assertEqual(outcome.status, "partial")
        # The new reports are additive only.
        self.assertIn("report_json", outcome.reports)
        self.assertIn("observation_audit_json", outcome.reports)

    def test_disk_tamper_after_acceptance_is_unknown(self):
        def fix(artifact):
            if artifact["name"] == "samples.csv":
                content = artifact["content"].encode("utf-8")
                artifact["sha256"] = sha256_bytes(content)

        self._rewrite_air_quality(fix)
        self._start_portal()
        outcome = run_pipeline(self.config, reset=True)
        target = self.config.download.artifact_dir / "air-quality/2025-01/samples.jsonl"
        original = target.read_bytes()
        target.write_bytes(original.replace(b'"pm25":11', b'"pm25":99'))
        report = self._contracts(outcome.run_id)
        for name in (CONTRACT_AUDIT, CONTRACT_SUMMARY):
            self.assertEqual(
                report["versions"][0]["contracts"][name]["status"], STATUS_UNKNOWN
            )
            codes = [
                issue["code"]
                for issue in report["versions"][0]["contracts"][name]["issues"]
            ]
            self.assertIn("artifact_tampered", codes)


if __name__ == "__main__":
    unittest.main()

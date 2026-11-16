"""Synthetic tests for declarative CSV schemas and cross-version compatibility.

Every portal fixture used here is generated inside the test's own temporary
directory. The fixtures exercise:
  * both v2 shapes (front-inserted nullable note; decimal->text relaxation),
  * quoted commas/newlines, duplicate headers/primary keys, invalid values,
  * byte-accepted but schema-invalid content, tampering, and reruns,
  * the two independent consumer contracts, and the read-only console.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
import urllib.request
from pathlib import Path

from helpers import free_port, write_project
from releaseharbor.config import load_config
from releaseharbor.console import ConsoleServer
from releaseharbor.csvschema import (
    NAME_BOUND_CONTRACT,
    POSITIONAL_CONTRACT,
    SchemaColumn,
    SchemaDeclaration,
    adjudicate_compatibility,
    validate_csv,
)
from releaseharbor.db import connect
from releaseharbor.discovery import Discovery
from releaseharbor.errors import FixtureError
from releaseharbor.fixture import load_portal_fixture
from releaseharbor.httpclient import SafeHttpClient
from releaseharbor.portal_server import PortalServer
from releaseharbor.pipeline import run_pipeline
from releaseharbor.security import UrlPolicy

DATASET = "ledger-exports"
FILENAME = "entries.csv"


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


V1_SCHEMA = {
    "schema_version": "v1",
    "change_reason": "initial publication",
    "columns": [
        {"name": "id", "type": "integer", "nullable": False},
        {"name": "amount", "type": "decimal", "nullable": False},
    ],
    "primary_key": ["id"],
}

# v2 variant A: a nullable text column inserted *before* the existing columns.
V2_NOTE_SCHEMA = {
    "schema_version": "v2",
    "predecessor": "v1",
    "change_reason": "add optional note column before the existing fields",
    "columns": [
        {"name": "note", "type": "text", "nullable": True},
        {"name": "id", "type": "integer", "nullable": False},
        {"name": "amount", "type": "decimal", "nullable": False},
    ],
    "primary_key": ["id"],
}

# v2 variant B: amount widens decimal -> text (numeric sample values on purpose:
# the ruling must still be based on the declaration, never on sample content).
V2_TEXT_SCHEMA = {
    "schema_version": "v2",
    "predecessor": "v1",
    "change_reason": "amount retyped to free text",
    "columns": [
        {"name": "id", "type": "integer", "nullable": False},
        {"name": "amount", "type": "text", "nullable": False},
    ],
    "primary_key": ["id"],
}

V1_CONTENT = "id,amount\n1,184.20\n2,96.75\n3,241.00\n"
V2_NOTE_CONTENT = (
    'note,id,amount\n"first, quarterly",1,184.20\n'
    '"multi\nline note",2,96.75\n,3,241.00\n'
)
V2_TEXT_CONTENT = "id,amount\n1,184.20\n2,96.75\n3,241.00\n"


def csv_artifact(name: str, content: str, schema: dict | None) -> dict:
    artifact = {
        "name": name,
        "media_type": "text/csv",
        "content": content,
        "sha256": sha256_text(content),
    }
    if schema is not None:
        artifact["schema"] = schema
    return artifact


def version(version_id: str, artifacts: list[dict], *, channel: str = "stable") -> dict:
    return {
        "id": version_id,
        "label": f"Release {version_id}",
        "channel": channel,
        "released_at": "2025-04-02",
        "notes": "Synthetic schema evolution release.",
        "artifacts": artifacts,
    }


def build_fixture(versions: list[dict], *, extra_datasets: list[dict] | None = None) -> dict:
    datasets = [
        {
            "id": DATASET,
            "title": "Ledger Export Evolution",
            "summary": "Synthetic CSV schema lineage for compatibility rulings.",
            "publisher": "Aurora Municipal Data Office",
            "license": "CC-BY-4.0 (synthetic fixture)",
            "versions": versions,
        }
    ]
    datasets.extend(extra_datasets or [])
    return {
        "schema_version": 1,
        "portal_title": "Schema Evolution Sandbox",
        "portal_description": "Synthetic portal for declarative CSV schema tests.",
        "publisher": "Aurora Fixture Data Guild",
        "datasets": datasets,
    }


def lineage_fixture(v2_schema: dict, v2_content: str) -> dict:
    return build_fixture(
        [
            version("v1", [csv_artifact(FILENAME, V1_CONTENT, V1_SCHEMA)]),
            version("v2", [csv_artifact(FILENAME, v2_content, v2_schema)]),
        ]
    )


class SchemaHarness:
    """Builds a project + portal from an in-memory portal fixture dict."""

    def __init__(self, fixture_data: dict):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        config_path = write_project(root, free_port())
        (root / "fixtures" / "portal.json").write_text(
            json.dumps(fixture_data), encoding="utf-8"
        )
        self.config = load_config(config_path)
        fixture = load_portal_fixture(self.config.portal.fixture)
        self.portal = PortalServer(
            fixture, "127.0.0.1", int(self.config.portal.base_url.rsplit(":", 1)[1])
        ).start()

    def stop(self) -> None:
        self.portal.stop()
        self.temp.cleanup()

    def artifact_path(self, version_id: str, filename: str = FILENAME) -> Path:
        return (
            self.config.download.artifact_dir / DATASET / version_id / filename
        )


class CsvSchemaCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.harness = SchemaHarness(lineage_fixture(V2_NOTE_SCHEMA, V2_NOTE_CONTENT))

    def tearDown(self):
        self.harness.stop()

    def _evaluations(self, config):
        connection = connect(config.download.db_path)
        try:
            rows = {
                row["version_id"]: dict(row)
                for row in connection.execute(
                    "SELECT * FROM csv_schema_evaluations ORDER BY version_id"
                ).fetchall()
            }
            return rows, connection
        except BaseException:
            connection.close()
            raise

    def test_front_inserted_note_is_positional_break_but_name_bound_compatible(self):
        config = self.harness.config
        outcome = run_pipeline(config, reset=True)
        self.assertEqual(outcome.status, "success")
        rows, connection = self._evaluations(config)
        try:
            v2 = rows["v2"]
            self.assertTrue(bool(v2["validation_ok"]))
            self.assertEqual(bool(v2["positional_compatible"]), False)
            self.assertEqual(bool(v2["name_bound_compatible"]), True)
            contracts = {item["contract"]: item for item in json.loads(v2["contracts_json"])}
            self.assertIn("name changed", contracts[POSITIONAL_CONTRACT]["reasons"][0])
            self.assertTrue(contracts[NAME_BOUND_CONTRACT]["compatible"])
            v1 = rows["v1"]
            self.assertTrue(bool(v1["validation_ok"]))
            self.assertTrue(bool(v1["positional_compatible"]))
            self.assertTrue(bool(v1["name_bound_compatible"]))
        finally:
            connection.close()

    def test_report_distinguishes_transfer_complete_from_consumer_readability(self):
        config = self.harness.config
        outcome = run_pipeline(config, reset=True)
        report = json.loads(
            (config.download.report_dir / "report.json").read_text(encoding="utf-8")
        )
        artifacts = {
            (version["id"], artifact["filename"]): artifact
            for dataset in report["datasets"]
            for version in dataset["versions"]
            for artifact in version["artifacts"]
        }
        v1_artifact = artifacts[("v1", FILENAME)]
        v2_artifact = artifacts[("v2", FILENAME)]
        # Byte acceptance and schema-level readability are separate fields.
        self.assertEqual(v2_artifact["status"], "verified")
        self.assertTrue(v2_artifact["schema"]["validation_ok"])
        self.assertIs(v2_artifact["schema"]["positional_compatible"], False)
        self.assertIs(v2_artifact["schema"]["name_bound_compatible"], True)
        # Before/after declaration summaries and change reason are retained.
        self.assertEqual(
            v2_artifact["schema"]["declaration"]["schema_version"], "v2"
        )
        self.assertEqual(
            v2_artifact["schema"]["predecessor"]["schema_version"], "v1"
        )
        self.assertIn("note", v2_artifact["schema"]["change_reason"])
        # The accepted content digest recorded with the verdict is byte evidence.
        self.assertEqual(
            v2_artifact["schema"]["accepted_content"]["sha256"],
            sha256_text(V2_NOTE_CONTENT),
        )
        self.assertEqual(report["summary"]["csv_schemas"]["evaluated"], 2)
        self.assertEqual(report["summary"]["csv_schemas"]["schema_valid"], 2)
        markdown = (config.download.report_dir / "report.md").read_text(encoding="utf-8")
        self.assertIn("positional", markdown)
        self.assertIn("NOT compatible", markdown)

    def test_decimal_to_text_breaks_both_contracts_even_with_numeric_samples(self):
        self.harness.stop()
        self.harness = SchemaHarness(lineage_fixture(V2_TEXT_SCHEMA, V2_TEXT_CONTENT))
        config = self.harness.config
        run_pipeline(config, reset=True)
        rows, connection = self._evaluations(config)
        try:
            v2 = rows["v2"]
            self.assertTrue(bool(v2["validation_ok"]))
            self.assertEqual(bool(v2["positional_compatible"]), False)
            self.assertEqual(bool(v2["name_bound_compatible"]), False)
            contracts = {item["contract"]: item for item in json.loads(v2["contracts_json"])}
            name_reasons = " ".join(contracts[NAME_BOUND_CONTRACT]["reasons"])
            self.assertIn("type changed", name_reasons)
            self.assertIn("decimal", name_reasons)
            self.assertIn("text", name_reasons)
        finally:
            connection.close()

    def test_quoted_commas_and_embedded_newlines_parse_as_single_fields(self):
        config = self.harness.config
        run_pipeline(config, reset=True)
        rows, connection = self._evaluations(config)
        try:
            validation = json.loads(rows["v2"]["validation_json"])
            self.assertTrue(rows["v2"]["validation_ok"])
            self.assertEqual(validation["record_count"], 3)
            self.assertEqual(
                validation["header"], ["note", "id", "amount"]
            )
        finally:
            connection.close()
        on_disk = self.harness.artifact_path("v2").read_text(encoding="utf-8")
        self.assertIn('"first, quarterly"', on_disk)
        self.assertIn('"multi\nline note"', on_disk)


class CsvSchemaFailureTests(unittest.TestCase):
    def _run(self, fixture_data: dict):
        self.harness = SchemaHarness(fixture_data)
        config = self.harness.config
        run_pipeline(config, reset=True)
        connection = connect(config.download.db_path)
        try:
            row = connection.execute(
                "SELECT * FROM csv_schema_evaluations WHERE version_id = 'v2'"
            ).fetchone()
            artifact = connection.execute(
                "SELECT status FROM artifacts WHERE version_id = 'v2'"
            ).fetchone()
            return config, dict(row), dict(artifact), connection
        except BaseException:
            connection.close()
            raise

    def tearDown(self):
        if hasattr(self, "harness"):
            self.harness.stop()

    def test_duplicate_header_is_rejected_but_byte_status_stays_verified(self):
        bad = "id,id\n1,2\n"
        fixture = build_fixture(
            [
                version("v1", [csv_artifact(FILENAME, V1_CONTENT, V1_SCHEMA)]),
                version(
                    "v2",
                    [csv_artifact(FILENAME, bad, V1_SCHEMA)],
                ),
            ]
        )
        config, evaluation, artifact, connection = self._run(fixture)
        try:
            self.assertEqual(artifact["status"], "verified")
            self.assertFalse(bool(evaluation["validation_ok"]))
            self.assertIsNone(evaluation["positional_compatible"])
            self.assertIsNone(evaluation["name_bound_compatible"])
            codes = [item["code"] for item in json.loads(evaluation["validation_json"])["errors"]]
            self.assertIn("duplicate_header", codes)
        finally:
            connection.close()

    def test_duplicate_primary_key_is_rejected_with_deterministic_location(self):
        bad = "id,amount\n1,10.00\n1,20.00\n"
        fixture = build_fixture(
            [version("v1", [csv_artifact(FILENAME, bad, V1_SCHEMA)])]
        )
        self.harness = SchemaHarness(fixture)
        config = self.harness.config
        run_pipeline(config, reset=True)
        connection = connect(config.download.db_path)
        try:
            row = dict(
                connection.execute(
                    "SELECT * FROM csv_schema_evaluations WHERE version_id = 'v1'"
                ).fetchone()
            )
            errors = json.loads(row["validation_json"])["errors"]
            self.assertEqual(errors[0]["code"], "primary_key_duplicate")
            self.assertEqual(errors[0]["line"], 3)
        finally:
            connection.close()

    def test_primary_key_null_and_invalid_values_are_rejected(self):
        bad = "id,amount\n,10.00\n2,not-a-decimal\n"
        fixture = build_fixture(
            [version("v1", [csv_artifact(FILENAME, bad, V1_SCHEMA)])]
        )
        self.harness = SchemaHarness(fixture)
        config = self.harness.config
        run_pipeline(config, reset=True)
        connection = connect(config.download.db_path)
        try:
            row = dict(
                connection.execute(
                    "SELECT * FROM csv_schema_evaluations WHERE version_id = 'v1'"
                ).fetchone()
            )
            codes = [item["code"] for item in json.loads(row["validation_json"])["errors"]]
            self.assertIn("primary_key_null", codes)
            self.assertIn("invalid_value", codes)
        finally:
            connection.close()

    def test_wrong_column_count_and_invalid_date_are_rejected(self):
        date_schema = {
            "schema_version": "v1",
            "columns": [
                {"name": "id", "type": "integer", "nullable": False},
                {"name": "observed_on", "type": "date", "nullable": False},
            ],
            "primary_key": ["id"],
        }
        bad = "id,observed_on\n1,2025-02-30\n2,2025-01-15,extra\n"
        fixture = build_fixture(
            [version("v1", [csv_artifact(FILENAME, bad, date_schema)])]
        )
        self.harness = SchemaHarness(fixture)
        config = self.harness.config
        run_pipeline(config, reset=True)
        connection = connect(config.download.db_path)
        try:
            row = dict(
                connection.execute(
                    "SELECT * FROM csv_schema_evaluations WHERE version_id = 'v1'"
                ).fetchone()
            )
            errors = json.loads(row["validation_json"])["errors"]
            codes = [item["code"] for item in errors]
            self.assertIn("invalid_value", codes)
            self.assertIn("column_count", codes)
        finally:
            connection.close()

    def test_header_not_matching_declaration_is_rejected(self):
        wrong_header = "identifier,amount\n1,10.00\n"
        fixture = build_fixture(
            [version("v1", [csv_artifact(FILENAME, wrong_header, V1_SCHEMA)])]
        )
        self.harness = SchemaHarness(fixture)
        config = self.harness.config
        run_pipeline(config, reset=True)
        connection = connect(config.download.db_path)
        try:
            row = dict(
                connection.execute(
                    "SELECT * FROM csv_schema_evaluations WHERE version_id = 'v1'"
                ).fetchone()
            )
            codes = [item["code"] for item in json.loads(row["validation_json"])["errors"]]
            self.assertIn("header_mismatch", codes)
        finally:
            connection.close()

    def test_unterminated_quote_is_a_fatal_parse_failure_after_byte_acceptance(self):
        bad = 'id,amount\n1,"unterminated\n'
        fixture = build_fixture(
            [version("v1", [csv_artifact(FILENAME, bad, V1_SCHEMA)])]
        )
        self.harness = SchemaHarness(fixture)
        config = self.harness.config
        run_pipeline(config, reset=True)
        connection = connect(config.download.db_path)
        try:
            artifact = dict(
                connection.execute(
                    "SELECT status FROM artifacts WHERE version_id = 'v1'"
                ).fetchone()
            )
            row = dict(
                connection.execute(
                    "SELECT * FROM csv_schema_evaluations WHERE version_id = 'v1'"
                ).fetchone()
            )
            # Byte verification still succeeded; the schema verdict did not.
            self.assertEqual(artifact["status"], "verified")
            self.assertFalse(bool(row["validation_ok"]))
            self.assertIsNone(row["positional_compatible"])
            fatal = json.loads(row["validation_json"])["fatal"]
            self.assertEqual(fatal["code"], "unterminated_quote")
        finally:
            connection.close()

    def test_cross_version_predecessor_reference_error_blocks_compatibility(self):
        broken_schema = dict(V2_NOTE_SCHEMA)
        broken_schema["predecessor"] = "v0-never-published"
        fixture = build_fixture(
            [
                version("v1", [csv_artifact(FILENAME, V1_CONTENT, V1_SCHEMA)]),
                version("v2", [csv_artifact(FILENAME, V2_NOTE_CONTENT, broken_schema)]),
            ]
        )
        config, evaluation, artifact, connection = self._run(fixture)
        try:
            self.assertTrue(bool(evaluation["validation_ok"]))
            self.assertIn("not published", evaluation["reference_error"])
            self.assertIsNone(evaluation["positional_compatible"])
            self.assertIsNone(evaluation["name_bound_compatible"])
        finally:
            connection.close()

    def test_tampered_redownload_restores_evaluation_and_never_reuses_old_verdict(self):
        fixture = lineage_fixture(V2_NOTE_SCHEMA, V2_NOTE_CONTENT)
        self.harness = SchemaHarness(fixture)
        config = self.harness.config
        first = run_pipeline(config, reset=True)
        target = self.harness.artifact_path("v2")
        target.write_bytes(b"tampered local copy\n")
        second = run_pipeline(config)
        self.assertNotEqual(first.run_id, second.run_id)
        connection = connect(config.download.db_path)
        try:
            rows = connection.execute(
                "SELECT run_id, validation_ok, content_sha256 "
                "FROM csv_schema_evaluations WHERE version_id = 'v2' ORDER BY created_at"
            ).fetchall()
            self.assertEqual(len(rows), 2)
            # Each verdict is pinned to the byte-accepted content digest at its run.
            for row in rows:
                self.assertTrue(bool(row["validation_ok"]))
                self.assertEqual(row["content_sha256"], sha256_text(V2_NOTE_CONTENT))
            self.assertNotEqual(rows[0]["run_id"], rows[1]["run_id"])
        finally:
            connection.close()

    def test_rerun_is_idempotent_and_historical_verdicts_remain_reviewable(self):
        fixture = lineage_fixture(V2_NOTE_SCHEMA, V2_NOTE_CONTENT)
        self.harness = SchemaHarness(fixture)
        config = self.harness.config
        run_pipeline(config, reset=True)
        run_pipeline(config)
        connection = connect(config.download.db_path)
        try:
            total = connection.execute(
                "SELECT COUNT(*) FROM csv_schema_evaluations"
            ).fetchone()[0]
            # Two runs over the same two artifacts: one immutable verdict each.
            self.assertEqual(total, 4)
            declarations = connection.execute(
                "SELECT DISTINCT schema_version, fingerprint "
                "FROM csv_schema_declarations ORDER BY schema_version"
            ).fetchall()
            self.assertEqual([row["schema_version"] for row in declarations], ["v1", "v2"])
            # Old verdicts stay readable with their own before/after summaries.
            older = connection.execute(
                "SELECT predecessor_summary_json FROM csv_schema_evaluations "
                "WHERE version_id = 'v2' ORDER BY created_at LIMIT 1"
            ).fetchone()
            self.assertIn("v1", older["predecessor_summary_json"])
        finally:
            connection.close()

    def test_interrupted_run_leaves_no_false_compatibility_and_rerun_repairs_it(self):
        self.harness = SchemaHarness(lineage_fixture(V2_NOTE_SCHEMA, V2_NOTE_CONTENT))
        config = self.harness.config
        run_pipeline(config, reset=True)
        connection = connect(config.download.db_path)
        try:
            # Simulate a crash after byte acceptance but before the v2 verdict
            # was written. The artifact row must not claim schema compatibility.
            connection.execute(
                "DELETE FROM csv_schema_evaluations WHERE version_id = 'v2'"
            )
            connection.commit()
            orphan = connection.execute(
                "SELECT status FROM artifacts WHERE version_id = 'v2'"
            ).fetchone()["status"]
            self.assertEqual(orphan, "verified")
        finally:
            connection.close()
        # The rerun re-evaluates the accepted file and restores the verdict.
        run_pipeline(config)
        connection = connect(config.download.db_path)
        try:
            restored = dict(
                connection.execute(
                    "SELECT * FROM csv_schema_evaluations WHERE version_id = 'v2' "
                    "ORDER BY created_at DESC LIMIT 1"
                ).fetchone()
            )
            self.assertTrue(bool(restored["validation_ok"]))
            self.assertFalse(bool(restored["positional_compatible"]))
            self.assertTrue(bool(restored["name_bound_compatible"]))
        finally:
            connection.close()

    def test_evolving_the_portal_declaration_keeps_the_prior_verdict_intact(self):
        # First run only knows v1.
        self.harness = SchemaHarness(
            build_fixture([version("v1", [csv_artifact(FILENAME, V1_CONTENT, V1_SCHEMA)])])
        )
        config = self.harness.config
        run_pipeline(config, reset=True)
        # The portal now publishes v2 under the same project/fixture path.
        fixture_path = config.portal.fixture
        fixture_path.write_text(
            json.dumps(lineage_fixture(V2_NOTE_SCHEMA, V2_NOTE_CONTENT)),
            encoding="utf-8",
        )
        self.harness.portal.stop()
        fixture = load_portal_fixture(fixture_path)
        self.harness.portal = PortalServer(
            fixture, "127.0.0.1", int(config.portal.base_url.rsplit(":", 1)[1])
        ).start()
        run_pipeline(config)
        connection = connect(config.download.db_path)
        try:
            v2_rows = connection.execute(
                "SELECT * FROM csv_schema_evaluations WHERE version_id = 'v2'"
            ).fetchall()
            self.assertEqual(len(v2_rows), 1)
            v2 = dict(v2_rows[0])
            self.assertFalse(bool(v2["positional_compatible"]))
            self.assertTrue(bool(v2["name_bound_compatible"]))
            # The original v1 declaration and its verdict remain reviewable.
            v1_history = connection.execute(
                "SELECT COUNT(*) FROM csv_schema_declarations WHERE schema_version = 'v1'"
            ).fetchone()[0]
            self.assertEqual(v1_history, 1)
            v1_verdicts = connection.execute(
                "SELECT COUNT(*) FROM csv_schema_evaluations WHERE version_id = 'v1'"
            ).fetchone()[0]
            self.assertEqual(v1_verdicts, 2)
        finally:
            connection.close()


class NonCsvAndUndeclaredTests(unittest.TestCase):
    def tearDown(self):
        if hasattr(self, "harness"):
            self.harness.stop()

    def test_non_csv_artifacts_follow_the_original_byte_only_path(self):
        fixture = build_fixture(
            [
                version(
                    "v1",
                    [
                        csv_artifact(FILENAME, V1_CONTENT, V1_SCHEMA),
                        {
                            "name": "notes.txt",
                            "media_type": "text/plain",
                            "content": "Synthetic release notes.\n",
                            "sha256": sha256_text("Synthetic release notes.\n"),
                        },
                    ],
                )
            ]
        )
        self.harness = SchemaHarness(fixture)
        config = self.harness.config
        outcome = run_pipeline(config, reset=True)
        self.assertEqual(outcome.status, "success")
        report = json.loads(
            (config.download.report_dir / "report.json").read_text(encoding="utf-8")
        )
        by_name = {
            artifact["filename"]: artifact
            for dataset in report["datasets"]
            for version in dataset["versions"]
            for artifact in version["artifacts"]
        }
        self.assertIsNone(by_name["notes.txt"]["schema"])
        self.assertIsNotNone(by_name[FILENAME]["schema"])
        connection = connect(config.download.db_path)
        try:
            evaluated = connection.execute(
                "SELECT COUNT(*) FROM csv_schema_evaluations"
            ).fetchone()[0]
            self.assertEqual(evaluated, 1)
        finally:
            connection.close()

    def test_csv_without_declaration_is_byte_accepted_but_not_schema_evaluated(self):
        fixture = build_fixture(
            [version("v1", [csv_artifact(FILENAME, V1_CONTENT, None)])]
        )
        self.harness = SchemaHarness(fixture)
        config = self.harness.config
        outcome = run_pipeline(config, reset=True)
        self.assertEqual(outcome.status, "success")
        connection = connect(config.download.db_path)
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM csv_schema_evaluations"
                ).fetchone()[0],
                0,
            )
            status = connection.execute(
                "SELECT status FROM artifacts"
            ).fetchone()["status"]
            self.assertEqual(status, "verified")
        finally:
            connection.close()

    def test_schema_declaration_on_non_csv_artifact_rejects_fixture(self):
        bad = build_fixture(
            [
                version(
                    "v1",
                    [
                        {
                            "name": "notes.txt",
                            "media_type": "text/plain",
                            "content": "notes\n",
                            "sha256": sha256_text("notes\n"),
                            "schema": V1_SCHEMA,
                        }
                    ],
                )
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "portal.json"
            path.write_text(json.dumps(bad), encoding="utf-8")
            with self.assertRaises(FixtureError):
                load_portal_fixture(path)

    def test_console_shows_schema_panel_through_read_only_connection(self):
        self.harness = SchemaHarness(lineage_fixture(V2_NOTE_SCHEMA, V2_NOTE_CONTENT))
        config = self.harness.config
        run_pipeline(config, reset=True)
        server = ConsoleServer(config.download.db_path, 0).start()
        try:
            connection = connect(config.download.db_path)
            try:
                uid = connection.execute(
                    "SELECT artifact_uid FROM artifacts "
                    "WHERE version_id = 'v2' AND filename = ?",
                    (FILENAME,),
                ).fetchone()[0]
            finally:
                connection.close()
            with urllib.request.urlopen(
                server.base_url + "/artifact/" + uid, timeout=3
            ) as response:
                body = response.read().decode("utf-8")
            self.assertEqual(response.status, 200)
            self.assertIn("Declarative CSV schema evaluations", body)
            self.assertIn("positional", body)
            self.assertIn("name_bound", body)
            self.assertIn(sha256_text(V2_NOTE_CONTENT)[:16], body)
        finally:
            server.stop()


class CsvSchemaUnitTests(unittest.TestCase):
    def test_contracts_are_declaration_driven_and_never_value_driven(self):
        v1 = SchemaDeclaration.from_dict(V1_SCHEMA)
        note = SchemaDeclaration.from_dict(V2_NOTE_SCHEMA)
        text = SchemaDeclaration.from_dict(V2_TEXT_SCHEMA)
        note_verdicts = {v.contract: v.compatible for v in adjudicate_compatibility(v1, note)}
        self.assertEqual(
            note_verdicts,
            {POSITIONAL_CONTRACT: False, NAME_BOUND_CONTRACT: True},
        )
        text_verdicts = {v.contract: v.compatible for v in adjudicate_compatibility(v1, text)}
        self.assertEqual(
            text_verdicts,
            {POSITIONAL_CONTRACT: False, NAME_BOUND_CONTRACT: False},
        )

    def test_nullability_weakening_and_pk_change_break_name_bound_contract(self):
        v1 = SchemaDeclaration.from_dict(V1_SCHEMA)
        weakened = SchemaDeclaration.from_dict(
            {
                "schema_version": "v2",
                "predecessor": "v1",
                "columns": [
                    {"name": "id", "type": "integer", "nullable": False},
                    {"name": "amount", "type": "decimal", "nullable": True},
                ],
                "primary_key": ["id"],
            }
        )
        verdicts = {v.contract: v for v in adjudicate_compatibility(v1, weakened)}
        self.assertFalse(verdicts[NAME_BOUND_CONTRACT].compatible)
        self.assertIn("non-null", " ".join(verdicts[NAME_BOUND_CONTRACT].reasons))
        rekeyed = SchemaDeclaration.from_dict(
            {
                "schema_version": "v2",
                "predecessor": "v1",
                "columns": [
                    {"name": "id", "type": "integer", "nullable": False},
                    {"name": "amount", "type": "decimal", "nullable": False},
                ],
                "primary_key": ["id", "amount"],
            }
        )
        verdicts = {v.contract: v for v in adjudicate_compatibility(v1, rekeyed)}
        self.assertFalse(verdicts[NAME_BOUND_CONTRACT].compatible)
        self.assertFalse(verdicts[POSITIONAL_CONTRACT].compatible)

    def test_appended_nullable_column_is_compatible_for_both_contracts(self):
        v1 = SchemaDeclaration.from_dict(V1_SCHEMA)
        appended = SchemaDeclaration.from_dict(
            {
                "schema_version": "v2",
                "predecessor": "v1",
                "columns": [
                    {"name": "id", "type": "integer", "nullable": False},
                    {"name": "amount", "type": "decimal", "nullable": False},
                    {"name": "note", "type": "text", "nullable": True},
                ],
                "primary_key": ["id"],
            }
        )
        verdicts = {v.contract: v.compatible for v in adjudicate_compatibility(v1, appended)}
        self.assertEqual(
            verdicts, {POSITIONAL_CONTRACT: True, NAME_BOUND_CONTRACT: True}
        )

    def test_reordering_only_is_name_bound_compatible_but_positional_break(self):
        v1 = SchemaDeclaration.from_dict(V1_SCHEMA)
        reordered = SchemaDeclaration.from_dict(
            {
                "schema_version": "v2",
                "predecessor": "v1",
                "columns": [
                    {"name": "amount", "type": "decimal", "nullable": False},
                    {"name": "id", "type": "integer", "nullable": False},
                ],
                "primary_key": ["id"],
            }
        )
        verdicts = {v.contract: v.compatible for v in adjudicate_compatibility(v1, reordered)}
        self.assertEqual(
            verdicts, {POSITIONAL_CONTRACT: False, NAME_BOUND_CONTRACT: True}
        )

    def test_invalid_schema_declarations_are_rejected(self):
        with self.assertRaises(Exception):
            SchemaDeclaration.from_dict(
                {
                    "schema_version": "v1",
                    "columns": [
                        {"name": "id", "type": "boolean", "nullable": False},
                    ],
                    "primary_key": ["id"],
                }
            )
        with self.assertRaises(Exception):
            SchemaDeclaration.from_dict(
                {
                    "schema_version": "v1",
                    "columns": [
                        {"name": "id", "type": "integer", "nullable": True},
                    ],
                    "primary_key": ["id"],
                }
            )
        with self.assertRaises(Exception):
            SchemaDeclaration.from_dict(
                {
                    "schema_version": "v1",
                    "columns": [
                        {"name": "id", "type": "integer", "nullable": False},
                        {"name": "id", "type": "text", "nullable": True},
                    ],
                    "primary_key": ["id"],
                }
            )

    def test_first_error_locations_are_deterministic_and_bounded(self):
        lines = ["id,amount"]
        lines.extend(f"{index},bad-{index}" for index in range(1, 30))
        content = "\n".join(lines) + "\n"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "x.csv"
            path.write_text(content, encoding="utf-8")
            report = validate_csv(path, SchemaDeclaration.from_dict(V1_SCHEMA))
        self.assertFalse(report.ok)
        self.assertLessEqual(len(report.errors), 20)
        self.assertEqual(report.errors[0]["line"], 2)
        self.assertEqual(report.errors[0]["column"], 2)

    def test_invalid_utf8_and_escaped_quotes_and_crlf_are_handled(self):
        text_schema = SchemaDeclaration(
            "v",
            (
                SchemaColumn("id", "integer", False),
                SchemaColumn("note", "text", True),
            ),
            ("id",),
            None,
            None,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "x.csv"
            path.write_bytes(b"id,note\n1,\xff\xfe\n")
            report = validate_csv(path, text_schema)
            self.assertFalse(report.ok)
            self.assertEqual(report.fatal["code"], "invalid_encoding")

            # CRLF line endings and an escaped quote inside a quoted field.
            path.write_bytes(b'id,note\r\n1,"he said ""hi"", ok"\r\n2,simple\r\n')
            report = validate_csv(path, text_schema)
            self.assertTrue(report.ok, report.errors)
            self.assertEqual(report.record_count, 2)

    def test_oversized_field_is_a_bounded_fatal_error(self):
        from releaseharbor.csvschema import MAX_FIELD_BYTES

        text_schema = SchemaDeclaration(
            "v",
            (
                SchemaColumn("id", "integer", False),
                SchemaColumn("note", "text", True),
            ),
            ("id",),
            None,
            None,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "x.csv"
            path.write_bytes(
                b"id,note\n1," + b"x" * (MAX_FIELD_BYTES + 10) + b"\n"
            )
            report = validate_csv(path, text_schema)
            self.assertFalse(report.ok)
            self.assertEqual(report.fatal["code"], "field_too_large")

    def test_text_column_accepts_digit_only_values_without_type_inference(self):
        text_schema = SchemaDeclaration(
            "v",
            (
                SchemaColumn("id", "integer", False),
                SchemaColumn("amount", "text", False),
            ),
            ("id",),
            None,
            None,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "x.csv"
            path.write_bytes(b"id,amount\n1,12345\n")
            report = validate_csv(path, text_schema)
        self.assertTrue(report.ok, report.errors)


class SchemaDiscoveryTests(unittest.TestCase):
    def tearDown(self):
        if hasattr(self, "harness"):
            self.harness.stop()

    def test_declaration_is_carried_from_portal_html_into_artifact_ref(self):
        self.harness = SchemaHarness(lineage_fixture(V2_NOTE_SCHEMA, V2_NOTE_CONTENT))
        config = self.harness.config
        client = SafeHttpClient(UrlPolicy(config.portal.base_url))
        result = Discovery(client, config).discover()
        refs = {
            (version.version_id, ref.filename): ref
            for dataset in result.datasets
            for version in dataset.versions
            for ref in version.artifacts
        }
        v2_ref = refs[("v2", FILENAME)]
        self.assertEqual(v2_ref.media_type, "text/csv")
        self.assertIsNotNone(v2_ref.schema)
        self.assertEqual(v2_ref.schema.schema_version, "v2")
        self.assertEqual(v2_ref.schema.predecessor, "v1")
        self.assertEqual(
            v2_ref.schema.column_names, ("note", "id", "amount")
        )
        self.assertTrue(v2_ref.schema.column("note").nullable)
        self.assertFalse(v2_ref.schema.column("id").nullable)

    def test_non_csv_artifact_ref_has_no_schema(self):
        fixture = build_fixture(
            [
                version(
                    "v1",
                    [
                        csv_artifact(FILENAME, V1_CONTENT, V1_SCHEMA),
                        {
                            "name": "notes.txt",
                            "media_type": "text/plain",
                            "content": "notes\n",
                            "sha256": sha256_text("notes\n"),
                        },
                    ],
                )
            ]
        )
        self.harness = SchemaHarness(fixture)
        config = self.harness.config
        client = SafeHttpClient(UrlPolicy(config.portal.base_url))
        result = Discovery(client, config).discover()
        refs = [
            ref
            for dataset in result.datasets
            for version in dataset.versions
            for ref in version.artifacts
        ]
        by_name = {ref.filename: ref for ref in refs}
        self.assertIsNone(by_name["notes.txt"].schema)
        self.assertIsNotNone(by_name[FILENAME].schema)


if __name__ == "__main__":
    unittest.main()

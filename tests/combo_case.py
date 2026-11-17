import json
import tempfile
import unittest
from pathlib import Path

from helpers import free_port, portal_for, write_project
from releaseharbor.clock import Clock
from releaseharbor.combo_models import (
    CONTRACT_INTERNAL,
    CONTRACT_PUBLIC,
)
from releaseharbor.combo_rules import load_rule_table
from releaseharbor.combo_service import run_combo_query
from releaseharbor.config import load_config
from releaseharbor.db import connect
from releaseharbor.ledger import Ledger
from releaseharbor.pipeline import run_pipeline


class ComboFixtureCase(unittest.TestCase):
    """Builds a real accepted-artifact ledger from the synthetic portal."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = load_config(write_project(Path(self.temp.name), free_port()))
        self.portal = portal_for(self.config).start()
        outcome = run_pipeline(self.config, reset=True)
        self.assertEqual(outcome.summary["verified"], 8)
        self.connection = connect(self.config.download.db_path)
        self.clock = Clock.from_settings(self.config.clock_mode, self.config.clock_fixed)
        self.rule_table = load_rule_table(self.config.combo.rule_table)
        rows = self.connection.execute(
            "SELECT artifact_uid, expected_sha256, status FROM artifacts "
            "WHERE status IN ('verified','skipped') ORDER BY artifact_uid"
        ).fetchall()
        self.accepted = [dict(row) for row in rows]
        self.ledger = Ledger(self.connection, self.clock)

    def tearDown(self):
        self.connection.close()
        self.portal.stop()
        self.temp.cleanup()

    def query(self, contract: str, sources, *, version: str | None = None):
        request = {
            "contract_id": contract,
            "rule_table_version": version if version is not None
            else self.rule_table.version,
            "sources": sources,
        }
        return run_combo_query(self.config, self.connection, self.clock, request)

    def src(self, index: int, label: str) -> dict:
        row = self.accepted[index]
        return {
            "artifact_uid": row["artifact_uid"],
            "fictional_label": label,
            "expected_sha256": row["expected_sha256"],
        }

    def x(self, index: int = 0) -> dict:
        return self.src(index, "FICT-X-1.0")

    def y(self, index: int = 1) -> dict:
        return self.src(index, "FICT-Y-1.0")

    def z(self, index: int = 2) -> dict:
        return self.src(index, "FICT-Z-1.0")

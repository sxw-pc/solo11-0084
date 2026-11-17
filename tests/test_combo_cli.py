import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from helpers import free_port, portal_for, write_project
from releaseharbor.cli import main
from releaseharbor.config import load_config
from releaseharbor.db import connect
from releaseharbor.pipeline import run_pipeline


class ComboCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = load_config(write_project(Path(self.temp.name), free_port()))
        self.portal = portal_for(self.config).start()
        run_pipeline(self.config, reset=True)

    def tearDown(self):
        self.portal.stop()
        self.temp.cleanup()

    def _request(self, contract: str, label: str) -> Path:
        connection = connect(self.config.download.db_path)
        try:
            row = connection.execute(
                "SELECT artifact_uid, expected_sha256 FROM artifacts "
                "WHERE status = 'verified' ORDER BY artifact_uid LIMIT 1"
            ).fetchone()
        finally:
            connection.close()
        request = {
            "contract_id": contract,
            "rule_table_version": "combo-rules-2026.1",
            "sources": [
                {
                    "artifact_uid": row["artifact_uid"],
                    "fictional_label": label,
                    "expected_sha256": row["expected_sha256"],
                }
            ],
        }
        path = Path(self.temp.name) / "combo-request.json"
        path.write_text(json.dumps(request), encoding="utf-8")
        return path

    def test_combo_command_returns_zero_when_deliverable(self):
        request = self._request("public_redistribution", "FICT-W-1.0")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(["combo", "--config", str(self.config.source_path),
                         str(request)])
        self.assertEqual(code, 0)
        self.assertIn("status: deliverable", output.getvalue())
        self.assertIn("combo-rules-2026.1", output.getvalue())

    def test_combo_command_returns_one_when_undecidable(self):
        request = self._request("public_redistribution", "FICT-UNKNOWN-9.9")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(["combo", "--config", str(self.config.source_path),
                         str(request)])
        self.assertEqual(code, 1)
        self.assertIn("status: undecidable", output.getvalue())


if __name__ == "__main__":
    unittest.main()

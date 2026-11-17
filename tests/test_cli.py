import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from helpers import FIXTURE, free_port, portal_for, write_project
from releaseharbor.cli import main


class CliTests(unittest.TestCase):
    def test_demo_command_completes_and_strict_mode_reports_partial(self):
        with tempfile.TemporaryDirectory() as directory:
            config = write_project(Path(directory), free_port())
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(["demo", "--config", str(config)]), 0)
            self.assertIn("status: partial", output.getvalue())
            self.assertIn("verified=11", output.getvalue())
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    main(["demo", "--config", str(config), "--strict"]), 1
                )

    def test_serve_requires_an_existing_ledger(self):
        with tempfile.TemporaryDirectory() as directory:
            config = write_project(Path(directory), free_port())
            error = io.StringIO()
            with contextlib.redirect_stderr(error):
                self.assertEqual(main(["serve", "--config", str(config)]), 2)
            self.assertIn("does not exist", error.getvalue())

    def test_transit_prints_two_contracts_and_writes_standalone_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = write_project(root, free_port())
            (root / "fixtures").mkdir(parents=True, exist_ok=True)
            (root / "fixtures/portal.json").write_bytes(FIXTURE.read_bytes())
            from releaseharbor.config import load_config
            from releaseharbor.pipeline import run_pipeline

            config = load_config(config_path)
            portal = portal_for(config).start()
            try:
                run_pipeline(config, reset=True)
            finally:
                portal.stop()
            query = root / "query.json"
            query.write_text(
                json.dumps(
                    {
                        "dataset_id": "transit-counts",
                        "old_version": "v3",
                        "new_version": "v4",
                        "old_zone": "W1",
                        "new_zone": "N4",
                        "old_label_version": "v3-labels",
                        "new_label_version": "v4-labels",
                        "edge_tables": [
                            {"version": "flows-v3-v4", "filename": "flow_edges.csv"}
                        ],
                    }
                ),
                encoding="utf-8",
            )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(
                    main(["transit", "--config", str(config_path), "--query", str(query)]), 0
                )
            text = output.getvalue()
            self.assertIn("guaranteed_relocation: 0", text)
            self.assertIn("maximum_exposure: 18320", text)
            self.assertIn("transit_json: reports/transit/transit.json", text)
            written = json.loads(
                (root / "reports/transit/transit.json").read_text(encoding="utf-8")
            )
            self.assertEqual(len(written["contracts"]), 2)
            self.assertTrue((root / "reports/transit/transit.md").is_file())

    def test_transit_without_ledger_fails_cleanly(self):
        with tempfile.TemporaryDirectory() as directory:
            config = write_project(Path(directory), free_port())
            query = Path(directory) / "query.json"
            query.write_text("{}", encoding="utf-8")
            error = io.StringIO()
            with contextlib.redirect_stderr(error):
                self.assertEqual(
                    main(["transit", "--config", str(config), "--query", str(query)]), 2
                )
            self.assertIn("does not exist", error.getvalue())


if __name__ == "__main__":
    unittest.main()

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from helpers import ROOT
from releaseharbor.config import load_config
from releaseharbor.errors import SecurityBoundaryError
from releaseharbor.fixture import load_portal_fixture
from releaseharbor.pipeline import run_pipeline
from releaseharbor.portal_server import PortalServer


class GoldenTests(unittest.TestCase):
    def test_fresh_demo_report_matches_golden(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config").mkdir()
            (root / "fixtures").mkdir()
            shutil.copyfile(ROOT / "config/demo.json", root / "config/demo.json")
            shutil.copyfile(ROOT / "fixtures/portal.json", root / "fixtures/portal.json")
            config = load_config(root / "config/demo.json")
            fixture = load_portal_fixture(config.portal.fixture)
            portal = PortalServer(fixture, config.portal.bind_host, 18765)
            try:
                portal.start()
            except SecurityBoundaryError as exc:
                self.skipTest(f"demo port unavailable: {exc}")
            try:
                outcome = run_pipeline(config, reset=True)
            finally:
                portal.stop()
            generated = json.loads(
                (config.download.report_dir / "report.json").read_text(encoding="utf-8")
            )
            golden = json.loads(
                (ROOT / "fixtures/golden/report.json").read_text(encoding="utf-8")
            )
            self.assertEqual(outcome.status, "partial")
            self.assertEqual(generated, golden)


if __name__ == "__main__":
    unittest.main()

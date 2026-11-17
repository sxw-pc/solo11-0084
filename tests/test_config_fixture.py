import json
import tempfile
import unittest
from pathlib import Path

from helpers import ROOT, free_port, write_project
from releaseharbor.config import load_config
from releaseharbor.errors import ConfigError, FixtureError
from releaseharbor.fixture import load_portal_fixture


class ConfigFixtureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config_path = write_project(self.root, free_port())

    def tearDown(self):
        self.temp.cleanup()

    def _mutate_config(self, mutation):
        data = json.loads(self.config_path.read_text(encoding="utf-8"))
        mutation(data)
        self.config_path.write_text(json.dumps(data), encoding="utf-8")
        return load_config(self.config_path)

    def test_demo_config_and_fixture_load(self):
        config = load_config(ROOT / "config" / "demo.json")
        self.assertEqual(config.portal.base_url, "http://127.0.0.1:18765")
        self.assertEqual(config.discovery.channels, ("stable", "beta"))
        self.assertTrue(config.download.db_path.is_relative_to(config.root))
        fixture = load_portal_fixture(config.portal.fixture)
        self.assertEqual(len(fixture.datasets), 3)
        self.assertEqual(
            sum(len(version.artifacts) for dataset in fixture.datasets for version in dataset.versions),
            13,
        )
        samples = fixture.artifact("air-quality", "2025-01", "samples.csv")
        self.assertNotEqual(samples.declared_sha256, samples.actual_sha256)

    def test_external_portal_and_path_escape_are_rejected(self):
        with self.assertRaises(ConfigError):
            self._mutate_config(
                lambda data: data["portal"].__setitem__("base_url", "http://example.invalid")
            )
        with self.assertRaises(ConfigError):
            self._mutate_config(
                lambda data: data["download"].__setitem__("work_dir", "../outside")
            )

    def test_unknown_keys_are_rejected(self):
        with self.assertRaises(ConfigError):
            self._mutate_config(lambda data: data.__setitem__("unexpected", True))

    def test_artifact_traversal_in_fixture_is_rejected(self):
        data = json.loads((self.root / "fixtures/portal.json").read_text(encoding="utf-8"))
        data["datasets"][0]["versions"][0]["artifacts"][0]["name"] = "../escape.csv"
        (self.root / "fixtures/portal.json").write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(FixtureError):
            load_portal_fixture(self.root / "fixtures/portal.json")


if __name__ == "__main__":
    unittest.main()

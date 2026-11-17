import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from helpers import free_port, portal_for, write_project
from releaseharbor.config import load_config
from releaseharbor.discovery import Discovery
from releaseharbor.httpclient import SafeHttpClient
from releaseharbor.security import UrlPolicy


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = load_config(write_project(Path(self.temp.name), free_port()))
        self.portal = portal_for(self.config).start()
        self.events = []
        self.client = SafeHttpClient(UrlPolicy(self.portal.base_url))
        self.discovery = Discovery(
            self.client, self.config, on_event=lambda code, subject, detail: self.events.append(code)
        )

    def tearDown(self):
        self.portal.stop()
        self.temp.cleanup()

    def test_full_discovery_filters_channels_and_is_deterministic(self):
        result = self.discovery.discover()
        self.assertEqual([item.dataset_id for item in result.datasets], ["air-quality", "civic-budget", "transit-counts"])
        self.assertEqual(sum(len(item.versions) for item in result.datasets), 8)
        artifacts = [art for ds in result.datasets for ver in ds.versions for art in ver.artifacts]
        self.assertEqual(len(artifacts), 12)
        self.assertNotIn("nightly-2025-05-11", [ver.version_id for ds in result.datasets for ver in ds.versions])
        self.assertIn("discovery.channel_filtered", self.events)
        self.assertEqual(result.pages_fetched, 13)
        self.assertEqual(result.blocked, [])

    def test_limits_apply_to_every_level(self):
        limited = replace(
            self.config,
            discovery=replace(
                self.config.discovery,
                max_datasets=2,
                max_versions=1,
                max_artifacts=1,
            ),
        )
        result = Discovery(self.client, limited).discover()
        self.assertEqual(len(result.datasets), 2)
        self.assertEqual(sum(len(item.versions) for item in result.datasets), 2)
        self.assertEqual(
            sum(len(ver.artifacts) for ds in result.datasets for ver in ds.versions), 2
        )

    def test_artifact_metadata_is_complete(self):
        result = self.discovery.discover()
        artifact = result.datasets[2].versions[1].artifacts[0]
        self.assertEqual(artifact.dataset_id, "transit-counts")
        self.assertEqual(artifact.filename, "stations-manifest.txt")
        self.assertEqual(len(artifact.expected_sha256), 64)
        self.assertGreater(artifact.expected_size, 0)
        self.assertTrue(artifact.url.startswith(self.portal.base_url))


if __name__ == "__main__":
    unittest.main()

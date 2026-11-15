import tempfile
import unittest
from pathlib import Path

from helpers import free_port, portal_for, ref_for, write_project
from releaseharbor.clock import Clock
from releaseharbor.config import load_config
from releaseharbor.downloader import Downloader, artifact_destination
from releaseharbor.fixture import load_portal_fixture
from releaseharbor.httpclient import SafeHttpClient
from releaseharbor.security import UrlPolicy
from releaseharbor.verify import verify_file


class DownloaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = load_config(write_project(Path(self.temp.name), free_port()))
        self.portal = portal_for(self.config).start()
        client = SafeHttpClient(UrlPolicy(self.portal.base_url))
        self.downloader = Downloader(self.config, client, Clock())
        self.fixtures = load_portal_fixture(self.config.portal.fixture)

    def tearDown(self):
        self.portal.stop()
        self.temp.cleanup()

    def test_complete_download_and_digest(self):
        ref = ref_for(self.config, "civic-budget", "2025.1", "budget_lines.csv")
        result = self.downloader.download(ref)
        self.assertTrue(result.accepted)
        self.assertEqual(result.attempts, 1)
        integrity = verify_file(result.path, ref.expected_size, ref.expected_sha256)
        self.assertTrue(integrity.accepted)
        self.assertFalse(result.partial_path.exists())

    def test_partial_file_is_resumed_with_range(self):
        ref = ref_for(self.config, "transit-counts", "v3", "stations.csv")
        target = artifact_destination(
            self.config.download.artifact_dir, ref.dataset_id, ref.version_id, ref.filename
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(target.name + ".part")
        original = self.fixtures.artifact("transit-counts", "v3", "stations.csv").content
        partial.write_bytes(original[:40])
        result = self.downloader.download(ref)
        self.assertTrue(result.accepted)
        self.assertEqual(result.resumed_from, 40)
        self.assertEqual(result.bytes_written, len(original))
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual(result.attempts, 1)

    def test_transient_failure_is_retried(self):
        ref = ref_for(self.config, "civic-budget", "2025.1", "budget_notes.txt")
        result = self.downloader.download(ref)
        self.assertTrue(result.accepted)
        self.assertEqual(result.attempts, 2)
        integrity = verify_file(result.path, ref.expected_size, ref.expected_sha256)
        self.assertTrue(integrity.accepted)

    def test_oversized_partial_is_discarded(self):
        ref = ref_for(self.config, "air-quality", "2025-01", "samples.jsonl")
        target = artifact_destination(
            self.config.download.artifact_dir, ref.dataset_id, ref.version_id, ref.filename
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(target.name + ".part")
        partial.write_bytes(b"x" * (ref.expected_size + 20))
        result = self.downloader.download(ref)
        self.assertTrue(result.accepted)
        self.assertEqual(result.resumed_from, 0)
        self.assertEqual(target.stat().st_size, ref.expected_size)


if __name__ == "__main__":
    unittest.main()

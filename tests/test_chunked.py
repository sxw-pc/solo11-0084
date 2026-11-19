import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from helpers import (
    CHUNK_SIZE,
    CHUNKED_CONTENT,
    CountingClient,
    free_port,
    portal_for,
    ref_for,
    seed_partial,
    write_chunked_project,
)
from releaseharbor.clock import Clock
from releaseharbor.config import load_config
from releaseharbor.discovery import Discovery
from releaseharbor.downloader import Downloader, artifact_destination
from releaseharbor.httpclient import SafeHttpClient
from releaseharbor.repair import chunk_temp_path
from releaseharbor.security import UrlPolicy
from releaseharbor.util import sha256_bytes
from releaseharbor.verify import verify_file


class ChunkedDownloadCase:
    """Shared portal/config harness; concrete contracts set class attributes."""

    contract = "repair"
    budget = 4096

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = load_config(
            write_chunked_project(
                Path(self.temp.name),
                free_port(),
                contract=self.contract,
                max_recovery_bytes=self.budget,
            )
        )
        self.portal = portal_for(self.config).start()
        self.client = CountingClient(SafeHttpClient(UrlPolicy(self.portal.base_url)))
        self.events = []
        self.downloader = Downloader(
            self.config,
            self.client,
            Clock(),
            on_event=lambda code, subject, detail: self.events.append(code),
        )

    def tearDown(self):
        self.portal.stop()
        self.temp.cleanup()

    def ref(self, filename="payload.bin"):
        return ref_for(self.config, "chunked-releases", "v1", filename)

    def target(self, ref):
        return artifact_destination(
            self.config.download.artifact_dir, ref.dataset_id, ref.version_id, ref.filename
        )

    def tampered_middle(self, ref):
        data = bytearray(CHUNKED_CONTENT)
        data[CHUNK_SIZE : 2 * CHUNK_SIZE] = b"X" * CHUNK_SIZE
        return seed_partial(self.config, ref, bytes(data))


class ChunkedDownloadTests(ChunkedDownloadCase, unittest.TestCase):
    """Local-repair contract with a generous recovery byte budget."""

    def test_version_page_declares_chunk_manifest(self):
        url = self.portal.base_url + "/datasets/chunked-releases/versions/v1"
        with self.client.open("GET", url) as response:
            body = response.text()
        self.assertIn(f'data-chunk-size="{CHUNK_SIZE}"', body)
        self.assertIn(sha256_bytes(CHUNKED_CONTENT[:CHUNK_SIZE]), body)
        self.assertIn(sha256_bytes(CHUNKED_CONTENT), body)

    def test_discovery_parses_manifest_and_rejects_inconsistent(self):
        result = Discovery(self.client, self.config).discover()
        artifacts = {
            art.filename: art
            for dataset in result.datasets
            for version in dataset.versions
            for art in version.artifacts
        }
        self.assertNotIn("broken-manifest.bin", artifacts)
        self.assertTrue(
            any(item["code"] == "artifact.metadata_invalid" for item in result.issues)
        )
        payload = artifacts["payload.bin"]
        self.assertEqual(payload.chunk_size, CHUNK_SIZE)
        self.assertEqual(len(payload.chunk_sha256), 4)
        self.assertEqual(payload.chunk_sha256[-1], sha256_bytes(CHUNKED_CONTENT[-64:]))

    def test_fresh_download_accepts_all_chunks(self):
        ref = self.ref()
        result = self.downloader.download(ref)
        self.assertTrue(result.accepted, result.error_detail)
        self.assertEqual(self.target(ref).read_bytes(), CHUNKED_CONTENT)
        self.assertTrue(verify_file(result.path, ref.expected_size, ref.expected_sha256).accepted)
        self.assertEqual(len(self.client.gets()), 1)
        self.assertIn("download.chunk_audit", self.events)

    def test_empty_manifest_artifact_downloads(self):
        ref = self.ref("empty.bin")
        result = self.downloader.download(ref)
        self.assertTrue(result.accepted, result.error_detail)
        self.assertEqual(self.target(ref).read_bytes(), b"")
        self.assertTrue(verify_file(result.path, 0, sha256_bytes(b"")).accepted)

    def test_repair_rejects_extra_byte_response(self):
        ref = self.ref("extra-byte.bin")
        partial = self.tampered_middle(ref)
        before = partial.read_bytes()
        result = self.downloader.download(ref)
        self.assertFalse(result.accepted)
        self.assertEqual(result.error_code, "chunk_repair_failed")
        self.assertEqual(partial.read_bytes(), before)
        self.assertFalse(self.target(ref).exists())
        self.assertEqual(len(self.client.gets()), 3)

    def test_repair_rejects_wrong_content_range_total(self):
        ref = self.ref("bad-total.bin")
        partial = self.tampered_middle(ref)
        before = partial.read_bytes()
        result = self.downloader.download(ref)
        self.assertFalse(result.accepted)
        self.assertEqual(result.error_code, "chunk_repair_failed")
        self.assertEqual(partial.read_bytes(), before)
        self.assertFalse(self.target(ref).exists())

    def test_repair_rejects_swapped_server_content(self):
        ref = self.ref("swapped.bin")
        partial = self.tampered_middle(ref)
        before = partial.read_bytes()
        result = self.downloader.download(ref)
        self.assertFalse(result.accepted)
        self.assertEqual(result.error_code, "chunk_repair_failed")
        self.assertEqual(partial.read_bytes(), before)
        self.assertFalse(self.target(ref).exists())

    def test_stale_chunk_declaration_fails_bounded(self):
        ref = self.ref("stale-chunks.bin")
        result = self.downloader.download(ref)
        self.assertFalse(result.accepted)
        self.assertEqual(result.error_code, "chunk_repair_failed")
        self.assertEqual(len(self.client.gets()), 4)

    def test_invalid_chunk_manifest_fails_without_transfer(self):
        ref = replace(self.ref(), chunk_sha256=self.ref().chunk_sha256[:3])
        result = self.downloader.download(ref)
        self.assertFalse(result.accepted)
        self.assertEqual(result.error_code, "chunk_manifest_invalid")
        self.assertEqual(self.client.gets(), [])

    def test_append_resume_keeps_verified_chunks(self):
        ref = self.ref()
        seed_partial(self.config, ref, CHUNKED_CONTENT[:200])
        result = self.downloader.download(ref)
        self.assertTrue(result.accepted, result.error_detail)
        self.assertEqual(result.resumed_from, 200)
        gets = self.client.gets()
        self.assertEqual(len(gets), 1)
        self.assertEqual(gets[0]["headers"].get("Range"), "bytes=200-")
        self.assertEqual(self.target(ref).read_bytes(), CHUNKED_CONTENT)

    def test_staged_chunk_temp_is_spliced_without_network(self):
        ref = self.ref()
        partial = self.tampered_middle(ref)
        temp = chunk_temp_path(partial, 1)
        temp.write_bytes(CHUNKED_CONTENT[CHUNK_SIZE : 2 * CHUNK_SIZE])
        result = self.downloader.download(ref)
        self.assertTrue(result.accepted, result.error_detail)
        self.assertEqual(self.client.gets(), [])
        self.assertEqual(self.target(ref).read_bytes(), CHUNKED_CONTENT)
        self.assertFalse(temp.exists())

    def test_complete_partial_promotes_without_network(self):
        ref = self.ref()
        seed_partial(self.config, ref, CHUNKED_CONTENT)
        result = self.downloader.download(ref)
        self.assertTrue(result.accepted, result.error_detail)
        self.assertEqual(self.client.gets(), [])
        self.assertEqual(self.target(ref).read_bytes(), CHUNKED_CONTENT)

    def test_short_last_chunk_is_repaired(self):
        ref = self.ref()
        data = bytearray(CHUNKED_CONTENT)
        data[3 * CHUNK_SIZE :] = b"Y" * 64
        seed_partial(self.config, ref, bytes(data))
        result = self.downloader.download(ref)
        self.assertTrue(result.accepted, result.error_detail)
        gets = self.client.gets()
        self.assertEqual(len(gets), 1)
        self.assertEqual(
            gets[0]["headers"].get("Range"), f"bytes={3 * CHUNK_SIZE}-{len(CHUNKED_CONTENT) - 1}"
        )
        self.assertEqual(self.target(ref).read_bytes(), CHUNKED_CONTENT)


class TightBudgetRepairTests(ChunkedDownloadCase, unittest.TestCase):
    """Repair contract with a budget that covers exactly one chunk Range."""

    contract = "repair"
    budget = CHUNK_SIZE

    def test_bad_middle_chunk_repaired_within_one_chunk_budget(self):
        ref = self.ref()
        self.tampered_middle(ref)
        result = self.downloader.download(ref)
        self.assertTrue(result.accepted, result.error_detail)
        self.assertEqual(self.target(ref).read_bytes(), CHUNKED_CONTENT)
        self.assertEqual(result.chunks_repaired, 1)
        self.assertEqual(result.recovery_bytes, CHUNK_SIZE)
        gets = self.client.gets()
        self.assertEqual(len(gets), 1)
        self.assertEqual(
            gets[0]["headers"].get("Range"), f"bytes={CHUNK_SIZE}-{2 * CHUNK_SIZE - 1}"
        )


class ConservativeContractTests(ChunkedDownloadCase, unittest.TestCase):
    """Conservative contract: any bad on-disk chunk discards the whole partial."""

    contract = "conservative"
    budget = 4096

    def test_conservative_discards_partial_and_redownloads(self):
        ref = self.ref()
        self.tampered_middle(ref)
        result = self.downloader.download(ref)
        self.assertTrue(result.accepted, result.error_detail)
        self.assertEqual(self.target(ref).read_bytes(), CHUNKED_CONTENT)
        gets = self.client.gets()
        self.assertEqual(len(gets), 1)
        self.assertNotIn("Range", gets[0]["headers"])
        self.assertIn("download.contract_discard", self.events)

    def test_conservative_rejects_swapped_server_content(self):
        ref = self.ref("swapped.bin")
        self.tampered_middle(ref)
        result = self.downloader.download(ref)
        self.assertFalse(result.accepted)
        self.assertEqual(result.error_code, "chunk_audit_failed")
        self.assertFalse(self.target(ref).exists())
        self.assertEqual(len(self.client.gets()), 3)


class ConservativeTightBudgetTests(ChunkedDownloadCase, unittest.TestCase):
    """Same 4-chunk input, budget fits one chunk Range but not a whole GET."""

    contract = "conservative"
    budget = CHUNK_SIZE

    def test_conservative_stays_unaccepted(self):
        ref = self.ref()
        self.tampered_middle(ref)
        result = self.downloader.download(ref)
        self.assertFalse(result.accepted)
        self.assertEqual(result.error_code, "recovery_budget_exceeded")
        self.assertEqual(self.client.gets(), [])
        self.assertFalse(self.target(ref).exists())


if __name__ == "__main__":
    unittest.main()

import json
import tempfile
import unittest
from pathlib import Path

from helpers import (
    CHUNK_SIZE,
    CHUNKED_CONTENT,
    CountingClient,
    free_port,
    portal_for,
    ref_for,
    write_chunked_project,
)
from releaseharbor.clock import Clock
from releaseharbor.collector import ArtifactProcessor
from releaseharbor.config import load_config
from releaseharbor.db import connect
from releaseharbor.discovery import DiscoveredDataset, DiscoveredVersion
from releaseharbor.downloader import Downloader, artifact_destination
from releaseharbor.httpclient import SafeHttpClient
from releaseharbor.ledger import Ledger
from releaseharbor.pipeline import run_pipeline
from releaseharbor.security import UrlPolicy


class ChunkedRecoveryTests(unittest.TestCase):
    """Crash cut points: chunk state in DB must never be trusted over disk bytes."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = load_config(write_chunked_project(Path(self.temp.name), free_port()))
        self.portal = portal_for(self.config).start()
        self.client = CountingClient(SafeHttpClient(UrlPolicy(self.portal.base_url)))
        self.connection = connect(self.config.download.db_path)
        self.ledger = Ledger(self.connection, Clock())
        self.run_id = "run-recovery"
        self.ledger.start_run(self.run_id, "fingerprint")
        self.downloader = Downloader(
            self.config,
            self.client,
            Clock(),
            on_chunk_state=lambda uid, index, digest, status: self.ledger.record_chunk_state(
                uid, self.run_id, index, digest, status
            ),
        )

    def tearDown(self):
        self.connection.close()
        self.portal.stop()
        self.temp.cleanup()

    def ref(self, filename="payload.bin"):
        return ref_for(self.config, "chunked-releases", "v1", filename)

    def target(self, ref):
        return artifact_destination(
            self.config.download.artifact_dir, ref.dataset_id, ref.version_id, ref.filename
        )

    def persist_manifest(self, ref):
        dataset = DiscoveredDataset(
            ref.dataset_id, "Chunked", "publisher", "license", self.portal.base_url
        )
        version = DiscoveredVersion(
            ref.version_id, "v1", "stable", "2026-01-02", self.portal.base_url
        )
        self.ledger.upsert_dataset(self.run_id, dataset)
        self.ledger.upsert_version(self.run_id, version, ref.dataset_id)

    def processor(self):
        return ArtifactProcessor(self.config, self.client, self.ledger, self.downloader, self.run_id)

    def test_persisted_chunk_state_does_not_trust_disk(self):
        ref = self.ref()
        first = self.downloader.download(ref)
        self.assertTrue(first.accepted, first.error_detail)
        self.assertEqual(len(self.client.gets()), 1)
        rows = self.ledger.chunk_states(ref.artifact_uid)
        self.assertEqual(len(rows), 4)
        self.assertTrue(all(row["status"] == "accepted" for row in rows))
        # Crash window: DB says every chunk is accepted, then disk bytes are rewritten.
        target = self.target(ref)
        data = bytearray(target.read_bytes())
        data[2 * CHUNK_SIZE : 3 * CHUNK_SIZE] = b"Q" * CHUNK_SIZE
        target.write_bytes(bytes(data))
        second = self.downloader.download(ref)
        self.assertTrue(second.accepted, second.error_detail)
        self.assertEqual(target.read_bytes(), CHUNKED_CONTENT)
        gets = self.client.gets()[1:]
        self.assertEqual(len(gets), 1)
        self.assertEqual(
            gets[0]["headers"].get("Range"), f"bytes={2 * CHUNK_SIZE}-{3 * CHUNK_SIZE - 1}"
        )
        self.assertEqual(len(self.ledger.chunk_states(ref.artifact_uid)), 4)

    def test_formal_target_reverified_after_interruption(self):
        ref = self.ref()
        self.persist_manifest(ref)
        # Crash window: formal file replaced, ledger still says "downloading".
        target = self.target(ref)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(CHUNKED_CONTENT)
        self.ledger.upsert_artifact(self.run_id, ref)
        self.ledger.update_artifact(ref.artifact_uid, self.run_id, status="downloading")
        outcome = self.processor().process(ref)
        self.assertEqual(outcome, "verified")
        self.assertEqual(self.client.gets(), [])
        row = self.ledger.get_artifact(ref.artifact_uid)
        self.assertEqual(row["status"], "verified")

    def test_chunk_manifest_contradiction_quarantines_without_retry(self):
        ref = self.ref("wrong-digest.bin")
        self.persist_manifest(ref)
        outcome = self.processor().process(ref)
        self.assertEqual(outcome, "checksum_mismatch")
        row = self.ledger.get_artifact(ref.artifact_uid)
        self.assertEqual(row["status"], "checksum_mismatch")
        self.assertEqual(row["attempts"], 1)
        self.assertEqual(len(self.client.gets()), 1)
        quarantined = (
            self.config.download.quarantine_dir / "chunked-releases/v1/wrong-digest.bin"
        )
        self.assertTrue(quarantined.is_file())


class ChunkedPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = load_config(write_chunked_project(Path(self.temp.name), free_port()))
        self.portal = portal_for(self.config).start()

    def tearDown(self):
        self.portal.stop()
        self.temp.cleanup()

    def test_pipeline_records_chunk_states_and_events(self):
        outcome = run_pipeline(self.config, reset=True)
        self.assertEqual(outcome.status, "partial")
        self.assertEqual(outcome.summary["artifacts"], 7)
        self.assertEqual(outcome.summary["verified"], 4)
        self.assertEqual(outcome.summary["quarantined"], 1)
        self.assertEqual(outcome.summary["failed"], 2)
        report_path = self.config.root / outcome.reports["report_json"]
        report = json.loads(report_path.read_text(encoding="utf-8"))
        artifacts = [
            artifact
            for dataset in report["datasets"]
            for version in dataset["versions"]
            for artifact in version["artifacts"]
        ]
        payload = next(item for item in artifacts if item["filename"] == "payload.bin")
        self.assertEqual(len(payload["chunks"]), 4)
        self.assertTrue(all(item["status"] == "accepted" for item in payload["chunks"]))
        swapped = next(item for item in artifacts if item["filename"] == "swapped.bin")
        self.assertEqual(swapped["error"]["code"], "chunk_repair_failed")
        self.assertNotIn("chunks", swapped)
        audit = (self.config.root / outcome.reports["audit_jsonl"]).read_text(encoding="utf-8")
        self.assertIn("download.chunk_audit", audit)
        self.assertIn("artifact.quarantined", audit)
        connection = connect(self.config.download.db_path)
        try:
            rows = connection.execute(
                "SELECT artifact_uid, COUNT(*) AS amount FROM artifact_chunks "
                "GROUP BY artifact_uid"
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual(len(rows), 4)
        self.assertTrue(all(row["amount"] == 4 for row in rows))


if __name__ == "__main__":
    unittest.main()

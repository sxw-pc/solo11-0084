"""Address contract and peer-pinning tests for the loopback HTTP client.

Every test uses a fake resolver and/or fake socket factory from
``pinning_support``; no external network address is ever dialled.
"""

import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import EXTERNAL_V4, EXTERNAL_V6, event_sink, free_port, portal_for, write_project
from pinning_support import aio
from releaseharbor.config import load_config
from releaseharbor.errors import SecurityBoundaryError
from releaseharbor.httpclient import SafeHttpClient
from releaseharbor.security import AddressContract, LoopbackConnector, UrlPolicy


class _PortalHarness(unittest.TestCase):
    host = "127.0.0.1"
    contract = None

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = load_config(
            write_project(
                Path(self.temp.name), free_port(), host=self.host, contract=self.contract
            )
        )
        self.portal = portal_for(self.config).start()

    def tearDown(self):
        self.portal.stop()
        self.temp.cleanup()

    def client(self, **kwargs):
        events, sink = event_sink()
        policy = UrlPolicy(
            self.config.portal.base_url, contract=self.config.portal.address_contract
        )
        kwargs.setdefault("on_event", sink)
        return SafeHttpClient(policy, **kwargs), events

    def collect(self):
        from releaseharbor.clock import Clock
        from releaseharbor.collector import ArtifactProcessor
        from releaseharbor.db import connect
        from releaseharbor.discovery import Discovery
        from releaseharbor.downloader import Downloader
        from releaseharbor.ledger import Ledger
        from releaseharbor.pipeline import _persist_manifest

        client, events = self.client()
        connection = connect(self.config.download.db_path)
        clock = Clock.from_settings(self.config.clock_mode, self.config.clock_fixed)
        ledger = Ledger(connection, clock)
        run_id = ledger.allocate_run_id("run-pin-test")
        ledger.start_run(run_id, self.config.fingerprint)
        result = Discovery(client, self.config).discover()
        _persist_manifest(ledger, run_id, result)
        downloader = Downloader(self.config, client, clock)
        processor = ArtifactProcessor(self.config, client, ledger, downloader, run_id)
        statuses = [
            processor.process(ref)
            for dataset in result.datasets
            for version in dataset.versions
            for ref in version.artifacts
        ]
        connection.close()
        return statuses, events


class LiteralContractRejectsLocalhostName(_PortalHarness):
    """A portal bound on 127.0.0.1 but configured with 'localhost'."""

    host = "localhost"
    contract = "pinned_name"

    def test_literal_policy_refuses_localhost_origin(self):
        with self.assertRaises(SecurityBoundaryError) as caught:
            UrlPolicy("http://localhost:%d" % self.portal.port)
        self.assertIn("localhost", str(caught.exception))

    def test_literal_client_refuses_with_audited_reason(self):
        events, sink = event_sink()
        policy = UrlPolicy("http://127.0.0.1:%d" % self.portal.port)
        client = SafeHttpClient(policy, on_blocked=sink)
        url = "http://localhost:%d/datasets/d" % self.portal.port
        with self.assertRaises(SecurityBoundaryError):
            client.open("GET", url)
        self.assertEqual([event["code"] for event in events], ["target.blocked"])
        self.assertIn("localhost", events[0]["detail"]["reason"])


class PinnedNameContractCollectsPortal(_PortalHarness):
    host = "localhost"
    contract = "pinned_name"

    def test_config_uses_pinned_name_contract(self):
        self.assertEqual(self.config.portal.address_contract, AddressContract.PINNED_NAME)
        self.assertEqual(self.portal.host, "127.0.0.1")

    def test_pinned_name_contract_successfully_collects(self):
        statuses, events = self.collect()
        self.assertEqual(statuses.count("verified"), 8)
        self.assertEqual(statuses.count("checksum_mismatch"), 1)
        self.assertNotIn("target.blocked", [event["code"] for event in events])

    def test_pinned_name_connection_uses_numeric_peer(self):
        client, _ = self.client()
        target = client.validate(self.config.portal.base_url + "/")
        peer = client.pin(target)
        self.assertTrue(peer.is_loopback)
        self.assertEqual(peer.address, "127.0.0.1")
        self.assertEqual(peer.port, self.portal.port)

    def test_mixed_resolution_still_reaches_the_local_portal(self):
        def resolver(host, port, type=None):
            return aio(port, EXTERNAL_V4, EXTERNAL_V6, "127.0.0.1")

        client, events = self.client(
            connector=LoopbackConnector(resolver=resolver)
        )
        url = self.config.portal.base_url + "/"
        with mock.patch.object(socket, "getaddrinfo", resolver):
            with client.open("GET", url) as response:
                self.assertEqual(response.status, 200)
                self.assertIn("dataset-link", response.text())
        codes = [event["code"] for event in events]
        self.assertIn("target.resolution_filtered", codes)
        self.assertNotIn("target.blocked", codes)
